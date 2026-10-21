# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""Everything an EdenLive client sees: the tracks, the state, and the messages."""

from typing import Literal

import numpy as np
from edenlive_draft import Draft
from reactor_runtime import (
    InputField,
    InputState,
    MediaInput,
    MessageField,
    ModelMessage,
    Output,
    Video,
)

Key = Literal[
    "w",
    "a",
    "s",
    "d",
    "arrowup",
    "arrowdown",
    "arrowleft",
    "arrowright",
    "f",
    " ",
    "shift",
    "e",
    "r",
    "g",
]

# Where the draft video comes from: the native game, or the client's own video.
SOURCES = ("game", "client")


class EdenState(InputState):
    prompt: str = InputField(
        default="",
        max_length=4096,
        moderate=True,
        description=(
            "Scene description that styles the generated video. Set it with "
            "`set_prompt` or `select_game`; a change applies from the next chunk "
            "and keeps the world's visual history."
        ),
    )
    keys: list[Key] | None = InputField(
        default=None,
        max_length=14,
        description=(
            "Keys the client holds in the game. Set it with `set_keys`. Each "
            "game frame not yet captured reads the latest value; ignored while "
            "the draft source is `client`."
        ),
    )
    paused: bool = InputField(
        default=True,
        description=(
            "Whether the world is on hold. Set it with `set_paused`. True until "
            "`start`; resuming continues the same world."
        ),
    )
    _source: str = "game"
    _game: str = ""
    _image: np.ndarray | None = None
    _image_name: str = ""
    _seed: int = 2026092901
    _world_id: int = 0
    _applied_world_id: int | None = None
    _started: bool = False
    _chunks: int = 0
    _frames_wanted: int = 9
    _seconds: float | None = None
    _draft: Draft | None = None
    _ended: bool = False
    _keys_holder: object | None = None
    # The look rate (yaw, pitch) held now, each -1..1, set with `set_look`.
    _look: tuple[float, float] = (0.0, 0.0)
    _look_holder: object | None = None
    _prompt_changed: bool = False
    # The game's HUD after the latest generated chunk (`edenlive_game.hud_status`).
    _hud: dict | None = None
    # The event sentences the source added to the latest generated chunk.
    _events: tuple[str, ...] = ()


class EdenMedia(MediaInput):
    # Read only while the draft source is `client`.
    source_video: Video


class StateUpdate(ModelMessage):
    """Emitted on connect, after every accepted command, and after every chunk.

    The complete observable state of the session, so a client can render its
    controls from one message.
    """

    source: str = MessageField(
        description=(
            "Where the draft video comes from: `game` (the selected native "
            "game) or `client` (the client's `source_video` track)."
        )
    )
    game: str = MessageField(description="Selected game, or empty before `select_game`.")
    image_name: str = MessageField(
        description="File name of the appearance reference, or empty before one is staged."
    )
    prompt: str = MessageField(
        description=(
            "Scene description in effect. In `game` mode each chunk also adds the "
            "game's own event sentences."
        )
    )
    seed: int = MessageField(description="Seed of the current world.")
    keys: list[str] = MessageField(
        description="Keys held now. Frames already captured keep the keys they were captured with."
    )
    look: list[float] = MessageField(
        description="Look rate held now, as `[yaw, pitch]`, each -1..1; `[0, 0]` when none."
    )
    paused: bool = MessageField(description="Whether the world is on hold.")
    started: bool = MessageField(description="Whether `start` has created the current world.")
    ended: bool = MessageField(
        description="Whether the game reached an outcome. Only `start` or `set_source` continues."
    )
    game_state: str = MessageField(
        description=(
            "The game's own state after the latest chunk, such as `RUNNING`, `WON`, or "
            "`LOST`; empty before the first chunk, with source `client`, and after "
            "`start`, `select_game`, `set_image`, or `set_source`."
        )
    )
    outcome: list[str] = MessageField(
        description=(
            "The game's result card once it reaches an outcome: the result, its reason, "
            "and replay advice, one line each. Empty during play."
        )
    )
    health: float | None = MessageField(
        description="Player health after the latest chunk, or null when the game reports none."
    )
    health_max: float | None = MessageField(
        description="Maximum player health, or null when the game reports none."
    )
    events: list[str] = MessageField(
        description=(
            "What happened in the game during the chunk counted by `chunks`, as the "
            "sentences added to that chunk's prompt (for example a hit or a bite). "
            "Every `state_update` until the next chunk repeats them; empty when the "
            "chunk had none, with source `client`, and after the resets that clear "
            "`game_state`."
        )
    )
    world_id: int = MessageField(
        description="Identity of the current world; `start` increments it."
    )
    chunks: int = MessageField(description="Chunks generated in the current world.")
    seconds: float | None = MessageField(
        description="Seconds the last chunk took to generate, or null before the first chunk."
    )


class EdenOutput(Output):
    """The generated video and the draft it reskins, frame for frame on two tracks."""

    # The generated frames, 1280x704.
    main_video: Video
    # The draft each generated frame was made from, 1280x704, with the same
    # timestamp and metadata as its `main_video` frame.
    draft_video: Video
