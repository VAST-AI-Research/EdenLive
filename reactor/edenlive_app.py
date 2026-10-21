# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""Serve EdenLive without shadowing the upstream ``edenlive`` Python package."""

import asyncio
import json
import time
from concurrent.futures import Future
from pathlib import Path
from typing import NoReturn

import numpy as np
from edenlive_assets import GAMES, resolve_assets
from edenlive_draft import DRAFT_HEIGHT, DRAFT_WIDTH, ClientSource, DraftSource, fit_frames
from edenlive_game import (
    CLOSE_TIMEOUT_S,
    NO_LOOK,
    CaptureStalled,
    EdenGame,
    browser_drivers,
    join_closing_games,
)
from edenlive_images import decode_image, read_image
from edenlive_model import (
    EdenAnchor,
    EdenBank,
    EdenInput,
    EdenModel,
    EdenResult,
    condition_frames,
)
from edenlive_timing import ChunkTimer
from edenlive_types import SOURCES, EdenMedia, EdenOutput, EdenState, Key, StateUpdate
from PIL import Image
from reactor_runtime import (
    ApplicationError,
    ClientInfo,
    CommandError,
    InputField,
    ReactorApp,
    StepOutcome,
    TrackPayload,
    UploadedFile,
    connected,
    disconnected,
    event,
    get_logger,
    session_ended,
    session_started,
)
from reactor_runtime.distributed import DistributedRunner
from reactor_runtime.paths import get_weights_path

logger = get_logger(__name__)

# Per game, the event combinations its chunks carry, most frequent first. The
# scene text joined with each is a prompt the bank encodes before play.
BANK_EVENTS = Path(__file__).with_name("edenlive_bank.json")
# Chunks the load-time warm-up session plays of each game: a world's first
# chunk and its first 12-frame chunk, the ones slower in a fresh process.
WARM_UP_CHUNKS = 2
# Pre-roll frames: the reference plus one of a few fixed noise patterns, so each
# frame differs from the last and the encoder sends a steady stream of bits.
PREROLL_PATTERNS = 8
PREROLL_NOISE = 6
PREROLL_EMIT = 4


def chunk_prompt(scene: str, events) -> str:
    """A chunk's prompt: the scene text followed by that chunk's event sentences."""
    return " ".join((scene, *events))


