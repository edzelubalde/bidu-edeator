"""Stage 4: shot hints and analysis windows (spec section 4, "Analysis granularity")."""

from __future__ import annotations

from pathlib import Path

WINDOW_MS = 45_000
OVERLAP_MS = 5_000
SNAP_MS = 4_000
"""A window end moves to a detected cut if one is this close, so windows rarely split a shot."""


def detect_cuts(proxy: Path) -> list[int]:
    """Scene cuts in proxy milliseconds. A hint only; long takes have none."""
    from scenedetect import AdaptiveDetector, detect

    scenes = detect(str(proxy), AdaptiveDetector())
    return [int(start.get_seconds() * 1000) for start, _ in scenes[1:]]


def make_windows(
    duration_ms: int,
    cuts: list[int],
    *,
    window_ms: int = WINDOW_MS,
    overlap_ms: int = OVERLAP_MS,
    snap_ms: int = SNAP_MS,
) -> list[tuple[int, int]]:
    """Cover the whole clip with overlapping windows, preferring to end at cuts.

    Every millisecond is inside at least one window, so silent footage is
    analyzed too.
    """
    if duration_ms <= 0:
        return []
    if duration_ms <= window_ms + snap_ms:
        return [(0, duration_ms)]
    windows = []
    start = 0
    while True:
        end = start + window_ms
        if end >= duration_ms - snap_ms:
            windows.append((start, duration_ms))
            return windows
        near = [c for c in cuts if abs(c - end) <= snap_ms and c - start > overlap_ms * 2]
        if near:
            end = min(near, key=lambda c: abs(c - end))
        windows.append((start, end))
        start = end - overlap_ms


def shots_in_window(window: tuple[int, int], cuts: list[int]) -> list[tuple[int, int]]:
    """Split a window at the cuts inside it."""
    start, end = window
    inner = [c for c in cuts if start < c < end]
    edges = [start, *inner, end]
    return [(a, b) for a, b in zip(edges, edges[1:], strict=False) if b > a]
