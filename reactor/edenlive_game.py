# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""The native game as a draft source: simulation, capture, and event prompts."""

import asyncio
import contextlib
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future
from pathlib import Path
from threading import Condition

import psutil
from edenlive_draft import Draft, fit_frames
from edenlive_model import CONDITION_STRIDE
from reactor_runtime import get_logger

logger = get_logger(__name__)

# The shipped games report their HUD state here; anything else is an outcome.
_RUNNING_STATES = ("", "RUNNING", "READY")
# No look rate on either axis: the page has its own look only from keys.
NO_LOOK = (0.0, 0.0)

# A chunk captures in about 0.2 s on the GPU, and the first one also launches
# the browser and loads the game, each bounded by Playwright's own 30 s
# timeouts. A capture that has not advanced a frame for this long, counting
# only the time the world was not paused, is a stalled browser.
CAPTURE_TIMEOUT_S = 60.0
# How long the browser gets to close on its own before its processes are
# killed. A clean close takes well under a second.
CLOSE_TIMEOUT_S = 10.0
# Command-line marks of what a game spawns directly under this process: the
# Playwright node driver, which launches Chromium beneath itself.
_DRIVER_MARKS = ("playwright", "run-driver", "chrom", "headless_shell")
# Held while one game starts its Playwright driver, so the new child process
# is attributed to that game even when another game is launching or running.
_DRIVER_START = threading.Lock()
# The thread a closed game finishes its teardown on (`EdenGame.close`).
CLOSE_THREAD = "edenlive-game-close"


def event_lines(chunk) -> tuple[str, ...]:
    """Match the native one-chunk event lifetime and sentence deduplication."""
    indexed = []
    for frame in chunk.frames:
        state = frame.ui_state
        events = state.get("events") if isinstance(state, Mapping) else None
        for event in events if isinstance(events, list) else ():
            if not isinstance(event, Mapping):
                continue
            kind, text, event_id = (
                event.get("type"),
                event.get("prompt"),
                event.get("id"),
            )
            if (
                isinstance(kind, str)
                and kind
                and isinstance(text, str)
                and text.strip()
                and isinstance(event_id, int)
                and not isinstance(event_id, bool)
            ):
                indexed.append((event_id, text.strip()))
    return tuple(dict.fromkeys(text for _, text in sorted(indexed, key=lambda pair: pair[0])))


def reached_outcome(ui_state) -> bool:
    """Whether a frame's HUD state says the game is over (won, lost, ...)."""
    return isinstance(ui_state, Mapping) and ui_state.get("state", "") not in _RUNNING_STATES


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def hud_status(ui_state) -> dict:
    """The player-facing part of a frame's HUD, typed for `state_update`.

    The games share one HUD convention: ``state``, ``health``/``healthMax``, and
    an ``outcome`` card once the game ends (title, reason, advice). A game that
    reports only an ``outcomeReason`` line has it stand in for the card.
    Anything malformed reads as absent.
    """
    if not isinstance(ui_state, Mapping):
        return {}
    state = ui_state.get("state")
    card = ui_state.get("outcome")
    outcome = []
    if isinstance(card, list):
        outcome = [line for line in card if isinstance(line, str) and line]
    reason = ui_state.get("outcomeReason")
    if not outcome and isinstance(reason, str) and reason:
        outcome = [reason]
    return {
        "game_state": state if isinstance(state, str) else "",
        "outcome": outcome,
        "health": _number(ui_state.get("health")),
        "health_max": _number(ui_state.get("healthMax")),
    }


