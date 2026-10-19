"""Render a validated timeline to MP4 (spec section 9).

The picture is cut into segments at every V1 boundary. Each segment decodes
its exact source range from the original (accurate seek, never keyframe
snapping), applies rotation/tone mapping once, fits it to the canvas,
composites any V2 overlay, and encodes exactly ``duration_frames`` frames.
Segments are cached by a hash of everything that affects their pixels, so
after a trim only the changed segments re-render. Audio is mixed in one pass
over the whole timeline and muxed onto the concatenated picture.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel

from bidu_timeline import AudioClip, SourceInfo, Timeline, VideoClip, require_valid

from .ffmpeg import run_ffmpeg
from .normalize import fit_filter, sdr_filter
from .probe import Probe

RENDER_VERSION = "render-1"


class RenderProfile(BaseModel):
    name: str
    width: int
    height: int
    crf: int
    preset: str
    audio_bitrate: str = "192k"
    loudness_lufs: float = -16.0
    true_peak_db: float = -1.0


PREVIEW = RenderProfile(
    name="preview", width=1280, height=720, crf=26, preset="veryfast", audio_bitrate="128k"
)
DELIVERY = RenderProfile(name="delivery", width=1920, height=1080, crf=19, preset="medium")
PROFILES = {p.name: p for p in (PREVIEW, DELIVERY)}

PAD_COLOR = "0x111111"


@dataclass(frozen=True)
class RenderSource:
    path: Path
    probe: Probe

    @property
    def info(self) -> SourceInfo:
        return SourceInfo(
            duration_seconds=self.probe.usable_duration_seconds,
            has_audio=self.probe.audio is not None,
        )


@dataclass(frozen=True)
class Segment:
    index: int
    start_frame: int
    frames: int
    args: tuple[str, ...]
    key: str


class RenderResult(BaseModel):
    output: Path
    segments_rendered: int
    segments_reused: int


def _secs(x: Fraction) -> str:
    return f"{float(x):.6f}"


def _input(src: RenderSource, start: Fraction, duration: Fraction) -> list[str]:
    # Canonical time -> ffmpeg seek time; pad duration by a frame so the
    # decoder never runs dry before -frames:v is reached.
    seek = start + src.probe.seek_offset_seconds
    return [
        "-ss",
        _secs(max(seek, Fraction(0))),
        "-t",
        _secs(duration + Fraction(1, 10)),
        "-i",
        str(src.path),
    ]


def plan_segments(
    timeline: Timeline, sources: dict[str, RenderSource], profile: RenderProfile
) -> list[Segment]:
    rate = timeline.timebase
    fps = f"{rate.numerator}/{rate.denominator}"
    v1 = sorted(timeline.video("V1"), key=lambda c: c.timeline_start_frame)
    v2 = timeline.video("V2")
    encode = [
        "-c:v",
        "libx264",
        "-preset",
        profile.preset,
        "-crf",
        str(profile.crf),
        "-pix_fmt",
        "yuv420p",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-g",
        str(rate.numerator * 2),
        "-an",
    ]

    spans: list[tuple[int, int, VideoClip | None]] = []
    cursor = 0
    for clip in v1:
        if clip.timeline_start_frame > cursor:
            spans.append((cursor, clip.timeline_start_frame - cursor, None))
        spans.append((clip.timeline_start_frame, clip.duration_frames, clip))
        cursor = clip.timeline_end_frame
    if cursor < timeline.duration_frames:
        spans.append((cursor, timeline.duration_frames - cursor, None))

    segments = []
    for index, (start, frames, base) in enumerate(spans):
        seg_dur = rate.frames_to_seconds(frames)
        inputs: list[str] = []
        graph: list[str] = []
        identity: dict = {"v": RENDER_VERSION, "profile": profile.model_dump(), "frames": frames, "rate": fps}
        w, h = profile.width, profile.height
        if base is None:
            inputs += ["-f", "lavfi", "-i", f"color=c={PAD_COLOR}:s={w}x{h}:r={fps}"]
            graph.append("[0:v]setsar=1[base]")
            identity["base"] = None
        else:
            src = sources[base.asset_id]
            inputs += _input(src, base.source_in.seconds, seg_dur)
            graph.append(
                f"[0:v]{sdr_filter(src.probe)},{fit_filter(w, h, base.fit, PAD_COLOR)},"
                f"fps={fps},tpad=stop_mode=clone:stop_duration=1,setpts=PTS-STARTPTS[base]"
            )
            identity["base"] = _clip_identity(base, src)

        last = "base"
        overlays = [o for o in v2 if o.timeline_start_frame < start + frames and o.timeline_end_frame > start]
        for n, o in enumerate(sorted(overlays, key=lambda c: c.timeline_start_frame), start=1):
            src = sources[o.asset_id]
            lead_frames = max(0, start - o.timeline_start_frame)
            delay_frames = max(0, o.timeline_start_frame - start)
            visible = min(o.timeline_end_frame, start + frames) - max(o.timeline_start_frame, start)
            o_in = o.source_in.seconds + rate.frames_to_seconds(lead_frames)
            inputs += _input(src, o_in, rate.frames_to_seconds(visible))
            delay = _secs(rate.frames_to_seconds(delay_frames))
            end = _secs(rate.frames_to_seconds(delay_frames + visible))
            alpha = "" if o.opacity >= 1 else f",format=yuva420p,colorchannelmixer=aa={o.opacity}"
            graph.append(
                f"[{n}:v]{sdr_filter(src.probe)},{fit_filter(w, h, o.fit, PAD_COLOR)},fps={fps},"
                f"setpts=PTS-STARTPTS+{delay}/TB{alpha}[o{n}]"
            )
            graph.append(f"[{last}][o{n}]overlay=eof_action=pass:enable='between(t,{delay},{end})'[m{n}]")
            last = f"m{n}"
            identity.setdefault("overlays", []).append(
                {
                    "clip": _clip_identity(o, src),
                    "lead": lead_frames,
                    "delay": delay_frames,
                    "visible": visible,
                }
            )
        graph.append(f"[{last}]format=yuv420p[vout]")
        args = (
            *inputs,
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[vout]",
            "-frames:v",
            str(frames),
            "-r",
            fps,
            *encode,
        )
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
        segments.append(Segment(index=index, start_frame=start, frames=frames, args=args, key=key))
    return segments


def _clip_identity(clip: VideoClip, src: RenderSource) -> dict:
    return {
        "path": str(src.path),
        "size": src.probe.byte_size,
        "in": str(clip.source_in.seconds),
        "dur": str(clip.source_duration.seconds),
        "fit": clip.fit,
        "opacity": clip.opacity,
    }


def audio_args(
    timeline: Timeline,
    sources: dict[str, RenderSource],
    profile: RenderProfile,
    out: Path,
    *,
    music: dict[str, Path] | None = None,
) -> list[str]:
    """One mixing pass: place every A1/A2 clip, duck music under dialogue, normalize loudness."""
    rate = timeline.timebase
    total = rate.frames_to_seconds(timeline.duration_frames)
    inputs: list[str] = []
    graph: list[str] = []
    speech: list[str] = []
    beds: list[str] = []
    music_paths = music or {}

    clips: list[tuple[AudioClip, str]] = [(c, "A1") for c in timeline.audio("A1")]
    clips += [(c, "A2") for c in timeline.audio("A2")]
    for n, (clip, track) in enumerate(clips):
        dur = rate.frames_to_seconds(clip.duration_frames)
        if clip.asset_id in sources:
            inputs += _input(sources[clip.asset_id], clip.source_in.seconds, dur)
        elif clip.asset_id in music_paths:
            loop = ["-stream_loop", "-1"] if clip.loop else []
            inputs += [*loop, "-ss", _secs(clip.source_in.seconds), "-i", str(music_paths[clip.asset_id])]
        else:
            raise KeyError(f"No source for audio clip {clip.id}")
        delay_ms = int(rate.frames_to_seconds(clip.timeline_start_frame) * 1000)
        chain = [
            f"[{n}:a]aresample=48000",
            "aformat=sample_fmts=fltp:channel_layouts=stereo",
            f"atrim=0:{_secs(dur)}",
            "asetpts=PTS-STARTPTS",
            f"volume={clip.gain_db}dB",
        ]
        if clip.fade_in_frames:
            chain.append(f"afade=t=in:d={_secs(rate.frames_to_seconds(clip.fade_in_frames))}")
        if clip.fade_out_frames:
            fo = rate.frames_to_seconds(clip.fade_out_frames)
            chain.append(f"afade=t=out:st={_secs(dur - fo)}:d={_secs(fo)}")
        chain.append(f"adelay={delay_ms}:all=1")
        label = f"a{n}"
        graph.append(",".join(chain) + f"[{label}]")
        (speech if track == "A1" and clip.role == "dialogue" else beds).append(label)

    music_labels = [f"a{i}" for i, (c, _) in enumerate(clips) if c.role == "music"]
    other_beds = [b for b in beds if b not in music_labels]
    silence_index = len(clips)
    used_silence = False

    def mix(labels: list[str], name: str) -> str:
        nonlocal used_silence
        if not labels:
            used_silence = True
            graph.append(f"[{silence_index}:a]anull[{name}]")
        elif len(labels) == 1:
            graph.append(f"[{labels[0]}]anull[{name}]")
        else:
            srcs = "".join(f"[{x}]" for x in labels)
            graph.append(f"{srcs}amix=inputs={len(labels)}:normalize=0:duration=longest[{name}]")
        return name

    dialog = mix(speech + other_beds, "dialog")
    if music_labels:
        mus = mix(music_labels, "music")
        graph.append(f"[{dialog}]asplit=2[dmix][dkey]")
        graph.append(f"[{mus}][dkey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[ducked]")
        graph.append("[dmix][ducked]amix=inputs=2:normalize=0:duration=first[mixed]")
        final_in = "mixed"
    else:
        final_in = dialog
    graph.append(
        f"[{final_in}]apad,atrim=0:{_secs(total)},"
        f"loudnorm=I={profile.loudness_lufs}:TP={profile.true_peak_db}:LRA=11,"
        f"aresample=48000[aout]"
    )
    if used_silence:
        inputs += ["-f", "lavfi", "-t", _secs(total), "-i", "anullsrc=r=48000:cl=stereo"]
    return [*inputs, "-filter_complex", ";".join(graph), "-map", "[aout]", "-c:a", "pcm_s16le", str(out)]


def render(
    timeline: Timeline,
    sources: dict[str, RenderSource],
    out: Path,
    *,
    profile: RenderProfile = PREVIEW,
    cache_dir: Path,
    music: dict[str, Path] | None = None,
    workers: int = 2,
) -> RenderResult:
    """Render ``timeline``; raises InvalidTimeline before doing any work if it is invalid."""
    infos = {k: v.info for k, v in sources.items()}
    for asset_id in music or {}:
        infos.setdefault(asset_id, SourceInfo(duration_seconds=Fraction(10**9)))
    require_valid(timeline, infos)

    cache_dir.mkdir(parents=True, exist_ok=True)
    segments = plan_segments(timeline, sources, profile)
    reused = 0
    todo = []
    for seg in segments:
        if (cache_dir / f"{seg.key}.mp4").exists():
            reused += 1
        else:
            todo.append(seg)

    def build(seg: Segment) -> None:
        dest = cache_dir / f"{seg.key}.mp4"
        tmp = cache_dir / f".tmp-{seg.key}.mp4"
        run_ffmpeg([*seg.args, str(tmp)], code="RENDER_FAILED")
        tmp.replace(dest)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(build, todo))

    work = out.parent / f".work-{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    listing = work / "segments.txt"
    listing.write_text("".join(f"file '{(cache_dir / f'{s.key}.mp4').resolve()}'\n" for s in segments))
    audio = work / "mix.wav"
    run_ffmpeg(audio_args(timeline, sources, profile, audio, music=music), code="RENDER_FAILED")

    tmp_out = out.with_name(f".tmp-{out.name}")
    run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            profile.audio_bitrate,
            "-shortest",
            "-movflags",
            "+faststart",
            str(tmp_out),
        ],
        code="RENDER_FAILED",
    )
    tmp_out.replace(out)
    shutil.rmtree(work, ignore_errors=True)
    return RenderResult(output=out, segments_rendered=len(todo), segments_reused=reused)
