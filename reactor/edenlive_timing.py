# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""Where each chunk's wall time goes, measured on the application side.

A chunk's cycle runs from the end of the previous ``process_output()`` to the
end of its own, and splits into:

- ``emit_s``: the previous chunk's ``emit()``, which blocks while the playout
  queue (one chunk deep) is full, plus the loop's own turn;
- ``draft_wait_s``: refused steps while the source had no draft ready;
- ``generate_s``: ``generate()``, as the runtime measures it, including a
  text encode when the chunk's prompt was neither banked nor seen earlier in
  the world (``text_encoded``);
- the remainder: building the step input and ``process_output()``.

``capture_s`` is the source's own time to produce the draft once asked.
"""

import statistics
from dataclasses import dataclass, field

SUMMARY_EVERY = 12


@dataclass
class ChunkTimer:
    """Split each chunk's cycle into its parts and summarize every few chunks."""

    _output_at: float | None = None
    _input_since: float | None = None
    _draft_at: float | None = None
    _samples: list[dict] = field(default_factory=list)

    def restart(self) -> None:
        """Start a new measurement run at a pause, a new world, or a source switch.

        The previous chunk is forgotten, so the gap is not a slow cycle, and so
        are the samples not yet summarized: a summary covers one run only.
        """
        self._output_at = self._input_since = self._draft_at = None
        self._samples = []

    def attempt(self, now: float) -> None:
        """Mark a step attempt; the first one after a chunk ends its emit."""
        if self._input_since is None:
            self._input_since = now

    def drafted(self, now: float) -> None:
        """Mark the moment the source delivered this chunk's draft."""
        self._draft_at = now

    def finished(
        self,
        now: float,
        *,
        frames: int,
        generate_s: float | None,
        capture_s: float | None,
        prompt_changed: bool,
        text_encoded: bool = False,
    ) -> dict | None:
        """Close the chunk; return its sample when its whole cycle was observed."""
        sample = None
        if None not in (self._output_at, self._input_since, self._draft_at):
            sample = {
                "frames": frames,
                "cycle_s": now - self._output_at,
                "emit_s": self._input_since - self._output_at,
                "draft_wait_s": self._draft_at - self._input_since,
                "generate_s": generate_s,
                "capture_s": capture_s,
                "prompt_changed": prompt_changed,
                "text_encoded": text_encoded,
            }
            self._samples.append(sample)
        self._output_at = now
        self._input_since = self._draft_at = None
        return sample

    def summary(self) -> dict | None:
        """Return medians over the last ``SUMMARY_EVERY`` sampled chunks, then reset."""
        if len(self._samples) < SUMMARY_EVERY:
            return None
        samples, self._samples = self._samples, []

        def median(key: str) -> float | None:
            values = [s[key] for s in samples if s[key] is not None]
            return round(statistics.median(values), 3) if values else None

        cycle = sum(s["cycle_s"] for s in samples)
        return {
            "chunks": len(samples),
            "new_fps": round(sum(s["frames"] for s in samples) / cycle, 1),
            "cycle_s": median("cycle_s"),
            "emit_s": median("emit_s"),
            "draft_wait_s": median("draft_wait_s"),
            "generate_s": median("generate_s"),
            "capture_s": median("capture_s"),
            "prompt_changes": sum(s["prompt_changed"] for s in samples),
            "text_encodes": sum(s["text_encoded"] for s in samples),
        }
