# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""Exercise real Runtime hooks with fake compute and the model's bookkeeping."""

import asyncio
import contextlib
import io
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Condition, Event, Thread
from types import ModuleType, SimpleNamespace

import edenlive_app as edenlive
import edenlive_game
import numpy as np
import pytest
from edenlive_app import EdenLive
from edenlive_draft import (
    CLIENT_DEFAULT_FPS,
    CLIENT_FPS_RANGE,
    CLIENT_FPS_SMOOTHING,
    ClientSource,
    Draft,
    fit_frames,
)
from edenlive_game import (
    CAPTURE_TIMEOUT_S,
    NO_LOOK,
    CaptureClosed,
    CaptureStalled,
    EdenGame,
    event_lines,
    hud_status,
    reached_outcome,
    shutdown_capture,
)
from edenlive_model import (
    CONDITION_STRIDE,
    EdenInput,
    EdenModel,
    MissingAnchor,
    condition_frames,
)
from edenlive_types import EdenState
from PIL import Image
from reactor_runtime import ApplicationError, CommandError, ReadMode, StepOutcome, UploadedFile
from reactor_runtime.core.model import SessionStarted
from reactor_runtime.interface.model.contract import ContractError, ModelContract

pytestmark = pytest.mark.asyncio


class FakeBackend:
    def __init__(self):
        self.resets = 0
        self.steps = []
        self.anchors = []
        self.error = None
        self.banks = []
        self.bank_clears = 0
        self.text_encoded = False

    def reset(self):
        self.resets += 1

    def set_bank(self, prompts, *, wait):
        self.banks.append((tuple(prompts), wait))
        return len(prompts), 0.0

    def join_bank(self):
        pass

    def clear_bank(self):
        self.bank_clears += 1

    def start(self, anchor):
        self.anchors.append(anchor)

    def step(self, index, condition, prompt):
        self.steps.append((index, condition, prompt))
        if self.error:
            raise self.error
        return np.zeros((9 if index == 0 else 12, 704, 1280, 3), np.uint8)


class FakeGame:
    kind = "game"

    def __init__(self, **kwargs):
        self.slug = kwargs.get("slug")
        self.index = 0
        self.keys = []
        self.closed = False
        self.held = ()
        self.pending = None
        self.paused = False
        self.outcome = False
        self.stalled = False
        self.hud = None
        self.events = ()
        self.look = NO_LOOK
        self.launched = False
        self.loaded = Future()
        self.loaded.set_result(None)

    def launch(self):
        self.launched = True
        return self.loaded

    def set_keys(self, keys):
        self.held = keys

    def set_look(self, yaw, pitch):
        self.look = (yaw, pitch)

    def set_paused(self, paused):
        self.paused = paused

    def prepare(self, frames):
        if self.pending is None:
            self.pending = frames

    def ready(self):
        return self.pending is not None

    async def take(self):
        if self.stalled:
            raise CaptureStalled("stalled")
        if not self.ready():
            return None
        frames, self.pending = self.pending, None
        self.keys.append(self.held)
        metadata = tuple(
            {
                "source": "game",
                "frame_id": self.index + i + 1,
                "simulation_tick": (self.index + i + 1) * 2,
                "keys": list(self.held),
            }
            for i in range(frames)
        )
        self.index += frames
        return Draft(
            frames=np.full((frames, 704, 1280, 3), 7, np.uint8),
            metadata=metadata,
            events=self.events,
            ended=self.outcome,
            hud=self.hud,
        )

    def close(self):
        self.closed = True


class FakeTrack:
    """A client `source_video` track holding frames the test pushes."""

    def __init__(self):
        self.frames = []
        self.reads = []
        self.total_received = 0

    def push(self, count, value=5, shape=(720, 1280, 3)):
        for _ in range(count):
            self.frames.append(
                SimpleNamespace(data=np.full(shape, value, np.uint8), pts=len(self.frames) / 24)
            )
        self.total_received += count

    def try_read(self, n, mode=None):
        self.reads.append((n, mode))
        if len(self.frames) < n:
            return None
        latest, self.frames = self.frames[-n:], []
        return latest


async def app(monkeypatch):
    monkeypatch.setattr(edenlive, "EdenGame", FakeGame)
    instance = EdenLive()
    model = EdenModel()
    model._backend = FakeBackend()
    instance._engine = model
    instance.media = SimpleNamespace(source_video=FakeTrack())
    instance._on_loop_ready()
    sent = []
    instance.bind_output(
        broadcast=sent.append, addressed=lambda *args: None, media=lambda chunk: None
    )
    await instance._dispatch_reactor_event(SessionStarted("test"))
    return instance, model, sent


async def ready(monkeypatch):
    instance, model, sent = await app(monkeypatch)
    instance.state._game = "freeze"
    instance.state._image = np.zeros((64, 64, 3), np.uint8)
    instance.state.prompt = "A cavern."
    await instance.start(seed=7)
    return instance, model, sent


async def test_schema_and_runtime_free_model_boundary():
    contract = ModelContract.of(EdenLive)
    assert set(contract.commands) == {
        "set_source",
        "select_game",
        "set_image",
        "start",
        "set_prompt",
        "set_keys",
        "set_look",
        "set_paused",
    }
    for name in ("edenlive_model.py", "edenlive_backend.py", "edenlive_attention.py"):
        assert "reactor_runtime" not in (Path(__file__).parents[1] / name).read_text()


async def test_model_passes_execution_settings_without_loading_runtime(monkeypatch):
    calls = []
    stub = ModuleType("edenlive_backend")
    stub.EdenBackend = lambda **kwargs: calls.append(kwargs) or FakeBackend()
    monkeypatch.setitem(sys.modules, "edenlive_backend", stub)
    model = EdenModel()
    model.load(
        source=Path("/source"),
        weights=Path("/weights"),
        attention_backend="fa4",
        cuda_graph=True,
    )
    assert calls == [
        {
            "source": Path("/source"),
            "weights": Path("/weights"),
            "attention_backend": "fa4",
            "cuda_graph": True,
            "compile_model": False,
        }
    ]
    assert model._chunk_index == 0 and model._world_id is None


async def test_invalid_execution_options_fail_before_asset_loading(tmp_path):
    from edenlive_assets import resolve_assets

    config = tmp_path / "edenlive.yaml"
    config.write_text("attention_backend: sparse\n")
    with pytest.raises(ValueError, match="attention_backend"):
        resolve_assets(config, tmp_path)
    config.write_text('cuda_graph: "yes"\n')
    with pytest.raises(ValueError, match="boolean"):
        resolve_assets(config, tmp_path)
    config.write_text("game_capture: [832]\n")
    with pytest.raises(ValueError, match="game_capture"):
        resolve_assets(config, tmp_path)
    config.write_text("capture_format: png\n")
    with pytest.raises(ValueError, match="capture_format"):
        resolve_assets(config, tmp_path)
    config.write_text("draft_video: half\n")
    with pytest.raises(ValueError, match="draft_video"):
        resolve_assets(config, tmp_path)
    for bad in ("0", "121", "24.5", '"30"'):
        config.write_text(f"play_fps: {bad}\n")
        with pytest.raises(ValueError, match="play_fps"):
            resolve_assets(config, tmp_path)
    for bad in ("-1", "121", '"20"', "true"):
        config.write_text(f"preroll_s: {bad}\n")
        with pytest.raises(ValueError, match="preroll_s"):
            resolve_assets(config, tmp_path)


async def test_fit_frames_scales_a_repeated_frame_once():
    small = np.full((352, 640, 3), 7, np.uint8)
    other = np.full((352, 640, 3), 9, np.uint8)
    fitted = fit_frames([small, small, other, other])
    assert fitted.shape == (4, 704, 1280, 3)
    assert (fitted[0] == fitted[1]).all() and (fitted[2] == fitted[3]).all()
    assert fitted[0, 0, 0, 0] == 7 and fitted[2, 0, 0, 0] == 9


async def test_fa4_dense_layout_is_local_and_rejects_multiple_gpus(monkeypatch):
    from edenlive_attention import install_flash_attention4

    calls = []

    class Tensor:
        def __init__(self, name, transposes=()):
            self.name, self.transposes = name, transposes

        def transpose(self, a, b):
            return Tensor(self.name, self.transposes + ((a, b),))

    class Attention:
        def __init__(self, qkv_format="bshd"):
            self.qkv_format = qkv_format
            self.device_mesh = None

        def is_context_parallel_enabled(self):
            return self.device_mesh is not None

    class MultiHead:
        def __init__(self):
            self.attn_op = Attention()

    def flash(query, key, value, **kwargs):
        calls.append((query, key, value, kwargs))
        return Tensor("output"), None

    for name, attributes in (
        ("flash_attn.cute", {"flash_attn_func": flash}),
        ("flashdreams.core.attention.cp", {"ContextParallelAttention": Attention}),
        (
            "flashdreams.recipes.wan.transformer.impl.modules",
            {"MultiHeadAttention": MultiHead},
        ),
    ):
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    first, second = MultiHead(), MultiHead()
    untouched = second.attn_op
    install_flash_attention4(SimpleNamespace(modules=lambda: (first,)))
    assert second.attn_op is untouched
    output = first.attn_op._impl(Tensor("q"), Tensor("k"), Tensor("v"))
    assert all(t.transposes == ((1, 2),) for t in calls[0][:3])
    assert calls[0][3] == {"causal": False}
    assert output.transposes == ((1, 2),)
    first.attn_op.device_mesh = object()
    with pytest.raises(RuntimeError, match="single-GPU"):
        first.attn_op._impl(Tensor("q"), Tensor("k"), Tensor("v"))