class EdenGame:
    """The native game as a draft source.

    One bounded capture is in flight at a time, with a latest-held-keys mailbox
    sampled once per captured frame. The browser that renders the game lives
    on the capture worker's own thread; nothing here waits on that thread
    without a deadline, so a wedged browser can stall this source but never
    the application that drives it.
    """

    kind = "game"

    def __init__(
        self,
        *,
        source: Path,
        slug: str,
        size: tuple[int, int] = (1280, 704),
        capture_format: str = "jpeg",
        draft_video: str = "full",
    ) -> None:
        integrations = str(source / "realtime/integrations_v2")
        if integrations not in sys.path:
            sys.path.insert(0, integrations)
        from eden.apps.game2v.game_timeline import GameTimeline
        from capture.game_renderer import GameCaptureWorker

        self.slug = slug
        self.timeline = GameTimeline(slug)
        # The Playwright driver this game started, recorded when it starts (see
        # `_start_driver`); its browser runs beneath it.
        self._driver_pids: set[int] = set()
        self.capture = GameCaptureWorker(
            slug,
            width=size[0],
            height=size[1],
            games_root=source / "realtime/games",
            capture_format=capture_format,
            # `sparse`: only the condition frames are read back; the others
            # repeat the last one in the draft and keep their own metadata.
            condition_only=draft_video == "sparse",
            start_driver=self._start_driver,
        )
        self._held = set()
        self._latest = frozenset()
        # Look rates (yaw, pitch), each -1..1: the latest the client holds, and
        # the last one the page was given. The page only hears of a change.
        self._look = NO_LOOK
        self._look_sent = NO_LOOK
        self._gate = Condition()
        self._paused = False
        self._closed = False
        self._pending = None
        self._sampled = []
        self._submitted_at = None
        self._captured_at = None
        # The stall clock: when the capture in flight last advanced (it was
        # submitted, or a frame read its inputs), and when the world paused.
        self._progress_at = None
        self._paused_at = None

    def launch(self) -> Future:
        """Launch the browser and load the game in the background, now.

        Returns at once with a future that completes when the page is loaded.
        The page is left at its opening, untouched, so the first chunk starts
        from exactly where a game launched by that chunk would; it only no
        longer waits for the browser. Without this call the first `prepare`
        launches it.
        """
        started = time.monotonic()
        launched = self.capture.launch()
        launched.add_done_callback(lambda future: self._report_launch(future, started))
        return launched

    def _report_launch(self, future, started: float) -> None:
        seconds = round(time.monotonic() - started, 2)
        error = future.exception()
        if error is not None:
            # The first capture launches again and fails visibly if it still can't.
            logger.warning("game launch failed", game=self.slug, seconds=seconds, error=repr(error))
        else:
            logger.info("game launched ahead", game=self.slug, seconds=seconds)

    def _start_driver(self):
        """Start this game's Playwright driver and record its process.

        Runs on the capture thread. The process-wide lock makes the new child
        of this process this game's driver, whatever other games are doing.
        """
        from playwright.sync_api import sync_playwright

        with _DRIVER_START:
            before = _child_pids()
            playwright = sync_playwright().start()
            self._driver_pids.update(_driver_pids(before))
        return playwright

    def set_keys(self, held: tuple[str, ...]) -> None:
        with self._gate:
            self._latest = frozenset(held)

    def set_look(self, yaw: float, pitch: float) -> None:
        """Hold a look rate, each axis -1..1, until the next call replaces it."""
        with self._gate:
            self._look = (float(yaw), float(pitch))

    def set_paused(self, paused: bool) -> None:
        with self._gate:
            now = time.monotonic()
            if paused and not self._paused:
                self._paused_at = now
            elif not paused and self._paused:
                # The paused span never counts towards a stall: the capture is
                # held in `_drain` on purpose, or would be at its next frame.
                if self._progress_at is not None and self._paused_at is not None:
                    self._progress_at += now - self._paused_at
                self._paused_at = None
            self._paused = paused
            self._gate.notify_all()

    def _drain(self):
        # The capture thread reads plain application-owned values only. It
        # never touches Runtime state, handlers, the model or its caches.
        with self._gate:
            self._gate.wait_for(lambda: not self._paused or self._closed)
            if self._closed:
                raise CaptureClosed("Capture stopped")
            chosen = self._latest
            look = self._look
            self._progress_at = time.monotonic()
        events = [{"action": key, "phase": "up"} for key in sorted(self._held - chosen)] + [
            {"action": key, "phase": "down"} for key in sorted(chosen - self._held)
        ]
        if look != self._look_sent:
            # The page holds the rate and applies it on every game step until
            # the next change, as it does for a held key.
            events.append({"look": list(look)})
            self._look_sent = look
        self._held = set(chosen)
        self._sampled.append(tuple(sorted(chosen)))
        return events

    def prepare(self, frames: int) -> None:
        """Keep at most one capture in flight, including a completed one."""
        if self._pending is None:
            self._sampled = []
            packet = self.timeline.begin_chunk(frames, 24)
            self._submitted_at = time.monotonic()
            self._captured_at = None
            with self._gate:
                self._progress_at = self._submitted_at
            self._pending = self.capture.submit(packet, drain_events=self._drain)
            self._pending.add_done_callback(self._stamp_capture)

    def _stamp_capture(self, _future) -> None:
        self._captured_at = time.monotonic()

    def ready(self) -> bool:
        return self._pending is not None and self._pending.done()

    def stalled(self) -> bool:
        """Whether the capture in flight has not advanced for ``CAPTURE_TIMEOUT_S``.

        The clock runs from the submission or the last frame that read its
        inputs, whichever is later, and stops while the world is paused, so a
        long pause is never mistaken for a wedged browser.
        """
        if self._pending is None or self._pending.done():
            return False
        with self._gate:
            if self._paused or self._progress_at is None:
                return False
            return time.monotonic() - self._progress_at > CAPTURE_TIMEOUT_S

    async def take(self) -> Draft | None:
        """Return the captured chunk once it is ready; every frame keeps its keys.

        Raises:
            CaptureStalled: The capture in flight has not advanced for
                ``CAPTURE_TIMEOUT_S`` while the world ran; the browser is
                wedged and this source will not deliver again.
        """
        if not self.ready():
            if self.stalled():
                raise CaptureStalled(
                    f"The game has not captured a frame in {CAPTURE_TIMEOUT_S:.0f} s"
                )
            return None
        chunk = self._pending.result()
        self._pending = None
        sampled = tuple(self._sampled)
        if len(sampled) != len(chunk.frames):
            raise RuntimeError("Input samples do not match captured frames")
        conditioned = [i for i, frame in enumerate(chunk.frames) if frame.capture_condition]
        if conditioned != list(range(0, len(chunk.frames), CONDITION_STRIDE)):
            raise RuntimeError("Native condition frames are not every fourth draft frame")
        metadata = tuple(
            {
                "source": self.kind,
                "frame_id": frame.game_frame_id,
                "simulation_tick": frame.simulation_tick,
                "keys": list(keys),
            }
            for frame, keys in zip(chunk.frames, sampled, strict=True)
        )
        # The game may render below the draft size; scaling up runs off the loop.
        frames = await asyncio.to_thread(fit_frames, [frame.ui_frame for frame in chunk.frames])
        return Draft(
            frames=frames,
            metadata=metadata,
            events=event_lines(chunk),
            ended=reached_outcome(chunk.frames[-1].ui_state),
            hud=hud_status(chunk.frames[-1].ui_state),
            capture_seconds=None
            if self._captured_at is None
            else self._captured_at - self._submitted_at,
        )

    def close(self) -> None:
        """Stop the game and release its browser without waiting for either.

        A capture held by pause wakes and fails with ``CaptureClosed``. The
        browser then closes on a thread of its own, bounded by
        ``CLOSE_TIMEOUT_S``; one that does not exit in time is killed. The
        caller never blocks on the browser, so a wedged game cannot hold the
        application's event loop or the step lock.
        """
        with self._gate:
            self._closed = True
            self._gate.notify_all()
        self._pending = None
        # The set itself, not a copy: a driver this game is still starting
        # when it is closed is recorded there in time for the kill.
        threading.Thread(
            target=shutdown_capture,
            args=(self.capture, self._driver_pids),
            name=CLOSE_THREAD,
            daemon=True,
        ).start()


