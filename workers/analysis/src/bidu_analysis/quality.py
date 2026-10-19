"""Cheap technical quality signals, kept separate from story relevance (spec section 4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from .models import Quality

SAMPLE_W, SAMPLE_H = 320, 180


def _frames(proxy: Path, start_ms: int, end_ms: int, count: int = 3) -> list[np.ndarray]:
    frames = []
    span = max(1, end_ms - start_ms)
    for i in range(count):
        t = (start_ms + span * (i + 1) / (count + 1)) / 1000
        raw = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{t:.3f}",
                "-i",
                str(proxy),
                "-frames:v",
                "1",
                "-vf",
                f"scale={SAMPLE_W}:{SAMPLE_H},format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
        ).stdout
        if len(raw) == SAMPLE_W * SAMPLE_H:
            frames.append(np.frombuffer(raw, np.uint8).reshape(SAMPLE_H, SAMPLE_W).astype(np.float32))
    return frames


def measure(proxy: Path, start_ms: int, end_ms: int, *, speech: float | None = None) -> Quality:
    frames = _frames(proxy, start_ms, end_ms)
    if not frames:
        return Quality(sharpness=0.0, exposure=0.0, speech=speech)
    sharp, expo = [], []
    for f in frames:
        lap = f[:-2, 1:-1] + f[2:, 1:-1] + f[1:-1, :-2] + f[1:-1, 2:] - 4 * f[1:-1, 1:-1]
        # Variance of the Laplacian; ~300 and above reads as sharp at this size.
        sharp.append(min(1.0, float(lap.var()) / 300.0))
        mean = float(f.mean())
        clipped = float(((f < 8) | (f > 247)).mean())
        expo.append(max(0.0, 1.0 - abs(mean - 118) / 118 - clipped))
    return Quality(sharpness=sum(sharp) / len(sharp), exposure=sum(expo) / len(expo), speech=speech)
