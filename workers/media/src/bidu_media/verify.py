"""Validation before delivery (spec section 9): decode the whole output and check it."""

from __future__ import annotations

import re
import subprocess
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel

from bidu_timeline import Timeline

from .ffmpeg import FFMPEG, ffprobe_json
from .render import RenderProfile


class OutputCheck(BaseModel):
    ok: bool
    errors: list[str] = []
    review: list[str] = []
    """Sustained black or silence: flagged for the creator, not treated as failure."""
    frames: int
    duration_seconds: float


def verify_output(path: Path, timeline: Timeline, profile: RenderProfile) -> OutputCheck:
    data = ffprobe_json(path, "-count_frames", "-show_streams", "-show_format")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    errors: list[str] = []
    frames = 0
    if video is None:
        errors.append("Output has no video stream")
    else:
        frames = int(video.get("nb_read_frames") or 0)
        if abs(frames - timeline.duration_frames) > 1:
            errors.append(f"Expected {timeline.duration_frames} frames, decoded {frames}")
        if (int(video["width"]), int(video["height"])) != (profile.width, profile.height):
            errors.append(
                f"Expected {profile.width}x{profile.height}, got {video['width']}x{video['height']}"
            )
        if video.get("codec_name") != "h264":
            errors.append(f"Expected H.264, got {video.get('codec_name')}")
    if audio is None:
        errors.append("Output has no audio stream")
    duration = float(Fraction(data.get("format", {}).get("duration", "0")))

    review = _scan_black_and_silence(path)
    return OutputCheck(ok=not errors, errors=errors, review=review, frames=frames, duration_seconds=duration)


def _scan_black_and_silence(path: Path) -> list[str]:
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=1.0:pix_th=0.10",
            "-af",
            "silencedetect=n=-50dB:d=2.0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    notes = []
    for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", proc.stderr):
        notes.append(f"Black picture from {float(m[1]):.1f}s to {float(m[2]):.1f}s")
    starts = re.findall(r"silence_start: ([\d.]+)", proc.stderr)
    ends = re.findall(r"silence_end: ([\d.]+)", proc.stderr)
    for s, e in zip(starts, ends, strict=False):
        notes.append(f"Silence from {float(s):.1f}s to {float(e):.1f}s")
    return notes