async def test_refused_step_never_calls_model(monkeypatch):
    instance, model, _ = await app(monkeypatch)
    with pytest.raises(ApplicationError):
        await instance.process_input()
    assert not model._backend.steps


async def test_anchor_ack_prompt_and_frame_pairing(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    first = await instance.process_input()
    assert first.anchor.seed == 7
    result = instance.generate(first)
    output = await instance.process_output(StepOutcome(result=result, elapsed=0.5))
    assert output.main_video.shape == output.draft_video.shape == (9, 704, 1280, 3)
    assert np.all(output.draft_video == 7)
    assert np.all(output.main_video == 0)
    assert output.__metadata__["main_video"][0]["frame_id"] == 1
    assert output.__metadata__["draft_video"] == output.__metadata__["main_video"]
    await instance.set_prompt(prompt="The cavern glows blue.")
    second = await instance.process_input()
    assert second.anchor is None and second.world_id == first.world_id
    assert second.prompt == "The cavern glows blue."
    instance.state.prompt = "A later command cannot mutate a captured step."
    instance.generate(second)
    assert model._backend.steps[-1][2] == second.prompt
    assert len(model._backend.anchors) == 1


async def test_set_prompt_with_the_current_text_keeps_the_bank(monkeypatch):
    instance, model, sent = await app(monkeypatch)
    instance.state._game = "freeze"
    instance._bank_events = {"freeze": [["freeze shot."]]}
    await instance.set_prompt(prompt="A cavern.")
    banks = list(model._backend.banks)
    assert banks and banks[-1][1] is True
    published = len(sent)
    reply = await instance.set_prompt(prompt="  A cavern. ")
    assert model._backend.banks == banks
    assert reply.prompt == "A cavern."
    assert len(sent) == published + 1
    await instance.set_prompt(prompt="A frozen cavern.")
    assert len(model._backend.banks) == len(banks) + 1


async def test_failures_do_not_complete_or_reset(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    input = await instance.process_input()
    model._backend.error = RuntimeError("test compute failure")
    with pytest.raises(RuntimeError) as error:
        instance.generate(input)
    with pytest.raises(RuntimeError, match="test compute failure"):
        await instance.process_output(StepOutcome(error=error.value))
    assert instance.state._chunks == model._chunk_index == 0
    assert instance.state._applied_world_id is None
    assert model._backend.resets == 1


async def test_pause_update_resume_and_disconnect(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    await instance.set_keys(keys=["w", "f"])
    await instance.set_paused(paused=True)
    await instance.set_prompt(prompt="Blue ice.")
    with pytest.raises(ApplicationError):
        await instance.process_input()
    await instance.set_paused(paused=False)
    await instance.set_keys(keys=["w"], client=SimpleNamespace(id="player"))
    await instance.on_disconnected(SimpleNamespace(id="viewer"))
    assert instance.state.keys == ["w"]
    await instance.on_disconnected(SimpleNamespace(id="player"))
    assert instance.state._started and instance.state._world_id == 1
    assert not instance.state.keys and model._backend.resets == 0
    await instance.process_input()
    assert instance._draft_source.keys == [()]


async def test_restart_and_session_seed_isolation(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    first_game = instance._draft_source
    await instance.start(seed=11)
    assert first_game.closed
    assert instance.state._world_id == 2
    assert (await instance.process_input()).anchor.seed == 11
    instance.on_session_ended()
    assert model._backend.resets == 1 and instance._draft_source is None
    assert EdenState()._seed == 2026092901


async def test_refused_commands_send_nothing(monkeypatch):
    instance, _, sent = await app(monkeypatch)
    for method, kwargs, code in (
        (instance.start, {"seed": 0}, "game_required"),
        (instance.set_prompt, {"prompt": " "}, "prompt_required"),
        (instance.set_paused, {"paused": False}, "start_required"),
    ):
        before = len(sent)
        with pytest.raises(CommandError) as error:
            await method(**kwargs)
        assert error.value.code == code and len(sent) == before


async def test_upload_stages_without_generation(monkeypatch):
    instance, model, _ = await app(monkeypatch)
    instance.state._game = "freeze"
    data = io.BytesIO()
    Image.new("RGB", (64, 64)).save(data, format="PNG")
    await instance.set_image(
        UploadedFile(name="cover.png", mime_type="image/png", data=data.getvalue())
    )
    assert instance.state._image.shape == (64, 64, 3)
    assert not instance.state._started and not model._backend.steps
    for payload in (b"", b"broken", b"a" * (25 * 1024 * 1024 + 1)):
        with pytest.raises(CommandError) as error:
            await instance.set_image(
                UploadedFile(name="bad.png", mime_type="image/png", data=payload)
            )
        assert error.value.code == "invalid_image"


async def test_model_missing_anchor():
    model = EdenModel()
    model._backend = FakeBackend()
    with pytest.raises(MissingAnchor):
        model.generate(
            EdenInput(
                world_id=1,
                anchor=None,
                prompt="Scene",
                condition=np.zeros((3, 704, 1280, 3), np.uint8),
            )
        )
    assert model._backend.resets == 0


async def test_event_prompt_matches_native_dedup():
    chunk = SimpleNamespace(
        frames=[
            SimpleNamespace(
                ui_state={
                    "events": [
                        {"id": 2, "type": "shot", "prompt": " Blue ice. "},
                        {"id": 1, "type": "move", "prompt": "Moving."},
                        {"id": 3, "type": "shot", "prompt": "Blue ice."},
                    ]
                }
            )
        ]
    )
    assert event_lines(chunk) == ("Moving.", "Blue ice.")


async def test_actual_runtime_loop(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    emitted = []

    async def emit(output, **kwargs):
        emitted.append(output)
        instance._live.clear()

    monkeypatch.setattr(instance, "emit", emit)
    monkeypatch.setattr(instance, "_report_step", lambda *args: None)
    instance._live.set()
    task = asyncio.create_task(instance.run())
    for _ in range(100):
        if emitted:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(emitted) == 1 and len(model._backend.steps) == 1
    assert instance.state._chunks == 1


async def test_latest_keys_replace_unconsumed_transitions(monkeypatch):
    instance, _, _ = await ready(monkeypatch)
    await instance.set_keys(keys=["f"])
    await instance.set_keys(keys=[])
    snapshot = await instance.process_input()
    assert instance._draft_source.keys == [()]
    assert [frame["keys"] for frame in instance.state._draft.metadata] == [[]] * 9
    output = await instance.process_output(StepOutcome(result=instance.generate(snapshot)))
    assert output.__metadata__["main_video"][0]["keys"] == []
    assert instance.state._draft is None


async def test_set_look_schema_takes_two_rates_in_range():
    contract = ModelContract.of(EdenLive)
    command = contract.validate("set_look", {"yaw": -1.0, "pitch": 0.25})
    assert (command.yaw, command.pitch) == (-1.0, 0.25)
    assert contract.validate("set_look", {}).yaw == 0.0
    for bad in ({"yaw": 1.5}, {"pitch": -1.01}, {"yaw": "left"}, {"roll": 0.1}):
        with pytest.raises(ContractError):
            contract.validate("set_look", bad)
    schema = contract.render_schema().commands["set_look"].schema
    assert set(schema) == {"yaw", "pitch"}
    assert "keys" in contract.render_schema().commands["set_keys"].schema


async def test_look_is_held_until_replaced_and_reset_with_the_keys(monkeypatch):
    instance, _, sent = await ready(monkeypatch)
    assert sent[-1].look == [0.0, 0.0]
    await instance.set_look(yaw=0.5, pitch=-0.25, client=SimpleNamespace(id="player"))
    assert instance._draft_source.look == (0.5, -0.25) and sent[-1].look == [0.5, -0.25]
    # Keys and look are independent: one replaces neither.
    await instance.set_keys(keys=["w"])
    assert instance._draft_source.look == (0.5, -0.25) and sent[-1].keys == ["w"]
    await instance.on_disconnected(SimpleNamespace(id="viewer"))
    assert instance.state._look == (0.5, -0.25)
    await instance.on_disconnected(SimpleNamespace(id="player"))
    assert instance._draft_source.look == NO_LOOK and sent[-1].look == [0.0, 0.0]

    await instance.set_look(yaw=1.0, pitch=0.0)
    await instance.set_paused(paused=True)
    assert instance._draft_source.look == NO_LOOK and sent[-1].look == [0.0, 0.0]
    await instance.set_look(yaw=1.0, pitch=1.0)  # ignored while paused
    assert instance._draft_source.look == NO_LOOK and sent[-1].look == [0.0, 0.0]
    await instance.set_paused(paused=False)
    await instance.set_look(yaw=-0.3, pitch=0.0)
    await instance.start(seed=7)
    assert instance.state._look == NO_LOOK and sent[-1].look == [0.0, 0.0]
    await instance.set_look(yaw=-0.3, pitch=0.0)
    game = instance._draft_source
    instance.on_session_ended()
    assert instance.state._look == NO_LOOK and game.closed


async def test_look_reaches_the_page_only_when_it_changes():
    game = bare_game()
    assert game._drain() == []
    game.set_look(1, 0)
    game.set_keys(("w",))
    assert game._drain() == [{"action": "w", "phase": "down"}, {"look": [1.0, 0.0]}]
    assert game._drain() == []
    game.set_look(1, 0)
    assert game._drain() == []
    game.set_look(0, 0)
    assert game._drain() == [{"look": [0.0, 0.0]}]
    assert game._sampled == [(), ("w",), ("w",), ("w",), ("w",)]


async def test_terminal_game_pauses_until_fresh_start(monkeypatch):
    instance, _, _ = await ready(monkeypatch)
    instance._draft_source.outcome = True
    snapshot = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(snapshot)))
    assert instance.state.paused and instance.state._ended
    with pytest.raises(ApplicationError):
        await instance.process_input()
    with pytest.raises(CommandError) as error:
        await instance.set_paused(paused=False)
    assert error.value.code == "game_ended"
    await instance.start(seed=7)
    assert not instance.state._ended


async def test_actual_runtime_refusal_loop(monkeypatch):
    instance, model, _ = await app(monkeypatch)
    attempts = []
    original = instance.process_input

    async def observe():
        attempts.append(True)
        return await original()

    monkeypatch.setattr(instance, "process_input", observe)
    monkeypatch.setattr(instance, "_report_step", lambda *args: None)
    instance._live.set()
    task = asyncio.create_task(instance.run())
    for _ in range(100):
        if attempts:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts and not model._backend.steps


async def test_actual_runtime_failure_loop(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    model._backend.error = RuntimeError("inference failed")
    monkeypatch.setattr(instance, "_report_step", lambda *args: None)
    instance._live.set()
    with pytest.raises(RuntimeError, match="inference failed"):
        await asyncio.wait_for(instance.run(), timeout=5)
    assert instance.state._chunks == 0
    assert instance.state._applied_world_id is None


def bare_game():
    game = EdenGame.__new__(EdenGame)
    game._held, game._latest, game._sampled = set(), frozenset(), []
    game._look = game._look_sent = NO_LOOK
    game._gate, game._paused, game._closed = Condition(), False, False
    game._pending = None
    game._submitted_at = game._captured_at = None
    game._progress_at = game._paused_at = None
    game.slug, game._driver_pids = "freeze", set()
    game.timeline = SimpleNamespace(begin_chunk=lambda frames, fps: frames)
    return game


async def test_capture_samples_latest_state_per_frame_without_replaying_taps():
    game = bare_game()
    delivered = []

    def submit(count, drain_events):
        packets = []
        for i in range(count):
            if i == 1:
                game.set_keys(("w",))
                game.set_keys(("a",))
                game.set_keys(())
            if i == 2:
                game.set_keys(("f",))
            delivered.append(drain_events())
            pixels = np.zeros((2, 2, 3), np.uint8)
            packets.append(
                SimpleNamespace(
                    ui_frame=pixels,
                    condition_frame=pixels,
                    capture_condition=i in (0, 4, 8),
                    ui_state={"state": "WON"} if i == count - 1 else {},
                    game_frame_id=i + 1,
                    simulation_tick=2 * (i + 1),
                )
            )
        future = Future()
        future.set_result(SimpleNamespace(frames=packets))
        return future

    game.capture = SimpleNamespace(submit=submit)
    game.prepare(9)
    game.prepare(12)  # Completed captures also occupy the single slot.
    assert len(delivered) == 9
    draft = await game.take()
    assert [frame["keys"] for frame in draft.metadata] == [[], []] + [["f"]] * 7
    assert draft.frames.shape == (9, 704, 1280, 3) and draft.ended
    assert delivered[0] == []
    assert delivered[1] == []
    assert delivered[2] == [{"action": "f", "phase": "down"}]
    assert game._held == {"f"}

    game._held = {"w"}
    game.set_keys(("f",))
    assert game._drain() == [
        {"action": "w", "phase": "up"},
        {"action": "f", "phase": "down"},
    ]
    assert game._held == {"f"}


async def test_capture_wait_refuses_without_holding_runtime_lock(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    monkeypatch.setattr(instance._draft_source, "ready", lambda: False)
    with pytest.raises(ApplicationError, match="draft frames"):
        await instance.process_input()
    await instance.set_keys(keys=["w"])
    await instance.set_keys(keys=["d"])
    assert instance._draft_source.held == ("d",)
    assert model._backend.steps == []


async def test_capture_waits_for_the_step_and_keeps_pairing(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    first = await instance.process_input()
    assert instance._draft_source.pending is None
    await instance.process_output(StepOutcome(result=instance.generate(first)))
    for chunk in range(2, 13):
        snapshot = await instance.process_input()
        # Nothing is captured ahead, so keys set between steps reach the next chunk.
        assert instance._draft_source.pending is None
        result = instance.generate(snapshot)
        output = await instance.process_output(StepOutcome(result=result))
        metadata = output.__metadata__["main_video"]
        assert metadata[0]["frame_id"] == 10 + (chunk - 2) * 12
        assert metadata[-1]["frame_id"] == 9 + (chunk - 1) * 12
        assert output.main_video.shape[0] == 12
    assert len(model._backend.steps) == 12 and len(model._backend.anchors) == 1


async def test_pause_holds_capture_and_close_wakes_it():
    game = bare_game()
    game.set_paused(True)
    entered = Event()

    def drain():
        entered.set()
        return game._drain()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(drain)
        assert entered.wait(1) and not pending.done()
        game.set_keys(("w",))
        game.set_paused(False)
        assert pending.result(1) == [{"action": "w", "phase": "down"}]
        game.set_paused(True)
        entered.clear()
        pending = pool.submit(drain)
        assert entered.wait(1) and not pending.done()
        game.capture = SimpleNamespace(close=lambda: None)
        game.close()
        with pytest.raises(CaptureClosed):
            pending.result(1)


async def test_capture_failure_is_not_hidden():
    game = bare_game()
    game._pending = Future()
    game._pending.set_exception(RuntimeError("capture failed"))
    with pytest.raises(RuntimeError, match="capture failed"):
        await game.take()


async def test_capture_past_the_deadline_is_a_stall_not_a_wait():
    game = bare_game()
    game._pending = Future()  # in flight, never completes
    game._progress_at = time.monotonic()
    assert await game.take() is None and not game.stalled()
    game._progress_at = time.monotonic() - CAPTURE_TIMEOUT_S - 1
    assert game.stalled()
    with pytest.raises(CaptureStalled, match="has not captured"):
        await game.take()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def threaded_capture(game, *, hold_at, release):
    """A capture thread that drains every frame and waits on `release` before frame `hold_at`."""
    reached = Event()

    def submit(count, drain_events):
        future = Future()

        def run():
            packets = []
            for i in range(count):
                if i == hold_at:
                    reached.set()
                    release.wait(5)
                drain_events()
                pixels = np.zeros((2, 2, 3), np.uint8)
                packets.append(
                    SimpleNamespace(
                        ui_frame=pixels,
                        condition_frame=pixels,
                        capture_condition=i % 4 == 0,
                        ui_state={},
                        game_frame_id=i + 1,
                        simulation_tick=2 * (i + 1),
                    )
                )
            future.set_result(SimpleNamespace(frames=packets))

        Thread(target=run, daemon=True).start()
        return future

    game.capture = SimpleNamespace(submit=submit)
    return reached


async def test_a_long_pause_mid_capture_resumes_without_a_stall(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(edenlive_game, "time", clock)
    game = bare_game()
    release = Event()
    reached = threaded_capture(game, hold_at=3, release=release)
    game.prepare(9)
    assert reached.wait(1)
    # Paused between frames: the capture thread then blocks in the next drain.
    game.set_paused(True)
    release.set()
    clock.now += CAPTURE_TIMEOUT_S + 30
    assert not game.stalled()
    assert await game.take() is None
    game.set_paused(False)
    # Before the capture thread has woken, the pause still does not count.
    assert not game.stalled()
    for _ in range(200):
        if game.ready():
            break
        await asyncio.sleep(0.01)
    draft = await game.take()
    assert draft is not None and draft.frames.shape[0] == 9


async def test_a_capture_that_stops_advancing_while_running_is_a_stall(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(edenlive_game, "time", clock)
    game = bare_game()
    release = Event()
    reached = threaded_capture(game, hold_at=3, release=release)
    game.prepare(9)
    assert reached.wait(1)
    # Three frames advanced the clock; the browser then wedges with the world running.
    clock.now += CAPTURE_TIMEOUT_S - 1
    assert not game.stalled()
    # A pause and resume in between leaves the time already run on the clock.
    game.set_paused(True)
    clock.now += 500
    game.set_paused(False)
    assert not game.stalled()
    clock.now += 2
    assert game.stalled()
    with pytest.raises(CaptureStalled, match="has not captured"):
        await game.take()
    release.set()


async def test_game_close_never_waits_for_the_browser(monkeypatch):
    game = bare_game()
    game._driver_pids = {4242}
    release = Event()
    game.capture = SimpleNamespace(close=lambda: release.wait(5))
    handed_off = []
    monkeypatch.setattr(
        edenlive_game, "shutdown_capture", lambda *args, **kwargs: handed_off.append(args)
    )
    started = time.monotonic()
    game.close()
    assert time.monotonic() - started < 0.5
    assert game._closed and game._pending is None
    for _ in range(100):
        if handed_off:
            break
        await asyncio.sleep(0.01)
    assert handed_off == [(game.capture, {4242})]
    release.set()


async def test_each_game_records_its_own_driver_as_it_starts(monkeypatch):
    # Two games start their drivers at the same time, as a standby game does
    # while another plays: each records only the child that appeared while it
    # held the start lock, never the other's.
    children = {1}
    pids = iter([2, 3])
    driver = ["/opt/venv/lib/playwright/driver/node", "cli.js", "run-driver"]
    cmdlines = {1: ["python", "worker"], 2: driver, 3: driver}
    monkeypatch.setattr(edenlive_game, "_child_pids", lambda: set(children))
    monkeypatch.setattr(
        edenlive_game.psutil, "Process", lambda pid: SimpleNamespace(cmdline=lambda: cmdlines[pid])
    )

    class Starter:
        def start(self):
            time.sleep(0.02)
            children.add(next(pids))
            time.sleep(0.02)
            return object()

    stub = ModuleType("playwright.sync_api")
    stub.sync_playwright = Starter
    monkeypatch.setitem(sys.modules, "playwright.sync_api", stub)
    first, second = bare_game(), bare_game()
    threads = [Thread(target=game._start_driver) for game in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted([first._driver_pids, second._driver_pids], key=min) == [{2}, {3}]


async def test_a_driver_recorded_after_close_is_still_killed(monkeypatch):
    # A game closed while its launch is still starting the driver: the driver
    # is recorded after close() returned, and the kill still reaches it.
    game = bare_game()
    release = Event()
    game.capture = SimpleNamespace(close=lambda: release.wait(5))
    killed = []
    real = edenlive_game.shutdown_capture

    def kill(pids):
        killed.append(set(pids))
        release.set()

    monkeypatch.setattr(
        edenlive_game,
        "shutdown_capture",
        lambda capture, pids: real(capture, pids, timeout=0.3, kill=kill),
    )
    game.close()
    game._driver_pids.add(4242)
    for _ in range(200):
        if killed:
            break
        await asyncio.sleep(0.01)
    assert killed == [{4242}]


async def test_a_launch_runs_on_the_capture_thread_before_the_first_capture():
    from capture.game_renderer import GameCaptureWorker

    order = []

    class Renderer:
        last_evaluate_s = last_decode_s = 0.0
        webgl_renderer = "fake"

        def __init__(self, slug, **kwargs):
            order.append(("launch", kwargs.get("start_driver")))

        def render_chunk(self, chunk, **kwargs):
            order.append(("render", chunk))
            return chunk

        def close(self):
            order.append(("close", None))

    import capture.game_renderer as renderer_module

    original = renderer_module.GameRenderer
    renderer_module.GameRenderer = Renderer
    try:
        start = object()
        worker = GameCaptureWorker("freeze", start_driver=start)
        worker.launch().result()
        assert worker.submit("chunk").result() == "chunk"
        worker.close()
    finally:
        renderer_module.GameRenderer = original
    # One browser, launched ahead with the game's driver starter, then captured.
    assert order == [("launch", start), ("render", "chunk"), ("close", None)]


async def test_shutdown_kills_a_browser_that_does_not_close_in_time():
    release = Event()
    killed = []

    def kill(pids):
        killed.append(set(pids))
        release.set()  # the blocked Playwright call fails once its browser dies

    started = time.monotonic()
    await asyncio.to_thread(
        shutdown_capture,
        SimpleNamespace(close=lambda: release.wait(5)),
        {4242},
        timeout=0.2,
        kill=kill,
    )
    assert killed == [{4242}]
    assert time.monotonic() - started < 2


async def test_shutdown_leaves_a_browser_that_closes_alone():
    killed = []
    capture = SimpleNamespace(close=lambda: None)
    await asyncio.to_thread(shutdown_capture, capture, {4242}, timeout=0.2, kill=killed.append)
    assert killed == []


async def test_driver_pids_are_the_new_children_that_look_like_a_browser(monkeypatch):
    cmdlines = {
        1: ["/opt/venv/bin/python", "-c", "worker"],
        2: ["/opt/venv/lib/playwright/driver/node", "cli.js", "run-driver"],
        3: ["/root/.cache/ms-playwright/chrome-headless-shell", "--headless"],
        4: ["ffmpeg", "-i", "pipe:0"],
    }

    class Process:
        def __init__(self, pid):
            if pid not in cmdlines:
                raise edenlive_game.psutil.NoSuchProcess(pid)
            self.pid = pid

        def cmdline(self):
            return cmdlines[self.pid]

    monkeypatch.setattr(edenlive_game, "_child_pids", lambda: {1, 2, 3, 4, 5})
    monkeypatch.setattr(edenlive_game.psutil, "Process", Process)
    assert edenlive_game._driver_pids({1}) == {2, 3}
    assert edenlive_game._driver_pids({1, 2, 3}) == set()


async def test_stalled_game_is_replaced_and_the_step_refused(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    first = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(first)))
    stalled = instance._draft_source
    stalled.stalled = True
    with pytest.raises(ApplicationError, match="stalled"):
        await instance.process_input()
    assert stalled.closed
    fresh = instance._draft_source
    assert isinstance(fresh, FakeGame) and fresh is not stalled and not fresh.closed
    assert instance.state._started and instance.state._world_id == 1
    step = await instance.process_input()
    assert step.anchor is None and step.world_id == first.world_id
    assert len(model._backend.anchors) == 1


async def test_step_refusal_reason_is_logged_once_per_change(monkeypatch):
    instance, _, _ = await app(monkeypatch)
    records = []

    class Logger:
        def info(self, event, **fields):
            records.append((event, fields))

        def __getattr__(self, level):
            return lambda event, **fields: None

    monkeypatch.setattr(edenlive, "logger", Logger())
    for _ in range(3):
        with pytest.raises(ApplicationError):
            await instance.process_input()
    refused = [fields for event, fields in records if event == "step refused"]
    assert len(refused) == 1 and "Press Start" in refused[0]["reason"]
    instance.state._game = "freeze"
    instance.state._image = np.zeros((64, 64, 3), np.uint8)
    instance.state.prompt = "A cavern."
    await instance.start(seed=7)
    await instance.process_input()
    await instance.set_paused(paused=True)
    with pytest.raises(ApplicationError):
        await instance.process_input()
    assert len([1 for event, _ in records if event == "step refused"]) == 2
    assert [fields["command"] for event, fields in records if event == "command"] == [
        "start",
        "set_paused",
    ]


async def test_game_refuses_a_condition_layout_the_shared_slice_would_miss():
    game = bare_game()
    game._sampled = [()] * 9
    future = Future()
    frame = SimpleNamespace(
        ui_frame=np.zeros((2, 2, 3), np.uint8), ui_state={}, game_frame_id=1, simulation_tick=1
    )
    frames = [SimpleNamespace(**vars(frame), capture_condition=i in (1, 5, 9)) for i in range(9)]
    future.set_result(SimpleNamespace(frames=frames))
    game._pending = future
    with pytest.raises(RuntimeError, match="every fourth"):
        await game.take()


async def test_hud_outcome_detection():
    assert not reached_outcome({"state": "RUNNING"})
    assert not reached_outcome({"roundId": 0})
    assert not reached_outcome(None)
    assert reached_outcome({"state": "LOST"})


async def test_hud_status_reads_the_shared_hud_convention():
    card = ["TRY AGAIN", "The spiders got you.", "Keep your distance."]
    assert hud_status({"state": "LOST", "health": 0, "healthMax": 100, "outcome": card}) == {
        "game_state": "LOST",
        "outcome": card,
        "health": 0.0,
        "health_max": 100.0,
    }
    assert hud_status({"state": "RUNNING", "health": None, "healthMax": None, "outcome": None}) == {
        "game_state": "RUNNING",
        "outcome": [],
        "health": None,
        "health_max": None,
    }
    # A reason line stands in for a missing card; malformed values read as absent.
    assert hud_status({"state": 3, "health": True, "outcomeReason": "Out of time."}) == {
        "game_state": "",
        "outcome": ["Out of time."],
        "health": None,
        "health_max": None,
    }
    assert hud_status(None) == {}


async def test_state_update_carries_the_hud_and_the_chunk_events(monkeypatch):
    instance, _, sent = await ready(monkeypatch)
    assert sent[-1].game_state == "" and sent[-1].health is None and sent[-1].events == []
    game = instance._draft_source
    game.hud = {"game_state": "RUNNING", "outcome": [], "health": 84.0, "health_max": 100.0}
    game.events = ("A spider lunges and bites the player.",)
    snapshot = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(snapshot)))
    update = sent[-1]
    assert (update.game_state, update.health, update.health_max) == ("RUNNING", 84.0, 100.0)
    assert update.events == ["A spider lunges and bites the player."] and update.chunks == 1
    # A command between chunks repeats the latest chunk's events.
    await instance.set_keys(keys=["w"])
    assert sent[-1].events == update.events and sent[-1].chunks == 1

    card = ["TRY AGAIN", "The spiders got you.", "Keep your distance."]
    game.hud = {"game_state": "LOST", "outcome": card, "health": 0.0, "health_max": 100.0}
    game.events = ()
    game.outcome = True
    snapshot = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(snapshot)))
    update = sent[-1]
    assert update.ended and update.paused and update.game_state == "LOST"
    assert update.outcome == card and update.health == 0.0 and update.events == []

    await instance.start(seed=7)
    update = sent[-1]
    assert (update.game_state, update.outcome, update.health, update.events) == ("", [], None, [])


async def test_condition_frames_are_the_shared_stride():
    draft = np.arange(12)[:, None, None, None] * np.ones((12, 2, 2, 3), np.uint8)
    assert CONDITION_STRIDE == 4
    assert [int(f[0, 0, 0]) for f in condition_frames(draft)] == [0, 4, 8]
    assert len(condition_frames(draft[:9])) == 3


async def test_fit_frames_cover_crops_any_client_geometry():
    tall = np.zeros((1080, 720, 3), np.uint8)
    tall[540, 360] = 255
    wide = np.full((480, 1920, 3), 9, np.uint8)
    exact = np.full((704, 1280, 3), 3, np.uint8)
    fitted = fit_frames([tall, wide, exact])
    assert fitted.shape == (3, 704, 1280, 3) and fitted.dtype == np.uint8
    assert np.all(fitted[1] == 9) and np.all(fitted[2] == 3)


async def test_client_source_reads_latest_frames_and_never_ends():
    track = FakeTrack()
    source = ClientSource(track)
    assert await source.take() is None
    source.prepare(9)
    track.push(5)
    assert await source.take() is None
    track.push(20, value=8, shape=(480, 640, 3))
    draft = await source.take()
    assert draft.frames.shape == (9, 704, 1280, 3) and np.all(draft.frames == 8)
    assert draft.metadata[0]["source"] == "client" and not draft.ended and draft.events == ()
    assert track.reads[-1][1] is ReadMode.LATEST


async def client_ready(monkeypatch):
    instance, model, sent = await app(monkeypatch)
    await instance.set_source(source="client")
    data = io.BytesIO()
    Image.new("RGB", (64, 64)).save(data, format="PNG")
    await instance.set_image(
        UploadedFile(name="look.png", mime_type="image/png", data=data.getvalue())
    )
    await instance.set_prompt(prompt="A neon city.")
    await instance.start(seed=3)
    return instance, model, sent


async def test_client_source_goes_through_the_same_step(monkeypatch):
    instance, model, sent = await client_ready(monkeypatch)
    track = instance.media.source_video
    with pytest.raises(ApplicationError, match="from the client"):
        await instance.process_input()
    track.push(9, value=40)
    first = await instance.process_input()
    assert first.anchor.seed == 3 and first.prompt == "A neon city."
    assert np.all(first.condition == 40) and first.condition.shape == (3, 704, 1280, 3)
    output = await instance.process_output(StepOutcome(result=instance.generate(first)))
    assert np.all(output.draft_video == 40)
    assert output.__metadata__["main_video"][0]["source"] == "client"
    assert sent[-1].source == "client" and sent[-1].chunks == 1
    track.push(12, value=41)
    second = await instance.process_input()
    assert second.anchor is None and np.all(second.condition == 41)


async def test_switching_source_mid_world_keeps_the_world(monkeypatch):
    instance, model, _ = await ready(monkeypatch)
    first = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(first)))
    game = instance._draft_source
    await instance.set_source(source="client")
    assert game.closed and isinstance(instance._draft_source, ClientSource)
    instance.media.source_video.push(12, value=50)
    step = await instance.process_input()
    assert step.anchor is None and step.world_id == first.world_id
    assert np.all(step.condition == 50)
    await instance.process_output(StepOutcome(result=instance.generate(step)))
    await instance.set_source(source="game")
    assert isinstance(instance._draft_source, FakeGame) and instance._draft_source is not game
    step = await instance.process_input()
    assert step.anchor is None and np.all(step.condition == 7)
    assert len(model._backend.anchors) == 1 and model._backend.resets == 1


class SteppedClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def send(track, clock, frames, fps):
    """The client sends ``frames`` at ``fps``: they arrive and the clock moves on."""
    track.push(frames)
    clock.now += frames / fps


async def test_client_rate_is_the_measured_arrival_rate():
    track, clock = FakeTrack(), SteppedClock()
    track.push(30)  # frames that arrived before the source opened do not count
    source = ClientSource(track, clock=clock)
    source.prepare(12)
    # Too short a span to measure: the default rate, and the span goes on.
    track.push(3)
    clock.now += 0.1
    assert (await source.take()).fps == CLIENT_DEFAULT_FPS
    send(track, clock, 12, 40)
    assert (await source.take()).fps == pytest.approx(15 / 0.4)
    send(track, clock, 12, 30)
    assert (await source.take()).fps == pytest.approx(37.5 + CLIENT_FPS_SMOOTHING * (30 - 37.5))
    # Rates are clamped to the range.
    for frames, fps, clamped in ((30, 100, CLIENT_FPS_RANGE[1]), (9, 4, CLIENT_FPS_RANGE[0])):
        source = ClientSource(track, clock=clock)
        source.prepare(9)
        send(track, clock, frames, fps)
        assert (await source.take()).fps == clamped
    # A reset track starts a new measurement.
    source = ClientSource(track, clock=clock)
    source.prepare(9)
    track.total_received = 0
    send(track, clock, 9, 4)
    assert (await source.take()).fps == CLIENT_DEFAULT_FPS


async def test_client_chunks_play_at_the_client_rate_and_game_chunks_at_play_fps(monkeypatch):
    instance, _, _ = await ready(monkeypatch)
    play_fps = instance._play_fps
    first = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(first)))
    # A game chunk never touches the live rate.
    assert "fps" not in instance.__dict__ and instance.output.fps == play_fps
    clock = SteppedClock()
    monkeypatch.setattr(edenlive, "ClientSource", lambda track: ClientSource(track, clock=clock))
    await instance.set_source(source="client")
    send(instance.media.source_video, clock, 12, 30)
    step = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(step)))
    assert instance.output.fps == pytest.approx(30)
    await instance.set_source(source="game")
    step = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(step)))
    assert instance.output.fps == play_fps


