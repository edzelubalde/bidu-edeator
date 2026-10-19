"""Timeline invariants (spec section 7, "Required invariants").

The renderer only accepts a timeline whose report has no errors. Warnings
(gaps, overlays over nothing) are surfaced to the creator, not blocked.
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel

from .model import AudioClip, Timeline, VideoClip


class SourceInfo(BaseModel):
    """What the validator needs to know about one project asset."""

    duration_seconds: Fraction
    has_audio: bool = True


class Issue(BaseModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    clip_id: str | None = None


class ValidationReport(BaseModel):
    issues: list[Issue] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}


class InvalidTimeline(ValueError):
    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("; ".join(f"{i.code}: {i.message}" for i in report.errors))


def validate_timeline(timeline: Timeline, sources: Mapping[str, SourceInfo]) -> ValidationReport:
    """Check every invariant. ``sources`` must contain only assets owned by the project."""
    issues: list[Issue] = []

    def err(code: str, message: str, clip_id: str | None = None) -> None:
        issues.append(Issue(code=code, severity="error", message=message, clip_id=clip_id))

    def warn(code: str, message: str, clip_id: str | None = None) -> None:
        issues.append(Issue(code=code, severity="warning", message=message, clip_id=clip_id))

    rate = timeline.timebase
    one_frame = rate.frame_seconds

    track_ids = [t.id for t in timeline.tracks]
    if len(set(track_ids)) != len(track_ids):
        err("DUPLICATE_TRACK", f"Track ids repeat: {track_ids}")
    if "V1" not in track_ids:
        err("MISSING_PRIMARY_TRACK", "Timeline has no V1 track")

    seen: set[str] = set()
    for clip in timeline.all_clips():
        if clip.id in seen:
            err("DUPLICATE_CLIP_ID", f"Clip id {clip.id} is used twice", clip.id)
        seen.add(clip.id)

    video_by_id: dict[str, VideoClip] = {
        c.id: c for c in timeline.video("V1") + timeline.video("V2")
    }

    for clip in timeline.all_clips():
        cid = clip.id
        src = sources.get(clip.asset_id)
        if src is None:
            err("ASSET_NOT_IN_PROJECT", f"Asset {clip.asset_id} is not a project asset", cid)
        if clip.source_duration.value <= 0:
            err("NON_POSITIVE_SOURCE", "Source duration must be positive", cid)
        if clip.source_in.value < 0:
            err("SOURCE_OUT_OF_BOUNDS", "Source in point is before the start of the file", cid)
        looping = isinstance(clip, AudioClip) and clip.loop
        if src is not None and not looping:
            # Allow half a source frame of slack for container rounding.
            slack = clip.source_in.rate.frame_seconds / 2
            if clip.source_out_seconds > src.duration_seconds + slack:
                err(
                    "SOURCE_OUT_OF_BOUNDS",
                    f"Source range ends at {float(clip.source_out_seconds):.3f}s but the file "
                    f"is {float(src.duration_seconds):.3f}s long",
                    cid,
                )
        if isinstance(clip, AudioClip) and src is not None and not src.has_audio:
            err("SOURCE_HAS_NO_AUDIO", f"Asset {clip.asset_id} has no audio stream", cid)
        if clip.timeline_end_frame > timeline.duration_frames:
            err(
                "OUTSIDE_OUTPUT",
                f"Clip ends at frame {clip.timeline_end_frame}, "
                f"after the timeline end {timeline.duration_frames}",
                cid,
            )
        if not looping:
            timeline_seconds = rate.frames_to_seconds(clip.duration_frames)
            if abs(timeline_seconds - clip.source_duration.seconds) > one_frame:
                err(
                    "DURATION_MISMATCH",
                    f"At speed 1 the source covers {float(clip.source_duration.seconds):.3f}s "
                    f"but the clip lasts {float(timeline_seconds):.3f}s",
                    cid,
                )
        if isinstance(clip, AudioClip):
            if clip.fade_in_frames + clip.fade_out_frames > clip.duration_frames:
                err("FADES_TOO_LONG", "Fades are longer than the clip", cid)
            if clip.linked_video_clip_id is not None:
                _check_link(clip, video_by_id, rate, err)

    for track in timeline.tracks:
        clips = sorted(track.clips, key=lambda c: c.timeline_start_frame)
        for a, b in zip(clips, clips[1:], strict=False):
            if b.timeline_start_frame < a.timeline_end_frame:
                err(
                    "TRACK_OVERLAP",
                    f"{a.id} and {b.id} overlap on {track.id}",
                    b.id,
                )

    v1 = sorted(timeline.video("V1"), key=lambda c: c.timeline_start_frame)
    cursor = 0
    for clip in v1:
        if clip.timeline_start_frame > cursor:
            warn(
                "GAP",
                f"V1 is empty from frame {cursor} to {clip.timeline_start_frame}",
                clip.id,
            )
        cursor = max(cursor, clip.timeline_end_frame)
    if v1 and cursor < timeline.duration_frames:
        warn("GAP", f"V1 is empty from frame {cursor} to the end")

    for clip in timeline.video("V2"):
        if not _covered_by_union(clip, v1):
            warn("OVERLAY_OVER_GAP", "Overlay extends over a gap in V1", clip.id)

    captions = sorted(timeline.captions, key=lambda c: c.start_frame)
    for cap in captions:
        if cap.end_frame <= cap.start_frame or cap.end_frame > timeline.duration_frames:
            err("CAPTION_OUT_OF_BOUNDS", f"Caption {cap.id} is outside the timeline")
    for a, b in zip(captions, captions[1:], strict=False):
        if b.start_frame < a.end_frame:
            err("CAPTION_OVERLAP", f"Captions {a.id} and {b.id} overlap")

    return ValidationReport(issues=issues)


def _check_link(clip: AudioClip, video_by_id: dict[str, VideoClip], rate, err) -> None:
    video = video_by_id.get(clip.linked_video_clip_id or "")
    if video is None:
        err("BROKEN_LINK", f"Linked video clip {clip.linked_video_clip_id} is missing", clip.id)
        return
    if video.asset_id != clip.asset_id:
        err("BROKEN_LINK", "Linked audio comes from a different asset than its video", clip.id)
        return
    # Sync holds when the source offset equals the timeline offset.
    source_offset = clip.source_in.seconds - video.source_in.seconds
    timeline_offset = rate.frames_to_seconds(clip.timeline_start_frame - video.timeline_start_frame)
    if abs(source_offset - timeline_offset) > rate.frame_seconds:
        err(
            "LINK_OUT_OF_SYNC",
            f"Audio is {float(source_offset - timeline_offset):+.3f}s out of sync with "
            f"{video.id}",
            clip.id,
        )


def _covered_by_union(clip: VideoClip, v1: list[VideoClip]) -> bool:
    cursor = clip.timeline_start_frame
    for p in v1:
        if p.timeline_start_frame <= cursor < p.timeline_end_frame:
            cursor = p.timeline_end_frame
        if cursor >= clip.timeline_end_frame:
            return True
    return cursor >= clip.timeline_end_frame


def require_valid(timeline: Timeline, sources: Mapping[str, SourceInfo]) -> ValidationReport:
    report = validate_timeline(timeline, sources)
    if not report.ok:
        raise InvalidTimeline(report)
    return report
