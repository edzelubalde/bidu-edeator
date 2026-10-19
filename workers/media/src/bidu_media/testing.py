"""Synthetic phone-like footage for tests and local runs.

Every frame carries its own frame index as a 12-bit row of white/black
blocks along the top edge, so a test can decode any rendered frame and know
exactly which source frame it came from. Audio carries a 1 kHz beep at the
start of every whole second.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

BITS = 12
CODE_BLOCKS = BITS + 2  # A white and a black guard block bracket the code.


@dataclass(frozen=True)
class Fixture:
    name: str
    description: str
    args: tuple[str, ...]
    supported: bool = True
    display_rotation: int | None = None
    """Stamp a display-matrix rotation afterwards, the way phones store portrait video."""


def _code_filter(width: int, height: int) -> str:
    """drawbox chain that paints the frame index as binary blocks across the top."""
    block = width // CODE_BLOCKS
    size = min(block, height // 6)
    boxes = [
        f"drawbox=x=0:y=0:w={block}:h={size}:color=white:t=fill",
        f"drawbox=x={block}:y=0:w={block * (BITS + 1)}:h={size}:color=black:t=fill",
    ]
    for bit in range(BITS):
        x = block * (bit + 1)
        boxes.append(
            f"drawbox=x={x}:y=0:w={block}:h={size}:color=white:t=fill:enable='eq(mod(floor(n/{2**bit}),2),1)'"
        )
    return ",".join(boxes)


def _video(width: int, height: int, rate: str, seconds: float, extra: str = "") -> list[str]:
    label = "drawtext=text='%{n}':fontsize=" + str(height // 8) + ":fontcolor=yellow:x=w/2:y=h/2"
    chain = f"testsrc2=s={width}x{height}:r={rate}:d={seconds},{_code_filter(width, height)},{label}"
    if extra:
        chain += "," + extra
    return ["-f", "lavfi", "-i", chain]


def _beeps(seconds: float) -> list[str]:
    return [
        "-f",
        "lavfi",
        "-i",
        f"sine=f=1000:r=48000:d={seconds},volume='if(lt(mod(t,1),0.1),0.5,0)':eval=frame,"
        "aformat=channel_layouts=stereo",
    ]


H264 = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-g", "60")
AAC = ("-c:a", "aac", "-b:a", "128k")

FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        "landscape_h264_30.mp4",
        "1080p30 H.264 + AAC, the common case",
        (*_video(1920, 1080, "30", 12), *_beeps(12), *H264, *AAC, "-shortest"),
    ),
    Fixture(
        "portrait_rotated.mov",
        "Phone portrait: stored 1920x1080 with a 90 degree display rotation",
        (*_video(1920, 1080, "30", 8), *_beeps(8), *H264, *AAC, "-shortest"),
        display_rotation=90,
    ),
    Fixture(
        "vfr_h264.mp4",
        "Variable frame rate: 30 fps for 3 s, then 10 fps",
        (
            *_video(1280, 720, "30", 9, "select='lt(t\\,3)+not(mod(n\\,3))'"),
            *_beeps(9),
            *H264,
            *AAC,
            "-fps_mode",
            "vfr",
            "-shortest",
        ),
    ),
    Fixture(
        "hevc_hlg_2997.mov",
        "iPhone-style HEVC 10-bit HLG at 29.97 fps",
        (
            *_video(1920, 1080, "30000/1001", 6),
            *_beeps(6),
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p10le",
            "-tag:v",
            "hvc1",
            "-x265-params",
            "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "arib-std-b67",
            "-colorspace",
            "bt2020nc",
            *AAC,
            "-shortest",
        ),
    ),
    Fixture(
        "silent_720p_25.mp4",
        "No audio track at all, 25 fps",
        (*_video(1280, 720, "25", 6), *H264, "-an"),
    ),
    Fixture(
        "unsupported_mpeg4.mp4",
        "MPEG-4 Part 2 video, which the validator must reject",
        (*_video(640, 360, "30", 2), "-c:v", "mpeg4", "-an"),
        supported=False,
    ),
)


def generate(out_dir: Path, only: list[str] | None = None) -> list[Path]:
    """Render fixtures into ``out_dir``; existing files are kept."""
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for fx in FIXTURES:
        if only and fx.name not in only:
            continue
        path = out_dir / fx.name
        if not path.exists():
            tmp = path.with_name(f".tmp-{fx.name}")
            ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            subprocess.run([*ff, *fx.args, str(tmp)], check=True)
            if fx.display_rotation is not None:
                rotated = path.with_name(f".rot-{fx.name}")
                subprocess.run(
                    [
                        *ff,
                        "-display_rotation:v:0",
                        str(fx.display_rotation),
                        "-i",
                        str(tmp),
                        "-c",
                        "copy",
                        str(rotated),
                    ],
                    check=True,
                )
                rotated.replace(tmp)
            tmp.rename(path)
        made.append(path)
    return made


def read_frame_codes(video: Path, *, frame_rate: str | None = None) -> list[int]:
    """Decode every frame of ``video`` and return the embedded source frame index per frame.

    The code row must be unscaled relative to the frame width (it is, under
    "contain" fit for a landscape source on a landscape canvas).
    """
    probe = (
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .split(",")
    )
    width, height = int(probe[0]), int(probe[1])
    scaled_w = CODE_BLOCKS * 8
    vf = f"crop={width}:{max(2, height // 24)}:0:0,scale={scaled_w}:1:flags=area,format=gray"
    if frame_rate:
        vf = f"fps={frame_rate}," + vf
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vf", vf, "-f", "rawvideo", "-"],
        check=True,
        capture_output=True,
    ).stdout
    codes = []
    for row in range(len(raw) // scaled_w):
        px = raw[row * scaled_w : (row + 1) * scaled_w]
        bits = [px[(b + 1) * 8 + 4] > 128 for b in range(BITS)]
        codes.append(sum(1 << i for i, on in enumerate(bits) if on))
    return codes