class CaptureClosed(Exception):
    """Wake an unfinished native capture when its world is closed."""


class CaptureStalled(RuntimeError):
    """The capture in flight has not advanced for ``CAPTURE_TIMEOUT_S`` while running."""


def shutdown_capture(
    capture,
    driver_pids: set[int],
    *,
    timeout: float = CLOSE_TIMEOUT_S,
    kill: Callable[[Iterable[int]], None] | None = None,
) -> None:
    """Close a capture worker, killing its browser when it does not exit in time.

    ``capture.close()`` runs on a helper thread because it waits on the
    capture thread, and a capture thread blocked inside the browser never
    answers. After ``timeout`` seconds the browser is killed: every process
    under the Playwright drivers in ``driver_pids``, which the caller resolved
    when it closed the game. The driver itself is left running: it reports
    the dead browser to the blocked call, which fails, and then closes on its
    own, whereas a driver killed from outside leaves Playwright's sync bridge
    spinning on a stopped event loop.
    """
    done = threading.Event()
    failure: list[BaseException] = []

    def graceful() -> None:
        try:
            capture.close()
        except Exception as error:  # noqa: BLE001 - reported below, never raised
            failure.append(error)
        finally:
            done.set()

    started = time.monotonic()
    threading.Thread(target=graceful, name="edenlive-game-close-browser", daemon=True).start()
    killed = False
    if not done.wait(timeout):
        driver_pids = set(driver_pids)
        logger.warning(
            "game browser did not close in time; killing it",
            timeout_s=timeout,
            driver_pids=sorted(driver_pids),
        )
        (kill or kill_browser_processes)(driver_pids)
        killed = True
        if not done.wait(timeout):
            logger.error(
                "game capture thread is still blocked after its browser was killed",
                waited_s=round(time.monotonic() - started, 1),
            )
            return
    if failure:
        logger.warning("game browser close failed", error=repr(failure[0]))
    logger.info("game closed", seconds=round(time.monotonic() - started, 2), killed=killed)


