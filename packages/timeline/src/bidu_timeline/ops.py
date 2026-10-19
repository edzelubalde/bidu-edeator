"""Typed storyboard edits (spec section 8, "Edit concurrency").

``apply_ops`` is pure: it takes a timeline and returns a new one. The API
validates the result and commits it as a new immutable EditVersion; undo is
a new version that copies an ancestor's timeline, so no op needs an inverse.

Layout model: V1 is the story spine and is always laid out end to end, with
beats in order and each beat's clips in order. Everything else (overlays,
linked and unlinked source audio, captions) is anchored to the V1 clip it sat
on before the edit and moves with it. Music on A2 is not anchored; it is
clamped to the new duration.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from .model import AudioClip, AudioTrack, Caption, Timeline, VideoClip, VideoTrack
from .rational import RationalTime


class OpError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class MoveCard(BaseModel):
    op: Literal["move_card"] = "move_card"
    beat_id: str
    to_index: int = Field(ge=0)


class TrimClip(BaseModel):
    op: Literal["trim_clip"] = "trim_clip"
    clip_id: str
    source_in: RationalTime
    source_duration: RationalTime


class SwapMoment(BaseModel):
    op: Literal["swap_moment"] = "swap_moment"
    clip_id: str
    asset_id: str
    moment_id: str | None
    source_in: RationalTime
    source_duration: RationalTime


class SetLock(BaseModel):
    op: Literal["set_lock"] = "set_lock"
    clip_id: str
    source: bool | None = None
    position: bool | None = None


class RemoveCard(BaseModel):
    op: Literal["remove_card"] = "remove_card"
    beat_id: str


class RemoveClip(BaseModel):
    op: Literal["remove_clip"] = "remove_clip"
    clip_id: str


class ExcludeMoment(BaseModel):
    op: Literal["exclude_moment"] = "exclude_moment"
    moment_id: str


EditOp = Annotated[
    MoveCard | TrimClip | SwapMoment | SetLock | RemoveCard | RemoveClip | ExcludeMoment,
    Field(discriminator="op"),
]


class OpsResult(BaseModel):
    timeline: Timeline
    excluded_moment_ids: list[str] = []
    """Moments the caller must add to the project's exclusion list."""


_OPS = TypeAdapter(list[EditOp])


def apply_ops(timeline: Timeline, ops: list[EditOp] | list[dict]) -> OpsResult:
    """Apply ops in order. Accepts op models or their JSON-shaped dicts."""
    ops = _OPS.validate_python(ops)
    state = _State.from_timeline(timeline)
    excluded: list[str] = []
    for op in ops:
        match op:
            case MoveCard():
                state.move_card(op.beat_id, op.to_index)
            case TrimClip():
                state.retime(op.clip_id, None, None, op.source_in, op.source_duration)
            case SwapMoment():
                state.retime(op.clip_id, op.asset_id, op.moment_id, op.source_in, op.source_duration)
            case SetLock():
                state.set_lock(op.clip_id, op.source, op.position)
            case RemoveCard():
                state.remove_v1([c.id for c in state.v1 if c.beat_id == op.beat_id], op.beat_id)
            case RemoveClip():
                state.remove_clip(op.clip_id)
            case ExcludeMoment():
                state.exclude_moment(op.moment_id)
                excluded.append(op.moment_id)
    return OpsResult(timeline=state.to_timeline(), excluded_moment_ids=excluded)


