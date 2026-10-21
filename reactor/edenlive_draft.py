# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""The draft chunk every source produces, and the client's video as a source.

A draft source is where the video EdenLive reskins comes from. The application
holds one source at a time, either the native game (``edenlive_game.EdenGame``)
or the client's ``source_video`` track (``ClientSource``), and drives both
through the same three calls:

- ``prepare(frames)`` asks for the next chunk of ``frames`` draft frames.
- ``await take()`` returns that chunk as a ``Draft``, or ``None`` while it is
  not ready yet.
- ``set_keys(held)``, ``set_look(yaw, pitch)``, ``set_paused(paused)`` and
  ``close()`` forward the session's controls; a source that has no use for
  one ignores it.

Past ``take()`` nothing knows which source made a chunk: the application
derives the model's condition frames, the prompt, and the left half of the
output from the ``Draft`` alone.
"""

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from PIL import Image
from reactor_runtime import BufferClosed, ReadMode, get_logger

logger = get_logger(__name__)

DRAFT_WIDTH = 1280
DRAFT_HEIGHT = 704
# The rate a client's frames are played at until their arrival rate is measured.
CLIENT_DEFAULT_FPS = 24.0
# Measured client rates are clamped to this range before they pace the output.
CLIENT_FPS_RANGE = (10.0, 60.0)
# Weight of each new measurement in the running estimate.
CLIENT_FPS_SMOOTHING = 0.25
# Shortest span of arrivals a measurement covers; a shorter one is extended.
CLIENT_FPS_WINDOW_S = 0.25


@dataclass(frozen=True, kw_only=True)
class Draft:
    """One chunk of draft video, ready to be reskinned.

    ``frames`` is RGB uint8 [T,704,1280,3]. ``metadata`` holds one dict per
    frame, published on `main_video` beside the frame. ``events`` are prompt
    sentences the source adds to this chunk only. ``ended`` says the source
    reached an outcome and cannot continue. ``hud`` is the source's own
    status after the chunk (the game's state, outcome, and health), or
    ``None`` for a source that has none. ``capture_seconds`` is how long the
    source took to produce the chunk once asked, when it measures that.
    ``fps`` is the rate the source's own frames advance at, which the output
    plays the chunk at; ``None`` plays it at the application's `play_fps`.
    """

    frames: np.ndarray
    metadata: tuple[dict[str, Any], ...]
    events: tuple[str, ...] = ()
    ended: bool = False
    hud: dict[str, Any] | None = None
    capture_seconds: float | None = None
    fps: float | None = None


class DraftSource(Protocol):
    """What the application needs from a source of draft video."""

    kind: str

    def prepare(self, frames: int) -> None: ...

    async def take(self) -> Draft | None: ...

    def set_keys(self, held: tuple[str, ...]) -> None: ...

    def set_look(self, yaw: float, pitch: float) -> None: ...

    def set_paused(self, paused: bool) -> None: ...

    def close(self) -> None: ...


def fit_frames(frames: Sequence[np.ndarray]) -> np.ndarray:
    """Scale each RGB frame to cover 1280x704 and center-crop it there."""
    fitted = []
    resized: dict[int, np.ndarray] = {}
    for frame in frames:
        if id(frame) in resized:
            # A repeated frame (draft_video: sparse) is scaled once.
            fitted.append(resized[id(frame)])
            continue
        source = frame
        height, width = frame.shape[:2]
        if (width, height) != (DRAFT_WIDTH, DRAFT_HEIGHT):
            scale = max(DRAFT_WIDTH / width, DRAFT_HEIGHT / height)
            size = (
                max(DRAFT_WIDTH, round(width * scale)),
                max(DRAFT_HEIGHT, round(height * scale)),
            )
            left = (size[0] - DRAFT_WIDTH) // 2
            top = (size[1] - DRAFT_HEIGHT) // 2
            picture = Image.fromarray(np.ascontiguousarray(frame[..., :3]))
            picture = picture.resize(size, Image.Resampling.BILINEAR)
            frame = np.asarray(picture.crop((left, top, left + DRAFT_WIDTH, top + DRAFT_HEIGHT)))
        resized[id(source)] = frame
        fitted.append(frame)
    return np.stack(fitted).astype(np.uint8, copy=False)


class ClientSource:
    """Draft frames read from the client's `source_video` track, newest first.

    Each chunk is the latest ``frames`` frames the client sent, so the draft
    follows the client's live video and never replays a backlog. The client
    has no keys or outcome; those calls do nothing.

    Each chunk plays at the rate the client's frames arrive, so it plays for
    as long as the client takes to send the next chunk's frames: the output
    neither holds a frame waiting for them nor skips frames the client sent.
    The WebRTC transport gives inbound frames no ``pts``, so the rate is
    measured here: frames the track received (its ``total_received``) over
    the server's clock, from one chunk to the next, each span at least
    ``CLIENT_FPS_WINDOW_S``. Measurements are smoothed and clamped to
    ``CLIENT_FPS_RANGE``; the rate is ``CLIENT_DEFAULT_FPS`` until the first.
    """

    kind = "client"

    def __init__(self, track, clock: Callable[[], float] = time.monotonic) -> None:
        self._track = track
        self._clock = clock
        self._wanted = 0
        self._fps = CLIENT_DEFAULT_FPS
        self._measured = False
        # When the current measurement started, and the track's count then.
        self._since = (clock(), track.total_received)

    @property
    def fps(self) -> float:
        """The client's frame rate as estimated so far."""
        return self._fps

    def _measure(self) -> None:
        now, received = self._clock(), self._track.total_received
        started, before = self._since
        seconds, frames = now - started, received - before
        if frames < 0:
            # The track was reset (a new connection); measure from here.
            self._since = (now, received)
            return
        if seconds < CLIENT_FPS_WINDOW_S:
            return
        self._since = (now, received)
        low, high = CLIENT_FPS_RANGE
        rate = min(max(frames / seconds, low), high)
        if self._measured:
            rate = self._fps + CLIENT_FPS_SMOOTHING * (rate - self._fps)
        self._fps = rate
        self._measured = True
        logger.debug(
            "client frame rate",
            fps=round(rate, 2),
            frames=frames,
            seconds=round(seconds, 3),
        )

    def prepare(self, frames: int) -> None:
        self._wanted = frames

    async def take(self) -> Draft | None:
        if not self._wanted:
            return None
        try:
            frames = self._track.try_read(self._wanted, mode=ReadMode.LATEST)
        except BufferClosed:
            return None
        if frames is None:
            return None
        self._measure()
        pixels = await asyncio.to_thread(fit_frames, [frame.data for frame in frames])
        metadata = tuple({"source": self.kind, "pts": frame.pts} for frame in frames)
        return Draft(frames=pixels, metadata=metadata, fps=self._fps)

    def set_keys(self, held: tuple[str, ...]) -> None:
        pass

    def set_look(self, yaw: float, pitch: float) -> None:
        pass

    def set_paused(self, paused: bool) -> None:
        pass

    def close(self) -> None:
        pass