async def test_switching_away_from_an_ended_game_allows_resume(monkeypatch):
    instance, _, _ = await ready(monkeypatch)
    instance._draft_source.outcome = True
    snapshot = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(snapshot)))
    assert instance.state._ended
    await instance.set_source(source="client")
    await instance.set_paused(paused=False)
    assert not instance.state._ended and not instance.state.paused


async def test_source_refusals(monkeypatch):
    instance, _, sent = await app(monkeypatch)
    await instance.set_source(source="client")
    with pytest.raises(CommandError) as error:
        await instance.start(seed=0)
    assert error.value.code == "image_required"
    instance, _, _ = await client_ready(monkeypatch)
    with pytest.raises(CommandError) as error:
        await instance.set_source(source="game")
    assert error.value.code == "game_required"
    assert instance.state._source == "client"


async def test_warmup_covers_capture_recapture_and_a_fresh_world(monkeypatch):
    backend = FakeBackend()
    backend.capture_index, backend.graph_warmup = 6, 2
    stub = ModuleType("edenlive_backend")
    stub.EdenBackend = lambda **kwargs: backend
    monkeypatch.setitem(sys.modules, "edenlive_backend", stub)
    model = EdenModel()
    model.load(source=Path("/source"), weights=Path("/weights"), warmup=True)
    steady, recapture = 6 + 2 + 2, 2 + 2
    # Two one-chunk worlds: the first chunk's cache commit is captured on its third call.
    assert [index for index, _, _ in backend.steps] == [
        *range(steady + recapture),
        0,
        0,
        *range(steady),
    ]
    prompts = [prompt for _, _, prompt in backend.steps[: steady + recapture]]
    assert len(set(prompts[:steady])) == 1 and prompts[steady] != prompts[0]
    assert [anchor.seed for anchor in backend.anchors] == [0, 1, 2, 3]
    assert backend.resets == 5
    assert model._world_id is None and model._chunk_index == 0


