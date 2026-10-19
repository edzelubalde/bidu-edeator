"""Stage 3: SDR preview proxy, analysis audio, thumbnails and waveform (spec section 4).

The original is never modified. The proxy is constant 30 fps so the browser
and the video model see a predictable stream; ``TimeMap`` records how proxy
time maps back to canonical source time.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel

from bidu_timeline import Rate

from .ffmpeg import has_filter, run_ffmpeg
from .probe import Probe

TRANSFORM_VERSION = "normalize-1"
PROXY_RATE = Rate(numerator=30)
PROXY_SHORT_EDGE = 720


class TimeMap(BaseModel):
    """``source = proxy + proxy_to_source_seconds``, in canonical source time.

    The ``fps`` filter keeps each frame's original timestamp and only drops or
    repeats frames, so the mapping is a constant offset even for variable
    frame rate input. The offset exists because ffmpeg rebases outputs to the
    container start while canonical time starts at the first video frame.
    """

    proxy_to_source_seconds: Fraction
    proxy_rate: Rate = PROXY_RATE
    source_duration_seconds: Fraction

    def to_source(self, proxy_seconds: Fraction) -> Fraction:
        t = Fraction(proxy_seconds) + self.proxy_to_source_seconds
        return min(max(t, Fraction(0)), self.source_duration_seconds)

    def to_proxy(self, source_seconds: Fraction) -> Fraction:
        return Fraction(source_seconds) - self.proxy_to_source_seconds


class Derived(BaseModel):
    proxy: Path
    analysis_audio: Path | None
    thumbnails: list[Path]
    thumbnail_interval_seconds: int
    waveform: Path | None
    time_map: TimeMap
    transform_version: str = TRANSFORM_VERSION


def sdr_filter(probe: Probe) -> str:
    """Tone-map HDR to Rec.709 once; pass SDR through untouched."""
    if not probe.video.is_hdr:
        return "format=yuv420p"
    if has_filter("zscale"):
        return (
            "zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
        )
    # Without zimg we can only convert the pixel format; colors will look flat.
    return "format=yuv420p"


def fit_filter(width: int, height: int, fit: str = "contain", pad_color: str = "0x111111") -> str:
    if fit == "cover":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={pad_color},setsar=1"
    )


def proxy_size(probe: Probe) -> tuple[int, int]:
    w, h = probe.video.display_size
    scale = Fraction(PROXY_SHORT_EDGE, min(w, h))
    if scale >= 1:
        scale = Fraction(1)
    return _even(w * scale), _even(h * scale)


def _even(x: Fraction) -> int:
    return max(2, int(x) // 2 * 2)


def normalize(source: Path, probe: Probe, out_dir: Path, *, thumb_every: int = 2) -> Derived:
    out_dir.mkdir(parents=True, exist_ok=True)
    proxy = out_dir / "proxy.mp4"
    w, h = proxy_size(probe)
    vf = f"{sdr_filter(probe)},scale={w}:{h},fps={PROXY_RATE.numerator}/{PROXY_RATE.denominator}"
    args = [
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
    ]
    if probe.audio:
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"]
    args += ["-movflags", "+faststart", "-metadata:s:v:0", "rotate=0", str(proxy)]
    _atomic(proxy, args)

    analysis_audio = None
    waveform = None
    if probe.audio:
        analysis_audio = out_dir / "audio_16k.wav"
        _atomic(
            analysis_audio,
            [
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(analysis_audio),
            ],
        )
        waveform = out_dir / "waveform.png"
        _atomic(
            waveform,
            [
                "-i",
                str(analysis_audio),
                "-filter_complex",
                "showwavespic=s=1200x120:colors=0x7aa2f7",
                "-frames:v",
                "1",
                str(waveform),
            ],
        )

    thumbs_dir = out_dir / "thumbs"
    thumbs_dir.mkdir(exist_ok=True)
    run_ffmpeg(
        [
            "-i",
            str(proxy),
            "-vf",
            f"fps=1/{thumb_every},scale=320:-2",
            "-q:v",
            "4",
            str(thumbs_dir / "t%05d.jpg"),
        ]
    )
    thumbnails = sorted(thumbs_dir.glob("t*.jpg"))

    time_map = TimeMap(
        proxy_to_source_seconds=-probe.seek_offset_seconds,
        source_duration_seconds=probe.usable_duration_seconds,
    )
    return Derived(
        proxy=proxy,
        analysis_audio=analysis_audio,
        thumbnails=thumbnails,
        thumbnail_interval_seconds=thumb_every,
        waveform=waveform,
        time_map=time_map,
    )


def _atomic(dest: Path, args: list[str]) -> None:
    """Write to a temporary name and rename, so a crash never leaves a half file in place."""
    tmp = dest.with_name(f".tmp-{dest.name}")
    run_ffmpeg([*args[:-1], str(tmp)])
    tmp.replace(dest)
