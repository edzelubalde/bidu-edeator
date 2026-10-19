"""Turn a shot plan into the canonical timeline the renderer accepts."""

from __future__ import annotations

from fractions import Fraction

from bidu_timeline import Locks, Provenance, Timeline, empty_timeline
from bidu_timeline.builders import overlay_shot, primary_shot

from .models import ShotPlan

AUDIO_FADE_FRAMES = 3
"""A tenth of a second at 30 fps: enough to avoid clicks at every cut."""


def assemble(
    plan: ShotPlan,
    *,
    project_id: str,
    edit_version_id: str,
    has_audio: dict[str, bool],
    planner_run_id: str | None = None,
) -> Timeline:
    t = empty_timeline(project_id, edit_version_id)
    rate = t.timebase
    last_primary: tuple[str, int] | None = None
    for i, shot in enumerate(plan.shots):
        clip_id = f"clip_{i + 1:03d}"
        if shot.overlay_offset_ms is not None:
            if last_primary is None:
                continue
            anchor_id, anchor_start = last_primary
            t = overlay_shot(
                t,
                clip_id=clip_id,
                asset_id=shot.asset_id,
                beat_id=shot.beat_id,
                moment_id=shot.moment_id,
                timeline_start_frame=anchor_start
                + rate.seconds_to_frames(Fraction(shot.overlay_offset_ms, 1000)),
                source_in_seconds=Fraction(shot.in_ms, 1000),
                duration_seconds=Fraction(shot.ms, 1000),
            )
            continue
        t = primary_shot(
            t,
            clip_id=clip_id,
            asset_id=shot.asset_id,
            beat_id=shot.beat_id,
            moment_id=shot.moment_id,
            source_in_seconds=Fraction(shot.in_ms, 1000),
            duration_seconds=Fraction(shot.ms, 1000),
            with_audio=has_audio.get(shot.asset_id, False),
            audio_fade_frames=AUDIO_FADE_FRAMES,
        )
        added = t.video("V1")[-1]
        last_primary = (added.id, added.timeline_start_frame)
        if shot.locked:
            v1 = t.video("V1")
            v1[-1] = added.model_copy(update={"locks": Locks(source=True)})
            t = t.model_copy(
                update={
                    "tracks": [
                        tr.model_copy(update={"clips": v1}) if tr.id == "V1" else tr for tr in t.tracks
                    ]
                }
            )

    # Overlays may run past a short anchor; clamp them so V2 never outlives its V1 shot.
    primaries = t.video("V1")
    v2 = []
    for clip in t.video("V2"):
        anchor = next(
            (
                p
                for p in primaries
                if p.timeline_start_frame <= clip.timeline_start_frame < p.timeline_end_frame
            ),
            None,
        )
        if anchor is None:
            continue
        frames = min(clip.duration_frames, anchor.timeline_end_frame - clip.timeline_start_frame)
        v2.append(
            clip.model_copy(
                update={
                    "duration_frames": frames,
                    "source_duration": clip.source_duration.model_copy(update={"value": frames}),
                }
            )
        )
    return t.model_copy(
        update={
            "tracks": [tr.model_copy(update={"clips": v2}) if tr.id == "V2" else tr for tr in t.tracks],
            "provenance": Provenance(proposal_id=plan.proposal_id, planner_run_id=planner_run_id),
        }
    )