async def test_a_standby_game_is_launched_ahead_and_taken_by_start(monkeypatch, tmp_path):
    instance, _, _ = await app(monkeypatch)
    for game in ("freeze", "dust2"):
        (tmp_path / "realtime/games" / game).mkdir(parents=True)
        Image.new("RGB", (64, 64)).save(tmp_path / "realtime/games" / game / "cover.png")
    instance._root = tmp_path
    instance._entries = {
        game: {"cover": "cover.png", "prompt": f"{game} prompt."} for game in ("freeze", "dust2")
    }
    await instance.set_paused(paused=True)
    await instance.select_game(game="freeze")
    standby = instance._standby
    assert standby.slug == "freeze" and standby.launched and instance._draft_source is None
    # Another game replaces it; selecting that game again keeps the new one.
    await instance.select_game(game="dust2")
    assert standby.closed and instance._standby.slug == "dust2"
    standby = instance._standby
    await instance.select_game(game="dust2")
    assert instance._standby is standby and not standby.closed
    await instance.start(seed=1)
    assert instance._draft_source is standby and instance._standby is None
    # The next one launches after a chunk, not with the world's start.
    step = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(step)))
    following = instance._standby
    assert following is not standby and following.launched and following.slug == "dust2"
    # Start again (same game): the world takes the standby launched during play.
    await instance.start(seed=2)
    assert instance._draft_source is following and standby.closed
    step = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(step)))
    # The client source wants no game: switching to it closes the standby.
    spare = instance._standby
    await instance.set_source(source="client")
    assert spare.closed and instance._standby is None
    await instance.set_source(source="game")
    step = await instance.process_input()
    await instance.process_output(StepOutcome(result=instance.generate(step)))
    # The session's end closes both the game and its standby.
    playing, spare = instance._draft_source, instance._standby
    instance.on_session_ended()
    assert playing.closed and spare.closed
    assert instance._draft_source is None and instance._standby is None