class EdenLive(ReactorApp):
    """Reskin a draft video, from a native game or from the client, into a generated world."""

    state: EdenState
    media: EdenMedia
    # One chunk, the smallest bound the runtime applies: the least latency.
    buffer_size = 12
    # A chunk covers 0.5 s of the game's 24 FPS clock. `play_fps` in edenlive.yaml
    # re-pins the playback rate in load(); any value other than 24 plays the same
    # frames faster or slower than the game ran. A client chunk plays at the
    # client's own rate instead (`_play_at`).
    fps = 24

    def __init__(self):
        super().__init__()
        self._engine = None
        self._draft_source: DraftSource | None = None
        # A game launched ahead for the selected game and not yet played, so
        # `start` takes a loaded page instead of waiting for a browser.
        self._standby: EdenGame | None = None
        self._standby_launched: Future | None = None
        self._root = None
        self._entries = {}
        self._capture = (1280, 704)
        self._capture_format = "raw"
        self._draft_video = "sparse"
        self._timer = ChunkTimer()
        self._last_prompt = ""
        self._last_refusal: str | None = None
        self._bank_events: dict[str, list[list[str]]] = {}
        self._play_fps = type(self).fps
        self._preroll_s = 0.0
        self._preroll: asyncio.Task | None = None
        self._preroll_stop = asyncio.Event()
        self._preroll_noise: np.ndarray | None = None

    def load(self, config_path: Path | None) -> None:
        (
            self._root,
            weights,
            self._entries,
            execution,
            self._capture,
            self._capture_format,
            self._draft_video,
            play_fps,
            self._preroll_s,
        ) = resolve_assets(config_path, get_weights_path())
        self._bank_events = json.loads(BANK_EVENTS.read_text())
        # The runtime reads the pinned rate from the class when the loop starts.
        type(self).fps = play_fps
        self._play_fps = play_fps
        self._engine = DistributedRunner(
            EdenModel,
            world_size=1,
            call_timeout=180,
            load_kwargs={"source": self._root, "weights": weights, **execution},
        )
        self._engine.start()
        if execution["warmup"]:
            asyncio.run(self._warm_up())

    async def _warm_up(self) -> None:
        """Play a throwaway session through the application's own path.

        A fresh process pays once for what the model's own warm-up never
        reaches: the first cover read, the first import of the capture module
        and Playwright, the first browser launch, each game's first captures
        (slower the first time that game is played in the process, even in a
        new browser), and the first chunks over the runner's shared memory,
        whose blocks grow to fit them. This session selects and starts every
        game and plays its first ``WARM_UP_CHUNKS`` chunks through the
        commands and the three step calls, as the runtime's step loop would,
        with no client: before the runtime binds the application, `send` and
        `output` reach nobody. Only the first game builds its prompt bank;
        the others would repeat that path for about 1.4 s each.

        It ends as a session does (`@session_ended`), waits for every browser
        it launched to exit, and leaves the application's state as a session
        end leaves it. The process keeps what any earlier session leaves
        behind: the runner's grown blocks and the opening prompts' embeddings.
        """
        started = time.monotonic()
        bank_events = self._bank_events
        self._bank_events = {GAMES[0]: bank_events.get(GAMES[0], [])}
        self.state = EdenState()
        try:
            self.on_session_started()
            for game in GAMES:
                await self.set_paused(paused=True)
                await self.select_game(game=game)
                await self.start(seed=0)
                while self.state._chunks < WARM_UP_CHUNKS:
                    await self._step()
        finally:
            self.on_session_ended()
            self.state = None
            self._bank_events = bank_events
            self._timer = ChunkTimer()
            self._last_prompt = ""
            self._last_refusal = None
        left = await asyncio.to_thread(join_closing_games, 2 * CLOSE_TIMEOUT_S + 5)
        logger.info(
            "EdenLive session warm-up done",
            games=len(GAMES),
            chunks=WARM_UP_CHUNKS,
            seconds=round(time.monotonic() - started, 1),
            closing_left=left,
            browsers_left=len(browser_drivers()),
        )

    async def _step(self) -> EdenOutput | None:
        """One turn of the runtime's step loop: the three step calls, or a refusal."""
        try:
            input = await self.process_input()
        except ApplicationError:
            await asyncio.sleep(0.005)
            return None
        started = time.perf_counter()
        try:
            result = self.generate(input)
        except Exception as error:  # noqa: BLE001 - process_output decides, as in the runtime
            outcome = StepOutcome(error=error, elapsed=time.perf_counter() - started)
        else:
            outcome = StepOutcome(result=result, elapsed=time.perf_counter() - started)
        return await self.process_output(outcome)

    @connected
    async def on_connected(self, client: ClientInfo) -> None:
        logger.info("client connected", client=client.id)
        await client.send(self._state_update())
        self._begin_preroll()

    @disconnected
    async def on_disconnected(self, client: ClientInfo) -> None:
        logger.info("client disconnected", client=client.id)
        if not self.connected.is_set():
            await self._stop_preroll()
        # Only the client holding the keys or the look releases them; a viewer
        # leaving changes nothing.
        if client.id not in (self.state._keys_holder, self.state._look_holder):
            return
        self._release_keys()
        await self.send(self._state_update())

    @session_started
    def on_session_started(self) -> None:
        self._timer = ChunkTimer()
        self._last_prompt = ""
        self._last_refusal = None

    @session_ended
    def on_session_ended(self) -> None:
        # Runs on the event loop under the step lock: nothing here may wait
        # on the game's browser. _close_source() hands that to its own thread.
        started = time.monotonic()
        self._preroll_stop.set()
        self._preroll = None
        self._close_source()
        self._close_standby()
        self._release_keys()
        self._engine.reset()
        logger.info("session ended", teardown_s=round(time.monotonic() - started, 3))

    # -- commands -------------------------------------------------------------

    @event(
        name="set_source",
        description=(
            "Choose where the draft video comes from: `game` (the selected native "
            "game, steered with `set_keys`) or `client` (the client's `source_video` "
            "track: each chunk takes the latest frames and plays them at the rate "
            "the client sends them, 24 fps until measured). Valid any "
            "time. Before `start` it picks the source `start` "
            "uses. During a world the switch applies from the next chunk and the "
            "world keeps its visual history; switching to `game` starts the "
            "selected game from its opening, and switching away from a game that "
            "reached an outcome lets `set_paused` resume. Emits `state_update`. "
            "Fails with `game_required` when switching a started world to `game` "
            "before `select_game`."
        ),
    )
    async def set_source(
        self,
        source: str = InputField(
            choices=list(SOURCES),
            description="`game` or `client`.",
        ),
    ) -> StateUpdate:
        logger.info("command", command="set_source", source=source)
        state = self.state
        if source == state._source:
            return await self._publish()
        if source == "game" and state._started and not state._game:
            raise CommandError("game_required", "Choose a game before switching to it.")
        state._source = source
        state._ended = False
        state._hud = None
        state._events = ()
        self._timer.restart()
        self._release_keys()
        if state._started:
            self._open_source()
        # Mid-world, the next standby is launched after the next chunk instead.
        self._keep_standby(launch=not state._started)
        if not state._started:
            await asyncio.gather(self._standby_ready(), self._refresh_bank())
        else:
            await self._refresh_bank()
        return await self._publish()

    @event(
        name="select_game",
        description=(
            "Stage one of the official games: its cover becomes the appearance "
            "reference and its prompt becomes `prompt`. Valid while paused. Ends "
            "the current world; `start` begins a new one. With source `client` "
            "the cover and prompt style the client's video. Replies once the game "
            "is loaded and the text of its usual chunk prompts is encoded, so `start` "
            "after the reply begins at once. Emits `state_update`. "
            "Fails with `pause_required` while a world plays."
        ),
    )
    async def select_game(
        self,
        game: str = InputField(choices=list(GAMES), description="Official game to stage."),
    ) -> StateUpdate:
        logger.info("command", command="select_game", game=game)
        self._require_paused()
        entry = self._entries[game]
        path = self._root / "realtime/games" / game / entry["cover"]
        pixels = await asyncio.to_thread(read_image, path)
        self.state._game = game
        self.state.prompt = entry["prompt"]
        self._stage_reference(pixels, f"{game}/{entry['cover']}")
        self._keep_standby(launch=True)
        # Reply once the game is loaded and its prompt bank encoded: a client
        # that starts on this reply never waits for the browser, and its
        # chunks find their prompts already encoded.
        await asyncio.gather(self._standby_ready(), self._refresh_bank())
        return await self._publish()

    @event(
        name="set_image",
        description=(
            "Stage an uploaded appearance reference in place of the game cover. "
            "Valid while paused. Ends the current world; `start` begins a new one "
            "from this image. Emits `state_update`. Fails with `pause_required` "
            "while a world plays, `game_required` before `select_game` when the "
            "source is `game`, or `invalid_image` for an empty, oversized, or "
            "undecodable file."
        ),
    )
    async def set_image(
        self,
        image: UploadedFile = InputField(  # noqa: B008 - Runtime schema declaration
            moderate=True,
            description=(
                "PNG, JPEG, or WebP, at most 25 MiB and 16 megapixels. Scaled and "
                "center-cropped to 1280x704 when the world starts. Works best when "
                "its composition matches the draft's opening frame."
            ),
        ),
    ) -> StateUpdate:
        logger.info("command", command="set_image", image=image.name, bytes=len(image.data))
        self._require_paused()
        if self.state._source == "game" and not self.state._game:
            raise CommandError("game_required", "Choose a game first.")
        try:
            pixels = await asyncio.to_thread(decode_image, image.data)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            raise CommandError(
                "invalid_image", "Choose a valid PNG/JPEG/WebP up to 25 MiB and 16 MP."
            ) from error
        self._stage_reference(pixels, image.name)
        return await self._publish()

    @event(
        name="start",
        description=(
            "Start a new world from the staged reference and `prompt`, drawing the "
            "draft from the current source, and resume. Valid any time; a running "
            "world is replaced. The first chunk arrives on `main_video` and `draft_video` once the "
            "source has delivered its draft frames. Emits `state_update`. Fails "
            "with `game_required` before `select_game` when the source is `game`, "
            "`image_required` without a reference when the source is `client`, or "
            "`prompt_required` when `prompt` is blank."
        ),
    )
    async def start(
        self,
        seed: int = InputField(
            default=2026092901,
            ge=0,
            le=2147483647,
            description=(
                "Seed of the new world. The same seed, reference, and draft repeat a world."
            ),
        ),
    ) -> StateUpdate:
        state = self.state
        logger.info("command", command="start", seed=seed, source=state._source, game=state._game)
        if state._source == "game" and (not state._game or state._image is None):
            raise CommandError("game_required", "Choose a game and reference before Start.")
        if state._image is None:
            raise CommandError("image_required", "Choose a game or upload a reference first.")
        if not state.prompt.strip():
            raise CommandError("prompt_required", "Enter a scene description before Start.")
        state._seed = seed
        state._world_id += 1
        state._chunks = 0
        state._frames_wanted = 9
        state._seconds = None
        state._ended = False
        state._hud = None
        state._events = ()
        state._draft = None
        state.keys = None
        state._look = NO_LOOK
        state._look_holder = None
        state._started = True
        self._timer.restart()
        state.paused = False
        self._open_source()
        self._keep_standby(launch=False)
        await self._stop_preroll()
        self.output.flush()
        return await self._publish()

    @event(
        name="set_prompt",
        description=(
            "Change `prompt`. Valid any time; applies from the next chunk and keeps "
            "the world's visual history, so the look changes gradually. A paused "
            "world stays paused. Before a world starts the reply waits until the "
            "game's usual chunk prompts are encoded with the new text; during a "
            "world they are encoded in the background. The current text again "
            "changes nothing and replies at once. Emits `state_update`. Fails with "
            "`prompt_required` for blank text."
        ),
    )
    async def set_prompt(
        self,
        prompt: str = InputField(
            max_length=4096,
            moderate=True,
            description="Scene description, non-empty, up to 4096 characters.",
        ),
    ) -> StateUpdate:
        logger.info("command", command="set_prompt", chars=len(prompt))
        if not prompt.strip():
            raise CommandError("prompt_required", "Enter a non-empty scene description.")
        # The bank already holds this text's prompts (or is being built for it).
        if prompt.strip() == self.state.prompt.strip():
            return await self._publish()
        self.state.prompt = prompt.strip()
        # Before a world the reply waits for the new bank; during one the bank
        # is rebuilt in the background and chunks encode what it lacks so far.
        await self._refresh_bank()
        return await self._publish()

    @event(
        name="set_keys",
        description=(
            "Replace the keys held in the game. Valid any time; the next game "
            "frame not yet captured reads them, and a press released before it "
            "is captured is dropped. Ignored while paused or while the source is "
            "`client`. Emits `state_update`."
        ),
    )
    async def set_keys(
        self,
        keys: list[Key] | None = InputField(  # noqa: B008 - Runtime schema declaration
            default=None,
            max_length=14,
            description="Keys held now; empty or null releases every key.",
        ),
        client: ClientInfo | None = None,
    ) -> StateUpdate:
        # Keys arrive many times a second, so this one stays at debug.
        logger.debug("command", command="set_keys", keys=keys)
        self.state.keys = [] if self.state.paused else sorted(set(keys or ()))
        self.state._keys_holder = client.id if client is not None and self.state.keys else None
        if self._draft_source is not None:
            self._draft_source.set_keys(tuple(self.state.keys))
        return await self._publish()

    @event(
        name="set_look",
        description=(
            "Hold a look rate in the game, like a mouse turning the view: the "
            "latest value applies on every game frame not yet captured until the "
            "next call replaces it, so send (0, 0) when the movement stops. It adds "
            "to the arrow keys' look and is limited to the game's own turn speed. "
            "Valid any time; ignored while paused or while the source is `client`, "
            "and reset by `set_paused`, `set_source`, an outcome, and the session's "
            "end. Emits `state_update`."
        ),
    )
    async def set_look(
        self,
        yaw: float = InputField(
            default=0.0,
            ge=-1.0,
            le=1.0,
            description=(
                "Turn rate, -1..1: positive turns right, negative left, 1 the fastest turn; 0 none."
            ),
        ),
        pitch: float = InputField(
            default=0.0,
            ge=-1.0,
            le=1.0,
            description=(
                "Tilt rate, -1..1: positive tilts up, negative down, 1 the fastest tilt; 0 none."
            ),
        ),
        client: ClientInfo | None = None,
    ) -> StateUpdate:
        # Look arrives many times a second, so this one stays at debug.
        logger.debug("command", command="set_look", yaw=yaw, pitch=pitch)
        look = NO_LOOK if self.state.paused else (float(yaw), float(pitch))
        self.state._look = look
        self.state._look_holder = client.id if client is not None and look != NO_LOOK else None
        if self._draft_source is not None:
            self._draft_source.set_look(*look)
        return await self._publish()

    @event(
        name="set_paused",
        description=(
            "Pause or resume the world. Pausing holds the draft source and "
            "generation and releases held keys and look; resuming continues the same world. "
            "Emits `state_update`. Fails with `start_required` when resuming before "
            "`start`, or `game_ended` when resuming a game that reached an outcome."
        ),
    )
    async def set_paused(
        self,
        paused: bool = InputField(
            default=True, description="True pauses; false resumes the same world."
        ),
    ) -> StateUpdate:
        logger.info("command", command="set_paused", paused=paused)
        if not paused and not self.state._started:
            raise CommandError("start_required", "Press Start before resuming.")
        if not paused and self.state._ended:
            raise CommandError("game_ended", "This game has ended. Press Start new world.")
        self.state.paused = paused
        self._timer.restart()
        self._release_keys()
        if self._draft_source is not None:
            self._draft_source.set_paused(paused)
        return await self._publish()

    # -- the step -------------------------------------------------------------

    async def process_input(self) -> EdenInput:
        state = self.state
        if not state._started or state.paused or state._ended:
            self._refuse("Press Start, or resume the paused world.")
        source = self._draft_source
        self._timer.attempt(time.monotonic())
        source.prepare(state._frames_wanted)
        try:
            draft = await source.take()
        except CaptureStalled as stalled:
            # The browser is wedged. A fresh game, from its opening, replaces
            # it; the stalled one is closed on its own thread and killed if it
            # does not exit. The world keeps its history, as on `set_source`.
            logger.error("game stalled; restarting it", reason=str(stalled), game=state._game)
            self._open_source()
            self._refuse("The game stalled and is restarting from its opening.")
        if draft is None:
            # Refusing releases the step lock, so commands land while the
            # source captures or receives this chunk. The runtime logs this
            # refusal at debug; it is the normal wait between chunks.
            raise ApplicationError(
                f"Waiting for {state._frames_wanted} draft frames from the {source.kind}."
            )
        self._last_refusal = None
        # The next chunk is prepared only when the next step asks for it, so a
        # key accepted between steps reaches the very next chunk.
        state._draft = draft
        self._timer.drafted(time.monotonic())
        anchor = None
        if state._world_id != state._applied_world_id:
            anchor = EdenAnchor(image=state._image, prompt=state.prompt, seed=state._seed)
        prompt = chunk_prompt(state.prompt, draft.events)
        state._prompt_changed = prompt != self._last_prompt
        self._last_prompt = prompt
        return EdenInput(
            world_id=state._world_id,
            anchor=anchor,
            prompt=prompt,
            condition=condition_frames(draft.frames),
        )

    def generate(self, input: EdenInput) -> EdenResult:
        return self._engine.generate(input)

    async def process_output(self, outcome: StepOutcome) -> EdenOutput:
        if outcome.error is not None:
            # An inference failure ends the session visibly; a silent reset
            # would hide a discontinuity in the world.
            raise outcome.error
        result: EdenResult = outcome.result
        state = self.state
        draft, state._draft = state._draft, None
        if result.world_id != state._world_id or draft.frames.shape != result.frames.shape:
            raise RuntimeError("Generated chunk does not match its draft")
        metadata = [
            {"world_id": result.world_id, "chunk": result.chunk_index, **frame}
            for frame in draft.metadata
        ]
        state._applied_world_id = result.world_id
        state._chunks = result.chunk_index
        state._frames_wanted = result.frames_wanted
        state._seconds = outcome.elapsed
        state._hud = draft.hud
        state._events = draft.events
        if draft.ended:
            # Only `start` or `set_source` continues after an outcome.
            state._ended = True
            state.paused = True
            self._release_keys()
            self._draft_source.set_paused(True)
            hud = draft.hud or {}
            logger.info(
                "game reached an outcome",
                game=state._game,
                world_id=result.world_id,
                chunk=result.chunk_index,
                game_state=hud.get("game_state", ""),
                outcome=hud.get("outcome", []),
                health=hud.get("health"),
            )
        await self.send(self._state_update())
        self._measure(draft, outcome.elapsed, result.text_encoded)
        # Launched after a chunk rather than at `start`, so it does not compete
        # with the world's first chunk; the next `start` finds a loaded page.
        self._keep_standby(launch=True)
        self._play_at(draft.fps or self._play_fps)
        return EdenOutput(
            main_video=TrackPayload(result.frames, metadata=metadata),
            draft_video=TrackPayload(draft.frames, metadata=metadata),
        )

    # -- helpers --------------------------------------------------------------

    def _measure(self, draft, generate_s: float | None, text_encoded: bool) -> None:
        sample = self._timer.finished(
            time.monotonic(),
            frames=len(draft.frames),
            generate_s=generate_s,
            capture_s=draft.capture_seconds,
            prompt_changed=self.state._prompt_changed,
            text_encoded=text_encoded,
        )
        if sample is not None:
            logger.debug("chunk timing", chunk=self.state._chunks, **sample)
        summary = self._timer.summary()
        if summary is not None:
            logger.info("chunk timing summary", source=self.state._source, **summary)

    def _play_at(self, fps: float) -> None:
        """Play the chunk about to be emitted, and those after it, at ``fps``.

        The runtime tags each emitted chunk with the live rate and re-paces the
        frames still queued. A game chunk plays at `play_fps`, so with source
        `game` the rate never changes and this does nothing.
        """
        if fps != self.output.fps:
            self.output.fps = fps

    async def _refresh_bank(self) -> None:
        """Encode the prompt bank for the selected game and the current scene text.

        Only the game source produces events, so any other source drops the
        bank. Before a world starts this returns once every prompt is encoded;
        during a world it returns at once and the bank fills in the background.
        """
        state = self.state
        events = self._bank_events.get(state._game, ()) if state._source == "game" else ()
        prompts = tuple(chunk_prompt(state.prompt, combination) for combination in events)
        wait = not state._started
        started = time.monotonic()
        result = await asyncio.to_thread(
            self._engine.generate, EdenBank(prompts=prompts, wait=wait)
        )
        logger.info(
            "prompt bank",
            game=state._game,
            prompts=result.prompts,
            waited=wait,
            encode_s=round(result.seconds, 3),
            reply_s=round(time.monotonic() - started, 3),
        )

    def _open_source(self) -> None:
        self._close_source()
        if self.state._source == "game":
            # The standby game, when it is this game, is the same game launched
            # earlier: its page sits at the opening, as a new one's would.
            game, self._standby = self._standby, None
            if game is None or game.slug != self.state._game:
                if game is not None:
                    game.close()
                game = self._new_game()
            self._draft_source = game
        else:
            self._draft_source = ClientSource(self.media.source_video)
        self._draft_source.set_paused(self.state.paused)

    def _new_game(self) -> EdenGame:
        return EdenGame(
            source=self._root,
            slug=self.state._game,
            size=self._capture,
            capture_format=self._capture_format,
            draft_video=self._draft_video,
        )

    def _keep_standby(self, *, launch: bool) -> None:
        """Keep at most one standby game, for the selected game with source `game`.

        A standby for anything else is closed. With ``launch``, a missing one
        is created and its browser launched in the background. Never waits for
        a browser, like every other call on the event loop; `_standby_ready`
        is what a command awaits.
        """
        state = self.state
        slug = state._game if state._source == "game" else None
        if self._standby is not None and self._standby.slug != slug:
            self._close_standby()
        if launch and slug and self._standby is None:
            self._standby = self._new_game()
            self._standby_launched = self._standby.launch()

    async def _standby_ready(self) -> None:
        """Wait until the standby game's page is loaded, so `start` never waits for it.

        A failed launch is not raised here: the first capture launches again
        and reports its own failure.
        """
        launched = self._standby_launched if self._standby is not None else None
        if launched is not None:
            waited = asyncio.wrap_future(launched)
            await asyncio.wait([waited])
            if not waited.cancelled():
                waited.exception()  # already logged by the game as "game launch failed"

    def _close_standby(self) -> None:
        if self._standby is not None:
            self._standby.close()
            self._standby = None
            self._standby_launched = None

    def _close_source(self) -> None:
        # Called under the step lock, on the event loop, from commands and
        # from `session_ended`: a source's close() must return at once and
        # finish its teardown elsewhere.
        if self._draft_source is not None:
            self._draft_source.close()
            self._draft_source = None

    def _stage_reference(self, pixels: np.ndarray, name: str) -> None:
        state = self.state
        state._image = pixels
        state._image_name = name
        state._started = False
        state._chunks = 0
        state._seconds = None
        state._ended = False
        state._hud = None
        state._events = ()
        self._close_source()
        self.output.flush()
        # A new reference ends the world: the pre-roll covers the gap until Start.
        # (The load-time warm-up stages references before `connected` exists.)
        connected = getattr(self, "connected", None)
        if connected is not None and connected.is_set():
            self._begin_preroll()

    def _begin_preroll(self) -> None:
        """Start the pre-roll unless it is off, already playing, or a world is."""
        if self._preroll_s <= 0 or self.state._started:
            return
        if self._preroll is not None and not self._preroll.done():
            return
        self._preroll_stop = asyncio.Event()
        self._preroll = asyncio.create_task(self._play_preroll(self._preroll_stop))

    def _preroll_frames(self, image: np.ndarray | None) -> np.ndarray:
        """The pre-roll's frames: ``image`` (black when None) fitted to the output, plus noise."""
        if self._preroll_noise is None:
            rng = np.random.default_rng(0)
            shape = (PREROLL_PATTERNS, DRAFT_HEIGHT, DRAFT_WIDTH, 3)
            self._preroll_noise = rng.integers(-PREROLL_NOISE, PREROLL_NOISE + 1, shape, np.int8)
        if image is None:
            base = np.full((DRAFT_HEIGHT, DRAFT_WIDTH, 3), PREROLL_NOISE, np.int16)
        else:
            base = fit_frames([image])[0].astype(np.int16)
        return np.clip(base + self._preroll_noise, 0, 255).astype(np.uint8)

    async def _play_preroll(self, stop: asyncio.Event) -> None:
        """Play the pre-roll until ``stop`` or `preroll_s`, paced at play_fps.

        Emitted outside any step, so the step lock stays free for commands;
        `start` stops this task before it flushes, so no pre-roll frame plays
        after the world's first one.
        """
        loop = asyncio.get_running_loop()
        began = loop.time()
        image = self.state._image
        frames = await asyncio.to_thread(self._preroll_frames, image)
        sent = 0
        logger.info("pre-roll started", seconds=self._preroll_s)
        try:
            while not stop.is_set() and loop.time() - began < self._preroll_s:
                if self.state._image is not image:  # a reference was staged: show it
                    image = self.state._image
                    frames = await asyncio.to_thread(self._preroll_frames, image)
                    if stop.is_set():
                        break
                picks = [(sent + k) % len(frames) for k in range(PREROLL_EMIT)]
                await self.emit(
                    EdenOutput(main_video=frames[picks], draft_video=frames[picks]), drop=True
                )
                sent += PREROLL_EMIT
                wait = began + sent / self._play_fps - loop.time()
                if wait > 0:
                    try:
                        await asyncio.wait_for(stop.wait(), wait)
                    except TimeoutError:
                        pass
        finally:
            logger.info("pre-roll stopped", frames=sent, seconds=round(loop.time() - began, 2))

    async def _stop_preroll(self) -> None:
        task, self._preroll = self._preroll, None
        if task is not None:
            self._preroll_stop.set()
            await asyncio.wait([task], timeout=1.0)

    def _release_keys(self) -> None:
        """Release the held keys and the held look rate."""
        self.state.keys = None
        self.state._keys_holder = None
        self.state._look = NO_LOOK
        self.state._look_holder = None
        if self._draft_source is not None:
            self._draft_source.set_keys(())
            self._draft_source.set_look(*NO_LOOK)

    def _refuse(self, reason: str) -> NoReturn:
        """Refuse the step, logging the reason at info once per change of reason.

        The step loop asks again every few milliseconds while a world is not
        running, so the reason is logged only when it changes; a successful
        step resets it. Raises ``ApplicationError``.
        """
        if reason != self._last_refusal:
            logger.info("step refused", reason=reason)
            self._last_refusal = reason
        raise ApplicationError(reason)

    def _require_paused(self) -> None:
        if not self.state.paused:
            raise CommandError("pause_required", "Pause before changing the game or reference.")

    def _state_update(self) -> StateUpdate:
        state = self.state
        hud = state._hud or {}
        return StateUpdate(
            source=state._source,
            game=state._game,
            image_name=state._image_name,
            prompt=state.prompt,
            seed=state._seed,
            keys=list(state.keys or ()),
            look=list(state._look),
            paused=state.paused,
            started=state._started,
            ended=state._ended,
            game_state=hud.get("game_state", ""),
            outcome=list(hud.get("outcome", ())),
            health=hud.get("health"),
            health_max=hud.get("health_max"),
            events=list(state._events),
            world_id=state._world_id,
            chunks=state._chunks,
            seconds=state._seconds,
        )

    async def _publish(self) -> StateUpdate:
        message = self._state_update()
        await self.send(message)
        return message