class _State:
    """Mutable working copy used while ops apply, then laid out once."""

    def __init__(self, base: Timeline):
        self.base = base
        self.v1: list[VideoClip] = sorted(base.video("V1"), key=lambda c: c.timeline_start_frame)
        self.v2: list[VideoClip] = list(base.video("V2"))
        self.a1: list[AudioClip] = list(base.audio("A1"))
        self.a2: list[AudioClip] = list(base.audio("A2"))
        self.captions: list[Caption] = list(base.captions)
        # Anchor everything to a V1 clip, using positions before any edit.
        self.anchor: dict[str, str] = {}
        self.offset: dict[str, int] = {}
        for item in [*self.v2, *self.a1]:
            self._anchor(item.id, item.timeline_start_frame, getattr(item, "linked_video_clip_id", None))
        for cap in self.captions:
            self._anchor(cap.id, cap.start_frame, None)

    @classmethod
    def from_timeline(cls, timeline: Timeline) -> _State:
        return cls(timeline)

    def _anchor(self, item_id: str, start: int, linked_to: str | None) -> None:
        parent: VideoClip | None = None
        if linked_to is not None:
            parent = next((c for c in self.v1 if c.id == linked_to), None)
            if parent is None and linked_to in self.anchor:
                parent = next(c for c in self.v1 if c.id == self.anchor[linked_to])
        if parent is None:
            parent = next(
                (c for c in self.v1 if c.timeline_start_frame <= start < c.timeline_end_frame),
                None,
            )
        if parent is not None:
            self.anchor[item_id] = parent.id
            self.offset[item_id] = start - parent.timeline_start_frame

    # --- operations -------------------------------------------------------

    def _find_video(self, clip_id: str) -> tuple[list[VideoClip], int]:
        for track in (self.v1, self.v2):
            for i, c in enumerate(track):
                if c.id == clip_id:
                    return track, i
        raise OpError("CLIP_NOT_FOUND", f"No video clip {clip_id}")

    def move_card(self, beat_id: str, to_index: int) -> None:
        order: list[str] = []
        for c in self.v1:
            if c.beat_id not in order:
                order.append(c.beat_id)
        if beat_id not in order:
            raise OpError("BEAT_NOT_FOUND", f"No card {beat_id}")
        order.remove(beat_id)
        order.insert(min(to_index, len(order)), beat_id)
        rank = {b: i for i, b in enumerate(order)}
        # Stable sort keeps each beat's own clip order.
        self.v1.sort(key=lambda c: rank[c.beat_id])

    def retime(
        self,
        clip_id: str,
        asset_id: str | None,
        moment_id: str | None,
        source_in: RationalTime,
        source_duration: RationalTime,
    ) -> None:
        track, i = self._find_video(clip_id)
        clip = track[i]
        if clip.locks.source:
            raise OpError("CLIP_LOCKED", f"{clip_id} is locked; unlock it before changing it")
        if source_duration.value <= 0:
            raise OpError("NON_POSITIVE_SOURCE", "Source duration must be positive")
        frames = self.base.timebase.seconds_to_frames(source_duration.seconds)
        if frames <= 0:
            raise OpError("TOO_SHORT", "Clip would be shorter than one frame")
        update: dict = {
            "source_in": source_in,
            "source_duration": source_duration,
            "duration_frames": frames,
        }
        if asset_id is not None:
            update["asset_id"] = asset_id
            update["moment_id"] = moment_id
        track[i] = clip.model_copy(update=update)
        # Linked audio follows its picture exactly.
        for j, a in enumerate(self.a1):
            if a.linked_video_clip_id == clip_id:
                self.a1[j] = a.model_copy(
                    update={
                        "asset_id": update.get("asset_id", a.asset_id),
                        "source_in": source_in,
                        "source_duration": source_duration,
                        "duration_frames": frames,
                        "fade_in_frames": min(a.fade_in_frames, frames // 2),
                        "fade_out_frames": min(a.fade_out_frames, frames // 2),
                    }
                )

    def set_lock(self, clip_id: str, source: bool | None, position: bool | None) -> None:
        track, i = self._find_video(clip_id)
        locks = track[i].locks.model_copy(
            update={k: v for k, v in {"source": source, "position": position}.items() if v is not None}
        )
        track[i] = track[i].model_copy(update={"locks": locks})

    def remove_v1(self, clip_ids: list[str], label: str) -> None:
        if not clip_ids:
            raise OpError("BEAT_NOT_FOUND", f"No card {label}")
        gone = set(clip_ids)
        self.v1 = [c for c in self.v1 if c.id not in gone]
        dropped = {k for k, v in self.anchor.items() if v in gone}
        self.v2 = [c for c in self.v2 if c.id not in dropped]
        self.a1 = [c for c in self.a1 if c.id not in dropped and c.linked_video_clip_id not in gone]
        self.captions = [c for c in self.captions if c.id not in dropped]

    def remove_clip(self, clip_id: str) -> None:
        if any(c.id == clip_id for c in self.v1):
            self.remove_v1([clip_id], clip_id)
            return
        before = len(self.v2) + len(self.a1) + len(self.a2)
        self.v2 = [c for c in self.v2 if c.id != clip_id]
        self.a1 = [c for c in self.a1 if c.id != clip_id and c.linked_video_clip_id != clip_id]
        self.a2 = [c for c in self.a2 if c.id != clip_id]
        if len(self.v2) + len(self.a1) + len(self.a2) == before:
            raise OpError("CLIP_NOT_FOUND", f"No clip {clip_id}")

    def exclude_moment(self, moment_id: str) -> None:
        locked = [c.id for c in self.v1 + self.v2 if c.moment_id == moment_id and c.locks.source]
        if locked:
            raise OpError("CLIP_LOCKED", f"Moment {moment_id} is locked in {', '.join(locked)}")
        v1_ids = [c.id for c in self.v1 if c.moment_id == moment_id]
        if v1_ids:
            self.remove_v1(v1_ids, moment_id)
        for c in [c for c in self.v2 if c.moment_id == moment_id]:
            self.remove_clip(c.id)

    # --- layout -----------------------------------------------------------

    def to_timeline(self) -> Timeline:
        new_start: dict[str, int] = {}
        new_end: dict[str, int] = {}
        cursor = 0
        laid_v1: list[VideoClip] = []
        for c in self.v1:
            laid_v1.append(c.model_copy(update={"timeline_start_frame": cursor}))
            new_start[c.id] = cursor
            new_end[c.id] = cursor + c.duration_frames
            cursor += c.duration_frames
        duration = cursor

        def place(item_id: str) -> tuple[int, int] | None:
            parent = self.anchor.get(item_id)
            if parent is None or parent not in new_start:
                return None
            return new_start[parent] + self.offset.get(item_id, 0), new_end[parent]

        rate = self.base.timebase
        laid_v2 = [c for c in (_fit(c, place(c.id), rate) for c in self.v2) if c]
        laid_video = {c.id: c for c in laid_v1 + laid_v2}
        laid_a1 = []
        for a in self.a1:
            if a.linked_video_clip_id is not None:
                # Hard sync with its picture, wherever that picture went.
                v = laid_video.get(a.linked_video_clip_id)
                if v is not None:
                    synced = a.model_copy(update={"timeline_start_frame": v.timeline_start_frame})
                    laid_a1.append(_shorten(synced, v.duration_frames, rate))
                continue
            placed = _fit(a, place(a.id), rate)
            if placed:
                laid_a1.append(placed)
        laid_a2 = []
        for m in self.a2:
            end = min(m.timeline_end_frame, duration)
            if end > m.timeline_start_frame:
                laid_a2.append(_shorten(m, end - m.timeline_start_frame, rate))
        laid_caps = []
        for cap in self.captions:
            spot = place(cap.id)
            if spot is None:
                continue
            start, limit = spot
            end = min(start + (cap.end_frame - cap.start_frame), limit)
            if end > start:
                laid_caps.append(cap.model_copy(update={"start_frame": start, "end_frame": end}))

        return self.base.model_copy(
            update={
                "duration_frames": duration,
                "tracks": [
                    VideoTrack(id="V1", clips=laid_v1),
                    VideoTrack(id="V2", clips=sorted(laid_v2, key=_start)),
                    AudioTrack(id="A1", clips=sorted(laid_a1, key=_start)),
                    AudioTrack(id="A2", clips=laid_a2),
                ],
                "captions": sorted(laid_caps, key=lambda c: c.start_frame),
            }
        )


def _start(c: VideoClip | AudioClip) -> int:
    return c.timeline_start_frame


def _shorten(clip, frames: int, rate):
    """Cut a clip's tail so it lasts ``frames``, keeping source and timeline in step."""
    if frames >= clip.duration_frames:
        return clip
    src_rate = clip.source_duration.rate
    update = {
        "duration_frames": frames,
        "source_duration": RationalTime.from_seconds(rate.frames_to_seconds(frames), src_rate),
    }
    if isinstance(clip, AudioClip):
        update["fade_in_frames"] = min(clip.fade_in_frames, frames)
        update["fade_out_frames"] = min(clip.fade_out_frames, frames - update["fade_in_frames"])
    return clip.model_copy(update=update)


def _fit(clip, spot: tuple[int, int] | None, rate):
    """Move an anchored clip to its new spot and trim anything past its anchor's end."""
    if spot is None:
        return None
    start, limit = spot
    if start >= limit:
        return None
    moved = clip.model_copy(update={"timeline_start_frame": start})
    return _shorten(moved, min(clip.duration_frames, limit - start), rate)