async def test_select_game_replies_once_the_game_is_loaded(monkeypatch, tmp_path):
    instance, _, _ = await app(monkeypatch)
    (tmp_path / "realtime/games/freeze").mkdir(parents=True)
    Image.new("RGB", (64, 64)).save(tmp_path / "realtime/games/freeze/cover.png")
    instance._root = tmp_path
    instance._entries = {"freeze": {"cover": "cover.png", "prompt": "freeze prompt."}}
    loading = Future()
    real_new_game = instance._new_game

    def new_game():
        game = real_new_game()
        game.loaded = loading
        return game

    instance._new_game = new_game
    await instance.set_paused(paused=True)
    reply = asyncio.ensure_future(instance.select_game(game="freeze"))
    await asyncio.sleep(0.05)
    assert instance._standby.launched and not reply.done()
    # A failed launch still replies: the first capture launches again and reports it.
    loading.set_exception(RuntimeError("no browser"))
    await asyncio.wait_for(reply, 1)
    assert instance.state._game == "freeze"


def unloaded_app(monkeypatch, tmp_path):
    """An application as `load()` leaves it before the runtime binds it, on a fake model."""
    created = []

    class Game(FakeGame):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.append(self)

    monkeypatch.setattr(edenlive, "EdenGame", Game)
    closing = []
    monkeypatch.setattr(
        edenlive, "join_closing_games", lambda timeout: closing.append(timeout) or 0
    )
    monkeypatch.setattr(edenlive, "browser_drivers", lambda: set())
    instance = EdenLive()
    model = EdenModel()
    model._backend = FakeBackend()
    instance._engine = model
    instance.media = SimpleNamespace(source_video=FakeTrack())
    for game in edenlive.GAMES:
        (tmp_path / "realtime/games" / game).mkdir(parents=True)
        Image.new("RGB", (64, 64)).save(tmp_path / "realtime/games" / game / "cover.png")
    instance._root = tmp_path
    instance._entries = {
        game: {"cover": "cover.png", "prompt": f"{game} prompt."} for game in edenlive.GAMES
    }
    instance._bank_events = {game: [[f"{game} shot."]] for game in edenlive.GAMES}
    return instance, model, created, closing