def join_closing_games(timeout: float) -> int:
    """Wait up to ``timeout`` seconds for every closed game to finish its teardown.

    Returns how many teardowns are still running: 0 once the browser of every
    closed game has exited or been killed.
    """
    deadline = time.monotonic() + timeout
    for thread in threading.enumerate():
        if thread.name == CLOSE_THREAD:
            thread.join(max(0.0, deadline - time.monotonic()))
    return sum(
        thread.name == CLOSE_THREAD and thread.is_alive() for thread in threading.enumerate()
    )


def browser_drivers() -> set[int]:
    """The Playwright drivers, each with its browser beneath it, running under this process."""
    return _driver_pids(set())


def _child_pids() -> set[int]:
    """Direct children of this process right now."""
    try:
        return {child.pid for child in psutil.Process().children()}
    except psutil.Error:
        return set()


def _driver_pids(children_before: set[int]) -> set[int]:
    """Playwright drivers this process gained since ``children_before``."""
    found = set()
    for pid in _child_pids() - children_before:
        try:
            cmdline = " ".join(psutil.Process(pid).cmdline()).lower()
        except psutil.Error:
            continue
        if any(mark in cmdline for mark in _DRIVER_MARKS):
            found.add(pid)
    return found


def kill_browser_processes(driver_pids: Iterable[int]) -> None:
    """Kill every process under each driver, and wait briefly for them to go."""
    processes = []
    for pid in driver_pids:
        try:
            processes.extend(psutil.Process(pid).children(recursive=True))
        except psutil.Error:
            continue
    for process in processes:
        with contextlib.suppress(psutil.Error):
            process.kill()
    psutil.wait_procs(processes, timeout=5)
