"""Canonical timeline contract (spec section 7), schema version 1.0.

The constrained v1 edit model: one primary video track (V1), one supporting
overlay track (V2), source audio (A1), optional music (A2) and captions.
Every position is an integer frame at the project timebase.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .rational import Rate, RationalTime

SCHEMA_VERSION = "1.0"

TrackId = Literal["V1", "V2", "A1", "A2"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Canvas(_Model):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Locks(_Model):
    source: bool = False
    """The moment and its trims survive regeneration."""
    position: bool = False
    """The clip also keeps its place in the story on regeneration."""


class _Clip(_Model):
    id: str
    asset_id: str
    beat_id: str
    timeline_start_frame: int = Field(ge=0)
    duration_frames: int = Field(gt=0)
    source_in: RationalTime
    source_duration: RationalTime

    @property
    def timeline_end_frame(self) -> int:
        return self.timeline_start_frame + self.duration_frames

    @property
    def source_out_seconds(self) -> Fraction:
        return self.source_in.seconds + self.source_duration.seconds


class VideoClip(_Clip):
    moment_id: str | None = None
    speed: Literal[1] = 1
    fit: Literal["contain", "cover"] = "contain"
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    locks: Locks = Field(default_factory=Locks)


class AudioClip(_Clip):
    linked_video_clip_id: str | None = None
    role: Literal["dialogue", "ambience", "music"] = "dialogue"
    gain_db: float = Field(default=0.0, ge=-60.0, le=12.0)
    fade_in_frames: int = Field(default=0, ge=0)
    fade_out_frames: int = Field(default=0, ge=0)
    loop: bool = False
    """Music only: loop the source to fill the clip."""


class VideoTrack(_Model):
    id: Literal["V1", "V2"]
    kind: Literal["video"] = "video"
    clips: list[VideoClip] = Field(default_factory=list)


class AudioTrack(_Model):
    id: Literal["A1", "A2"]
    kind: Literal["audio"] = "audio"
    clips: list[AudioClip] = Field(default_factory=list)


Track = Annotated[VideoTrack | AudioTrack, Field(discriminator="kind")]


class Caption(_Model):
    id: str
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    text: str
    language: str | None = None


class Provenance(_Model):
    proposal_id: str | None = None
    planner_run_id: str | None = None


class Timeline(_Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    project_id: str
    edit_version_id: str
    timebase: Rate
    canvas: Canvas
    duration_frames: int = Field(ge=0)
    tracks: list[Track]
    captions: list[Caption] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)

    def track(self, track_id: TrackId) -> VideoTrack | AudioTrack | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    def video(self, track_id: Literal["V1", "V2"]) -> list[VideoClip]:
        t = self.track(track_id)
        return list(t.clips) if isinstance(t, VideoTrack) else []

    def audio(self, track_id: Literal["A1", "A2"]) -> list[AudioClip]:
        t = self.track(track_id)
        return list(t.clips) if isinstance(t, AudioTrack) else []

    def all_clips(self) -> list[VideoClip | AudioClip]:
        return [c for t in self.tracks for c in t.clips]

    def beat_order(self) -> list[str]:
        """Beats in the order their first V1 clip appears."""
        order: list[str] = []
        for clip in sorted(self.video("V1"), key=lambda c: c.timeline_start_frame):
            if clip.beat_id not in order:
                order.append(clip.beat_id)
        return order

    @property
    def duration_seconds(self) -> Fraction:
        return self.timebase.frames_to_seconds(self.duration_frames)


def empty_timeline(
    project_id: str,
    edit_version_id: str,
    timebase: Rate | None = None,
    canvas: Canvas | None = None,
) -> Timeline:
    return Timeline(
        project_id=project_id,
        edit_version_id=edit_version_id,
        timebase=timebase or Rate(numerator=30),
        canvas=canvas or Canvas(width=1920, height=1080),
        duration_frames=0,
        tracks=[
            VideoTrack(id="V1"),
            VideoTrack(id="V2"),
            AudioTrack(id="A1"),
            AudioTrack(id="A2"),
        ],
    )