async def test_warm_up_plays_every_game_and_leaves_the_app_as_a_session_end_does(
    monkeypatch, tmp_path
):
    instance, model, created, closing = unloaded_app(monkeypatch, tmp_path)
    bank_events = instance._bank_events
    await instance._warm_up()
    backend = model._backend
    games = edenlive.GAMES
    # Every game is selected, started from its cover and opening prompt, and
    # plays its first chunk and its first 12-frame chunk.
    assert [anchor.prompt for anchor in backend.anchors] == [f"{game} prompt." for game in games]
    assert [index for index, _, _ in backend.steps] == [0, 1] * len(games)
    assert {game.slug for game in created} == set(games)
    # Only the first game builds its bank; the session's end drops it.
    assert backend.banks[0] == (("freeze prompt. freeze shot.",), True)
    assert all(prompts == () for prompts, _ in backend.banks[1:])
    assert backend.bank_clears >= 1 and instance._bank_events is bank_events
    # Nothing is left: every game closed and waited for, no standby, no world,
    # no session state, and the model holds no rollout.
    assert all(game.closed for game in created) and closing
    assert instance._draft_source is None and instance._standby is None
    assert instance._standby_launched is None and instance.state is None
    assert model._world_id is None and model._chunk_index == 0
    fresh = EdenLive()
    for name in ("_last_prompt", "_last_refusal"):
        assert getattr(instance, name) == getattr(fresh, name)
    assert vars(instance._timer) == vars(fresh._timer)
    # The first real session then starts exactly as on a fresh application.
    instance._on_loop_ready()
    instance.bind_output(broadcast=lambda m: None, addressed=lambda *a: None, media=lambda c: None)
    await instance._dispatch_reactor_event(SessionStarted("first"))
    state = instance._state_update()
    assert (state.game, state.started, state.paused, state.world_id, state.chunks) == (
        "",
        False,
        True,
        0,
        0,
    )
    assert instance._sessions_started == 1


async def test_a_failed_warm_up_still_ends_its_session(monkeypatch, tmp_path):
    instance, model, created, _ = unloaded_app(monkeypatch, tmp_path)
    bank_events = instance._bank_events
    model._backend.error = RuntimeError("no GPU")
    with pytest.raises(RuntimeError, match="no GPU"):
        await instance._warm_up()
    assert created and all(game.closed for game in created)
    assert instance._draft_source is None and instance._standby is None
    assert instance.state is None and instance._bank_events is bank_events


async def test_anchor_embeddings_are_encoded_once_per_process():
    import edenlive_backend

    encoded = []

    class Pipeline:
        _text_cache = None

        def encode_text(self, text):
            encoded.append(text[0])
            embeddings = object()
            self._text_cache = (tuple(text), embeddings)
            return embeddings

    backend = edenlive_backend.EdenBackend.__new__(edenlive_backend.EdenBackend)
    backend.pipeline = Pipeline()
    backend._bank = {}
    backend._encode_lock = threading.Lock()
    backend._anchor_embeddings = {}
    backend.reset()
    first = backend.anchor_embeddings("A cavern.")
    backend.reset()  # a new world keeps the opening prompts
    backend.pipeline._text_cache = (("A cavern. A shot rings out.",), object())
    assert backend.anchor_embeddings("A cavern.") is first and encoded == ["A cavern."]
    # A reuse records the prompt as the pipeline's last encoded one, as an encode
    # would, so initialize_cache builds the cache from it.
    assert backend.pipeline._text_cache == (("A cavern.",), first)
    limit = edenlive_backend.ANCHOR_EMBEDDINGS
    for i in range(limit):
        backend.anchor_embeddings(f"p{i}")
    assert "A cavern." not in backend._anchor_embeddings  # least recently used goes first
    assert len(backend._anchor_embeddings) == limit


async def test_text_embeddings_are_reused_within_a_world_only():
    import edenlive_backend

    encoded = []

    class Pipeline:
        _text_cache = None

        def encode_text(self, text):
            encoded.append(text[0])
            embeddings = object()
            self._text_cache = (tuple(text), embeddings)
            return embeddings

    backend = edenlive_backend.EdenBackend.__new__(edenlive_backend.EdenBackend)
    backend.pipeline = Pipeline()
    backend._bank = {}
    backend._encode_lock = threading.Lock()
    backend.reset()
    first = backend.text_embeddings("A cavern. A shot rings out.")
    backend.text_embeddings("A cavern.")
    again = backend.text_embeddings("A cavern. A shot rings out.")
    assert again is first and encoded == ["A cavern. A shot rings out.", "A cavern."]
    # A reuse leaves the pipeline's last-encoded record as an encode would.
    assert backend.pipeline._text_cache == (("A cavern. A shot rings out.",), first)
    backend.reset()
    assert backend.text_embeddings("A cavern. A shot rings out.") is not first
    assert len(encoded) == 3

    backend.reset()
    limit = edenlive_backend.TEXT_EMBEDDINGS_PER_WORLD
    for i in range(limit):
        backend.text_embeddings(f"p{i}")
    backend.text_embeddings("p0")  # used again: now the most recent
    backend.text_embeddings("new")  # full: drops the least recently used, p1
    count = len(encoded)
    backend.text_embeddings("p0")
    assert len(encoded) == count
    backend.text_embeddings("p1")
    assert len(encoded) == count + 1
    assert len(backend._text_embeddings) == limit


