"""Helpers for assembling timelines from shot decisions (planner, tests, fixtures)."""

from __future__ import annotations

from fractions import Fraction

from .model import AudioClip, AudioTrack, Timeline, VideoClip, VideoTrack
from .rational import Rate, RationalTime


def primary_shot(
    timeline: Timeline,
    *,
    clip_id: str,
    asset_id: str,
    beat_id: str,
    source_in_seconds: Fraction,
    duration_seconds: Fraction,
    source_rate: Rate | None = None,
    moment_id: str | None = None,
    with_audio: bool = True,
    audio_fade_frames: int = 0,
) -> Timeline:
    """Append a V1 clip (and its linked A1 audio) at the end of the spine."""
    rate = timeline.timebase
    src_rate = source_rate or rate
    frames = rate.seconds_to_frames(duration_seconds)
    start = max((c.timeline_end_frame for c in timeline.video("V1")), default=0)
    source_in = RationalTime.from_seconds(source_in_seconds, src_rate)
    source_duration = RationalTime.from_seconds(rate.frames_to_seconds(frames), src_rate)
    video = VideoClip(
        id=clip_id,
        asset_id=asset_id,
        moment_id=moment_id,
        beat_id=beat_id,
        timeline_start_frame=start,
        duration_frames=frames,
        source_in=source_in,
        source_duration=source_duration,
    )
    v1 = [*timeline.video("V1"), video]
    a1 = timeline.audio("A1")
    if with_audio:
        fade = min(audio_fade_frames, frames // 2)
        a1 = [
            *a1,
            AudioClip(
                id=f"{clip_id}_a",
                asset_id=asset_id,
                beat_id=beat_id,
                linked_video_clip_id=clip_id,
                timeline_start_frame=start,
                duration_frames=frames,
                source_in=source_in,
                source_duration=source_duration,
                fade_in_frames=fade,
                fade_out_frames=fade,
            ),
        ]
    return _replace(timeline, v1=v1, a1=a1, duration=max(timeline.duration_frames, start + frames))


def overlay_shot(
    timeline: Timeline,
    *,
    clip_id: str,
    asset_id: str,
    beat_id: str,
    timeline_start_frame: int,
    source_in_seconds: Fraction,
    duration_seconds: Fraction,
    source_rate: Rate | None = None,
    moment_id: str | None = None,
) -> Timeline:
    """Add supporting video on V2; dialogue under it keeps playing from A1."""
    rate = timeline.timebase
    src_rate = source_rate or rate
    frames = rate.seconds_to_frames(duration_seconds)
    clip = VideoClip(
        id=clip_id,
        asset_id=asset_id,
        moment_id=moment_id,
        beat_id=beat_id,
        timeline_start_frame=timeline_start_frame,
        duration_frames=frames,
        source_in=RationalTime.from_seconds(source_in_seconds, src_rate),
        source_duration=RationalTime.from_seconds(rate.frames_to_seconds(frames), src_rate),
    )
    return _replace(timeline, v2=[*timeline.video("V2"), clip])


def _replace(timeline: Timeline, *, v1=None, v2=None, a1=None, a2=None, duration=None) -> Timeline:
    return timeline.model_copy(
        update={
            "duration_frames": timeline.duration_frames if duration is None else duration,
            "tracks": [
                VideoTrack(id="V1", clips=timeline.video("V1") if v1 is None else v1),
                VideoTrack(id="V2", clips=timeline.video("V2") if v2 is None else v2),
                AudioTrack(id="A1", clips=timeline.audio("A1") if a1 is None else a1),
                AudioTrack(id="A2", clips=timeline.audio("A2") if a2 is None else a2),
            ],
        }
    )
