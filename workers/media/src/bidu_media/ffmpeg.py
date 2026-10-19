"""Subprocess wrappers for ffmpeg/ffprobe with time limits and safe error text."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .errors import MediaError

FFMPEG = os.environ.get("BIDU_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("BIDU_FFPROBE", "ffprobe")


def run_ffmpeg(args: list[str], *, timeout: float = 3600, code: str = "MEDIA_DECODE_FAILED") -> str:
    """Run ffmpeg; on failure raise MediaError with the tail of stderr (never the full log)."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise MediaError(code, "Media processing took too long and was stopped.", retryable=True) from e
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise MediaError(code, "Media processing failed.", detail=tail)
    return proc.stderr


def ffprobe_json(path: Path, *extra: str, timeout: float = 120) -> dict:
    cmd = [FFPROBE, "-v", "error", "-of", "json", *extra, str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise MediaError("MEDIA_DECODE_FAILED", "Reading the file took too long.", retryable=True) from e
    if proc.returncode != 0:
        raise MediaError(
            "MEDIA_DECODE_FAILED", "This file could not be read as video.", detail=proc.stderr[-500:]
        )
    return json.loads(proc.stdout or "{}")


def has_filter(name: str) -> bool:
    proc = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True)
    return any(line.split()[1:2] == [name] for line in proc.stdout.splitlines() if line.strip())
