# Copyright (c) 2026 Reactor Technologies, Inc. All rights reserved.
"""Bounded, off-loop image decoding; the backend retains the native float resize."""

import io
from pathlib import Path

import numpy as np
from PIL import Image


def decode_image(data: bytes) -> np.ndarray:
    """Return RGB uint8 pixels without changing the author's resize arithmetic."""
    if not data or len(data) > 25 * 1024 * 1024:
        raise ValueError("Choose a non-empty image up to 25 MiB")
    with Image.open(io.BytesIO(data)) as picture:
        if picture.width * picture.height > 16_000_000 or min(picture.size) < 32:
            raise ValueError(
                "Image dimensions must be at least 32 pixels and at most 16 megapixels"
            )
        if picture.format not in ("PNG", "JPEG", "WEBP"):
            raise ValueError("Choose a PNG, JPEG or WebP image")
        return np.array(picture.convert("RGB"))


def read_image(path: Path) -> np.ndarray:
    """Read and decode an authorized bundled reference in a worker thread."""
    return decode_image(path.read_bytes())
