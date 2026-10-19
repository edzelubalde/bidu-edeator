"""Stages 1-2: validate actual streams and capture what later stages need (spec section 4)."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel

from bidu_timeline import Rate

from .errors import MediaError
from .ffmpeg import ffprobe_json

SUPPORTED_CONTAINERS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}  # ffprobe reports these together
SUPPORTED_VIDEO = {"h264", "hevc"}
SUPPORTED_AUDIO = {"aac", "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be", "pcm_f32le"}
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}
MAX_FILE_BYTES = 5 * 1024**3


class VideoStream(BaseModel):
    index: int
    codec: str
    profile: str | None = None
    width: int
    height: int
    rotation: int = 0
    pix_fmt: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    nominal_rate: Rate
    average_rate: Rate | None = None
    variable_frame_rate: bool = False
    start_seconds: Fraction = Fraction(0)
    duration_seconds: Fraction | None = None

    @property
    def is_hdr(self) -> bool:
        return (self.color_transfer or "") in HDR_TRANSFERS

    @property
    def display_size(self) -> tuple[int, int]:
        """Width and height as the viewer sees them, after rotation."""
        if self.rotation % 180:
            return self.height, self.width
        return self.width, self.height

    @property
    def is_portrait(self) -> bool:
        w, h = self.display_size
        return h > w


class AudioStream(BaseModel):
    index: int
    codec: str
    sample_rate: int
    channels: int
    start_seconds: Fraction = Fraction(0)


class Probe(BaseModel):
    container: str
    byte_size: int
    duration_seconds: Fraction
    format_start_seconds: Fraction = Fraction(0)
    video: VideoStream
    audio: AudioStream | None = None
    capture: dict[str, str] = {}
    """Recording metadata such as creation_time, device model and ISO 6709 location."""

    @property
    def seek_offset_seconds(self) -> Fraction:
        """Add to a canonical source time to get an ffmpeg input ``-ss`` value.

        Canonical source time 0 is the first video frame; ffmpeg seeks relative
        to the container start, which can differ when streams start late.
        """
        return self.video.start_seconds - self.format_start_seconds

    @property
    def usable_duration_seconds(self) -> Fraction:
        """Canonical duration: from the first video frame to the end of the video stream."""
        end = self.format_start_seconds + self.duration_seconds
        if self.video.duration_seconds is not None:
            end = min(end, self.video.start_seconds + self.video.duration_seconds)
        return end - self.video.start_seconds


def _frac(value: str | None, default: Fraction | None = None) -> Fraction | None:
    if value in (None, "", "N/A"):
        return default
    return Fraction(value)


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return int(side["rotation"]) % 360
    tag = (stream.get("tags") or {}).get("rotate")
    return int(tag) % 360 if tag else 0


def _detect_vfr(path: Path, index: int, nominal: Rate) -> bool:
    """Look at real packet spacing in the first 20 s rather than trusting header rates."""
    data = ffprobe_json(
        path, "-select_streams", str(index), "-read_intervals", "%+20",
        "-show_entries", "packet=pts_time,flags",
    )
    times = sorted(
        Fraction(p["pts_time"]) for p in data.get("packets", []) if p.get("pts_time") not in (None, "N/A")
    )
    if len(times) < 3:
        return False
    deltas = [b - a for a, b in zip(times, times[1:], strict=False)]
    frame = nominal.frame_seconds
    return any(abs(d - frame) > frame / 4 for d in deltas)


def probe(path: Path) -> Probe:
    data = ffprobe_json(path, "-show_format", "-show_streams")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    container_names = set((fmt.get("format_name") or "").split(","))
    if not container_names & SUPPORTED_CONTAINERS:
        raise MediaError(
            "UNSUPPORTED_CONTAINER",
            "Only MP4 and MOV files are supported. This file is a "
            f"{fmt.get('format_long_name') or 'different'} file.",
        )
    byte_size = int(fmt.get("size") or path.stat().st_size)
    if byte_size > MAX_FILE_BYTES:
        raise MediaError("FILE_TOO_LARGE", "Files can be up to 5 GB each.")

    vids = [s for s in streams if s.get("codec_type") == "video" and not _is_cover_art(s)]
    if not vids:
        raise MediaError("NO_VIDEO_STREAM", "This file has no video track.")
    v = vids[0]
    codec = v.get("codec_name", "unknown")
    if codec not in SUPPORTED_VIDEO:
        raise MediaError(
            "UNSUPPORTED_CODEC",
            f"This video uses {codec.upper()}, but only H.264 and HEVC are supported. "
            "Export or re-record it as H.264 or HEVC.",
        )
    nominal = Rate.parse(v.get("r_frame_rate") or "30/1")
    avg_text = v.get("avg_frame_rate")
    average = Rate.parse(avg_text) if avg_text and not avg_text.startswith("0") else None
    variable = average is not None and abs(average.fps - nominal.fps) / nominal.fps > Fraction(1, 200)
    if not variable:
        variable = _detect_vfr(path, int(v["index"]), nominal)

    video = VideoStream(
        index=int(v["index"]),
        codec=codec,
        profile=v.get("profile"),
        width=int(v["width"]),
        height=int(v["height"]),
        rotation=_rotation(v),
        pix_fmt=v.get("pix_fmt"),
        color_primaries=v.get("color_primaries"),
        color_transfer=v.get("color_transfer"),
        color_space=v.get("color_space"),
        nominal_rate=nominal,
        average_rate=average,
        variable_frame_rate=variable,
        start_seconds=_frac(v.get("start_time"), Fraction(0)),
        duration_seconds=_frac(v.get("duration")),
    )

    audio = None
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    if auds:
        a = auds[0]
        if a.get("codec_name") not in SUPPORTED_AUDIO:
            raise MediaError(
                "UNSUPPORTED_AUDIO_CODEC",
                f"The audio in this file uses {a.get('codec_name', 'an unknown codec')}, "
                "but only AAC and PCM audio are supported.",
            )
        audio = AudioStream(
            index=int(a["index"]),
            codec=a["codec_name"],
            sample_rate=int(a.get("sample_rate") or 48000),
            channels=int(a.get("channels") or 2),
            start_seconds=_frac(a.get("start_time"), Fraction(0)),
        )

    duration = _frac(fmt.get("duration"))
    if not duration or duration <= 0:
        raise MediaError("MEDIA_DECODE_FAILED", "This file has no playable duration.")

    tags = {**(fmt.get("tags") or {}), **(v.get("tags") or {})}
    capture = {
        k: str(tags[k])
        for k in (
            "creation_time",
            "com.apple.quicktime.creationdate",
            "com.apple.quicktime.location.ISO6709",
            "location",
            "com.apple.quicktime.model",
            "com.android.manufacturer",
            "com.android.model",
        )
        if k in tags
    }

    return Probe(
        container=",".join(sorted(container_names & SUPPORTED_CONTAINERS)),
        byte_size=byte_size,
        duration_seconds=duration,
        format_start_seconds=_frac(fmt.get("start_time"), Fraction(0)),
        video=video,
        audio=audio,
        capture=capture,
    )


def _is_cover_art(stream: dict) -> bool:
    return bool((stream.get("disposition") or {}).get("attached_pic"))