class FakeTorch:
    """The few torch calls the prompt bank makes, without a GPU."""

    def __init__(self):
        self.cuda = SimpleNamespace(
            Stream=lambda: SimpleNamespace(synchronize=lambda: None),
            stream=lambda stream: contextlib.nullcontext(),
            current_stream=lambda: SimpleNamespace(synchronize=lambda: None),
        )

    @staticmethod
    def no_grad():
        return contextlib.nullcontext()


def bank_backend(encoder=None):
    """An EdenBackend without weights: a pipeline whose encodes are recorded objects."""
    import edenlive_backend

    encoded = []

    def encode(text):
        encoded.append(text[0])
        return object()

    def encode_rows(texts):
        # One row per prompt, as the text encoder returns a batch.
        encoded.extend(texts)
        return [object() for _ in texts]

    class Pipeline:
        _text_cache = None
        text_encoder = staticmethod(encoder or encode_rows)

        def _ensure_oneshot_encoders_loaded(self):
            pass

        def encode_text(self, text):
            embeddings = encode(text)
            self._text_cache = (tuple(text), embeddings)
            return embeddings

    backend = edenlive_backend.EdenBackend.__new__(edenlive_backend.EdenBackend)
    backend.torch = FakeTorch()
    backend.pipeline = Pipeline()
    backend._anchor_embeddings = {}
    backend._bank = {}
    backend._bank_generation = 0
    backend._bank_thread = None
    backend._bank_stream = None
    backend._encode_lock = threading.Lock()
    backend._bank_lock = threading.Lock()
    backend._idle = threading.Condition()
    backend._stepping = False
    backend.reset()
    return backend, encoded


async def test_bank_comes_first_then_the_world_then_an_encode():
    backend, encoded = bank_backend()
    assert backend.set_bank(["A cavern. Shot.", "A cavern. Shot.", "A cavern."], wait=True)[0] == 2
    assert encoded == ["A cavern. Shot.", "A cavern."]
    banked = backend._bank["A cavern. Shot."]
    # A banked prompt is never encoded in a step; it is recorded as the last
    # encoded prompt, as an encode would be.
    assert backend.text_embeddings("A cavern. Shot.") is banked and len(encoded) == 2
    assert backend.pipeline._text_cache == (("A cavern. Shot.",), banked)
    assert "A cavern. Shot." not in backend._text_embeddings
    # A prompt outside the bank: encoded live once, then reused within the world.
    live = backend.text_embeddings("A cavern. Rain.")
    assert backend.text_embeddings("A cavern. Rain.") is live and encoded[-1] == "A cavern. Rain."
    assert len(encoded) == 3
    # A new world keeps the bank; clearing it (a session's end) drops it.
    backend.reset()
    assert backend.text_embeddings("A cavern. Shot.") is banked and len(encoded) == 3
    backend.clear_bank()
    assert backend.text_embeddings("A cavern. Shot.") is not banked and len(encoded) == 4
    # The bank holds at most BANK_PROMPTS prompts.
    import edenlive_backend

    limit = edenlive_backend.BANK_PROMPTS
    assert backend.set_bank([f"p{i}" for i in range(limit + 5)], wait=True)[0] == limit
    assert len(backend._bank) == limit


async def test_a_background_bank_fills_while_steps_encode_what_it_lacks():
    release = threading.Event()
    calls = []

    def slow(text):
        calls.append(text[0])
        if text[0] == "b1":
            assert release.wait(5)
        return object()

    backend, encoded = bank_backend(encoder=slow)
    assert backend.set_bank(["b1", "b2"], wait=False) == (2, 0.0)
    # Nothing is visible until its encode has completed.
    assert backend._bank == {}
    # A new scene text replaces the build: the old one stops at its next
    # prompt and never publishes, and the new one fills the bank.
    backend.set_bank(["c1"], wait=False)
    release.set()
    backend.join_bank()
    assert set(backend._bank) == {"c1"} and "b2" not in calls
    assert backend.text_embeddings("c1") is backend._bank["c1"] and encoded == []
    # A prompt the bank lacks is encoded in the step, as before the bank.
    backend.text_embeddings("d")
    assert encoded == ["d"]


async def test_a_background_bank_encodes_only_between_steps():
    backend, encoded = bank_backend()
    in_step, release = threading.Event(), threading.Event()

    def step(index, condition, prompt):
        in_step.set()
        assert release.wait(5)
        return prompt

    backend._step = step
    stepping = ThreadPoolExecutor(1).submit(backend.step, 0, None, "x")
    assert in_step.wait(5)
    backend.set_bank(["b1", "b2"], wait=False)
    time.sleep(0.05)
    # The step still runs: nothing has been encoded.
    assert encoded == [] and backend._bank == {}
    release.set()
    assert stepping.result(5) == "x"
    backend.join_bank()
    assert encoded == ["b1", "b2"] and set(backend._bank) == {"b1", "b2"}


async def test_select_game_and_set_prompt_rebuild_the_bank(monkeypatch, tmp_path):
    instance, model, _ = await app(monkeypatch)
    (tmp_path / "realtime/games/dust2").mkdir(parents=True)
    Image.new("RGB", (64, 64)).save(tmp_path / "realtime/games/dust2/cover.png")
    instance._root = tmp_path
    instance._entries = {"dust2": {"cover": "cover.png", "prompt": "An alley."}}
    instance._bank_events = {"dust2": [["Shots."], ["Shots.", "A reload."], []]}
    banks = model._backend.banks
    await instance.set_paused(paused=True)
    await instance.select_game(game="dust2")
    # Before a world the reply waits for the bank: scene text + each combination.
    assert banks[-1] == (("An alley. Shots.", "An alley. Shots. A reload.", "An alley."), True)
    await instance.set_prompt(prompt="A desert.")
    assert banks[-1] == (("A desert. Shots.", "A desert. Shots. A reload.", "A desert."), True)
    await instance.start(seed=1)
    # During a world it is rebuilt in the background, and play goes on.
    await instance.set_prompt(prompt="A snowfield.")
    assert banks[-1] == (
        ("A snowfield. Shots.", "A snowfield. Shots. A reload.", "A snowfield."),
        False,
    )
    # The chunk prompt the bank is keyed by is the one the step sends.
    instance._draft_source.events = ("Shots.", "A reload.")
    snapshot = await instance.process_input()
    assert snapshot.prompt == "A snowfield. Shots. A reload."
    # Only the game source has events: another source drops the bank.
    await instance.set_source(source="client")
    assert banks[-1] == ((), False)
    # The session's end drops it in the worker.
    instance.on_session_ended()
    assert model._backend.bank_clears == 1


async def test_graphs_keep_one_cache_and_one_graph_per_step_shape(monkeypatch):
    import edenlive_graphs

    class Wrapper:
        def __init__(self, fn, warmup_iters):
            self.fn, self.warmup_iters = fn, warmup_iters

    flashdreams = ModuleType("flashdreams.infra.cuda_graph")
    flashdreams.CUDAGraphWrapper = Wrapper
    monkeypatch.setitem(sys.modules, "flashdreams.infra.cuda_graph", flashdreams)

    class Cache:
        def __init__(self, text):
            self.network_cache, self.text, self.resets = SimpleNamespace(), text, 0

        def reset(self):
            self.resets += 1

    class Network:
        def replace_text_embeddings(self, network_cache, text):
            replaced.append((network_cache, text))

    class Stock:
        enabled, warmup_iters, resets = True, 2, 0

        def fn(self):
            pass

        def reset(self):
            self.resets += 1

        def select(self, index, *, uncond):
            return ("stock", index, uncond)

    built, replaced = [], []
    stock = Stock()
    network = Network()
    transformer = SimpleNamespace(
        network=SimpleNamespace(_orig_mod=network),
        config=SimpleNamespace(network=SimpleNamespace(patch_size=(1, 2, 2))),
        _cuda_graph_dispatch=stock,
    )

    def stock_initialize(*, height, width, text_embeddings, **kwargs):
        stock.reset()
        transformer._output_height, transformer._output_width = height, width
        network._patch_h, network._patch_w = height // 2, width // 2
        built.append(Cache(text_embeddings))
        return built[-1]

    transformer.initialize_autoregressive_cache = stock_initialize
    dispatch = edenlive_graphs.install(transformer, steady_from=5)
    assert transformer._cuda_graph_dispatch is dispatch

    first = transformer.initialize_autoregressive_cache(height=44, width=80, text_embeddings="a")
    assert built == [first] and dispatch.kept
    graphs = [dispatch.select(index, uncond=False) for index in range(9)]
    # Step 0: one graph per timestep shape (per-token denoising, scalar commit).
    per_token, scalar = SimpleNamespace(shape=(2640,)), SimpleNamespace(shape=())
    calls = []
    Wrapper.__call__ = lambda self, **kwargs: calls.append((self, kwargs["timesteps"]))
    graphs[0](x=1, timesteps=per_token)
    graphs[0](x=2, timesteps=per_token)
    graphs[0](x=3, timesteps=scalar)
    assert calls[0][0] is calls[1][0] and calls[2][0] is not calls[0][0]
    assert calls[2][1] is scalar and calls[0][0].warmup_iters == 2
    assert dispatch.select(0, uncond=False) is graphs[0]
    assert dispatch.select(0, uncond=True) is not graphs[0]
    assert len({id(graph) for graph in graphs[1:5]}) == 4
    assert all(graph is graphs[5] for graph in graphs[5:])
    assert graphs[5] not in graphs[1:5] and graphs[1].warmup_iters == 2
    assert dispatch.select(5, uncond=True) is not graphs[5]

    # A new world at the same size: the same cache, reset and given its text in
    # place, and the same graphs.
    again = transformer.initialize_autoregressive_cache(height=44, width=80, text_embeddings="b")
    assert again is first and first.resets == 1 and len(built) == 1
    assert replaced == [(first.network_cache, "b")]
    assert [dispatch.select(index, uncond=False) for index in range(9)] == graphs
    graphs[0](x=4, timesteps=scalar)
    assert calls[3][0] is calls[2][0]

    # Anything else takes the stock path and the stock dispatch.
    resets = stock.resets
    other = transformer.initialize_autoregressive_cache(
        height=44, width=80, text_embeddings="c", negative_text_embeddings="n"
    )
    assert other is not first and not dispatch.kept and stock.resets == resets + 1
    assert dispatch.select(3, uncond=False) == ("stock", 3, False)
    transformer.initialize_autoregressive_cache(height=32, width=64, text_embeddings="d")
    assert not dispatch.kept and (network._patch_h, network._patch_w) == (16, 32)
    # Back at the kept size: the kept cache and its graphs, layout restored.
    back = transformer.initialize_autoregressive_cache(height=44, width=80, text_embeddings="e")
    assert back is first and dispatch.kept and len(built) == 3
    assert (transformer._output_height, network._patch_w) == (44, 40)
    assert dispatch.select(7, uncond=False) is graphs[5]


async def test_load_skips_warmup_unless_asked(monkeypatch):
    backend = FakeBackend()
    stub = ModuleType("edenlive_backend")
    stub.EdenBackend = lambda **kwargs: backend
    monkeypatch.setitem(sys.modules, "edenlive_backend", stub)
    EdenModel().load(source=Path("/source"), weights=Path("/weights"))
    assert backend.steps == []


async def test_chunk_timer_splits_the_cycle_and_summarizes():
    from edenlive_timing import SUMMARY_EVERY, ChunkTimer

    timer = ChunkTimer()
    assert (
        timer.finished(1.0, frames=9, generate_s=0.3, capture_s=None, prompt_changed=True) is None
    )
    at = 1.0
    for _ in range(SUMMARY_EVERY):
        timer.attempt(at + 0.5)  # emit blocked 0.5 s
        timer.attempt(at + 0.6)  # a refused attempt does not move the start
        timer.drafted(at + 0.7)  # draft arrived 0.2 s after the first attempt
        at += 1.0
        sample = timer.finished(at, frames=12, generate_s=0.25, capture_s=0.6, prompt_changed=False)
        assert sample["cycle_s"] == pytest.approx(1.0)
        assert sample["emit_s"] == pytest.approx(0.5)
        assert sample["draft_wait_s"] == pytest.approx(0.2)
    summary = timer.summary()
    assert summary["new_fps"] == pytest.approx(12.0) and summary["chunks"] == SUMMARY_EVERY
    assert summary["generate_s"] == 0.25 and summary["capture_s"] == 0.6
    assert timer.summary() is None
    timer.restart()
    assert (
        timer.finished(9.0, frames=12, generate_s=0.2, capture_s=0.5, prompt_changed=False) is None
    )


async def test_chunk_timer_summary_covers_one_run():
    from edenlive_timing import SUMMARY_EVERY, ChunkTimer

    def run(timer, at, chunks, capture_s):
        timer.finished(at, frames=12, generate_s=0.2, capture_s=capture_s, prompt_changed=False)
        for _ in range(chunks):
            timer.attempt(at + 0.1)
            timer.drafted(at + 0.2)
            at += 0.5
            timer.finished(at, frames=12, generate_s=0.2, capture_s=capture_s, prompt_changed=False)
        return at

    timer = ChunkTimer()
    at = run(timer, 0.0, SUMMARY_EVERY - 1, capture_s=0.6)
    timer.restart()  # e.g. `set_source` to `client`
    at = run(timer, at + 5.0, 1, capture_s=None)
    assert timer.summary() is None
    run(timer, at, SUMMARY_EVERY - 1, capture_s=None)
    summary = timer.summary()
    assert summary["chunks"] == SUMMARY_EVERY and summary["capture_s"] is None


class FakeViewer:
    id = "viewer"

    async def send(self, message):
        pass


async def preroll_app(monkeypatch, seconds):
    instance, model, sent = await app(monkeypatch)
    chunks = []
    instance.bind_output(broadcast=sent.append, addressed=lambda *a: None, media=chunks.append)
    instance._preroll_s = seconds
    instance._play_fps = 40
    return instance, chunks


def preroll_tracks(chunk):
    return {name: np.asarray(data.data) for name, data in chunk.bundle.tracks.items()}


async def test_preroll_plays_from_connect_until_start(monkeypatch):
    instance, chunks = await preroll_app(monkeypatch, 5)
    await instance.on_connected(FakeViewer())
    await asyncio.sleep(0.3)
    assert instance._preroll is not None and len(chunks) >= 3
    # Black plus noise before a reference is staged; both tracks the same, 1280x704, 4 per emit.
    tracks = preroll_tracks(chunks[0])
    assert set(tracks) == {"main_video", "draft_video"}
    main = tracks["main_video"]
    assert main.shape == (4, 704, 1280, 3) and (main == tracks["draft_video"]).all()
    assert main.max() <= 2 * edenlive.PREROLL_NOISE and not (main[0] == main[1]).all()
    # Paced at play_fps (40): about 12 frames in 0.3 s, never a burst.
    assert 8 <= sum(c.n_frames for c in chunks) <= 20
    # A staged reference replaces the black frames.
    instance.state._game = "freeze"
    instance.state._image = np.full((64, 64, 3), 200, np.uint8)
    instance.state.prompt = "A cavern."
    await asyncio.sleep(0.2)
    assert preroll_tracks(chunks[-1])["main_video"].min() >= 200 - edenlive.PREROLL_NOISE
    # Start stops it before the world: nothing from the pre-roll after the start.
    task = instance._preroll
    await instance.start(seed=7)
    assert task.done() and instance._preroll is None
    count = len(chunks)
    await asyncio.sleep(0.2)
    assert len(chunks) == count


async def test_preroll_stops_on_disconnect_and_at_its_limit(monkeypatch):
    instance, chunks = await preroll_app(monkeypatch, 0.2)
    await instance.on_connected(FakeViewer())
    await asyncio.sleep(0.4)
    assert instance._preroll.done()
    count = len(chunks)
    assert 4 <= sum(c.n_frames for c in chunks) <= 12
    await asyncio.sleep(0.1)
    assert len(chunks) == count
    instance._preroll_s = 5
    await instance.on_connected(FakeViewer())
    task = instance._preroll
    await asyncio.sleep(0.1)
    await instance.on_disconnected(FakeViewer())  # the last client: `connected` is clear
    assert task.done() and instance._preroll is None


async def test_preroll_off_and_not_during_a_world(monkeypatch):
    instance, chunks = await preroll_app(monkeypatch, 0)
    await instance.on_connected(FakeViewer())
    assert instance._preroll is None
    instance, chunks = await preroll_app(monkeypatch, 5)
    instance.state._game = "freeze"
    instance.state._image = np.zeros((64, 64, 3), np.uint8)
    instance.state.prompt = "A cavern."
    await instance.start(seed=7)
    await instance.on_connected(FakeViewer())  # a viewer joining a running world
    assert instance._preroll is None


async def test_preroll_resumes_when_a_new_reference_ends_the_world(monkeypatch):
    instance, chunks = await preroll_app(monkeypatch, 5)
    instance.connected.set()
    await instance.on_connected(FakeViewer())
    instance.state._game = "freeze"
    instance.state._image = np.zeros((64, 64, 3), np.uint8)
    instance.state.prompt = "A cavern."
    await instance.start(seed=7)
    assert instance._preroll is None
    instance._stage_reference(np.full((64, 64, 3), 90, np.uint8), "freeze/cover.png")
    assert instance._preroll is not None and not instance._preroll.done()
    await asyncio.sleep(0.2)
    assert preroll_tracks(chunks[-1])["main_video"].min() >= 90 - edenlive.PREROLL_NOISE
    instance.connected.clear()
    await instance.on_disconnected(FakeViewer())
    assert instance._preroll is None
