"""The storyboard view of an edit: sections (beats) of cards (shots), ready for the page."""

from __future__ import annotations

import subprocess
import uuid
from fractions import Fraction
from pathlib import Path

from bidu_analysis.models import Moment, TranscriptSegment
from bidu_media.render import RenderSource
from bidu_timeline import SourceInfo, Timeline, apply_ops, require_valid
from bidu_workflows import stages
from bidu_workflows.store import ProjectStore

from .projects import ProjectMeta


def load_timeline(store: ProjectStore, edit_id: str) -> Timeline:
    return Timeline.model_validate_json(store.edit_path(edit_id).read_text())


def _analysis(store: ProjectStore, asset_ids: set[str]):
    moments: dict[str, Moment] = {}
    transcript: list[TranscriptSegment] = []
    for a in asset_ids:
        try:
            an = stages.load_analysis(store, a)
        except FileNotFoundError:
            continue
        moments.update({m.moment_id: m for m in an.moments})
        transcript += an.transcript
    return moments, transcript


def _beat_titles(store: ProjectStore, timeline: Timeline) -> dict[str, tuple[str, str]]:
    pid = timeline.provenance.proposal_id
    path = store.root / "proposals" / f"{pid}.json"
    if not pid or not path.exists():
        return {}
    from bidu_analysis.planning import Proposal

    proposal = Proposal.model_validate_json(path.read_text())
    return {b.id: (b.title, b.purpose) for b in proposal.beats}


def build(store: ProjectStore, meta: ProjectMeta, base_url: str) -> dict | None:
    edit_id = meta.current_edit_id
    if edit_id is None:
        return None
    t = load_timeline(store, edit_id)
    rate = t.timebase
    assets = {c.asset_id for c in t.all_clips()}
    labels = {a: store.load_asset(a).label for a in assets}
    moments, transcript = _analysis(store, assets)
    titles = _beat_titles(store, t)

    def secs(frames: int) -> float:
        return round(float(rate.frames_to_seconds(frames)), 2)

    def card(clip, kind: str) -> dict:
        src_in = clip.source_in.seconds
        src_out = src_in + clip.source_duration.seconds
        m = moments.get(clip.moment_id or "")
        said = [
            s.text
            for s in transcript
            if s.asset_id == clip.asset_id
            and s.start_ms >= src_in * 1000 - 300
            and s.end_ms <= src_out * 1000 + 300
        ]
        mid = float((src_in + src_out) / 2)
        return {
            "clip_id": clip.id,
            "kind": kind,
            "asset_id": clip.asset_id,
            "file": labels[clip.asset_id],
            "start_s": secs(clip.timeline_start_frame),
            "duration_s": secs(clip.duration_frames),
            "source_in_s": round(float(src_in), 2),
            "source_out_s": round(float(src_out), 2),
            "thumb_url": f"{base_url}/thumbs/{clip.asset_id}?t={mid:.2f}",
            "source_url": f"{base_url}/assets/{clip.asset_id}/video",
            "why": m.observations[0] if m and m.observations else "",
            "quote": " ".join(said),
            "locked": bool(getattr(clip, "locks", None) and clip.locks.source),
            "moment_id": clip.moment_id,
        }

    beats = []
    overlays = t.video("V2")
    for beat_id in t.beat_order():
        cards = []
        for clip in [c for c in t.video("V1") if c.beat_id == beat_id]:
            cards.append(card(clip, "main"))
            for o in overlays:
                if clip.timeline_start_frame <= o.timeline_start_frame < clip.timeline_end_frame:
                    cards.append(card(o, "cutaway"))
        main = [c for c in t.video("V1") if c.beat_id == beat_id]
        title, purpose = titles.get(beat_id, (beat_id.replace("_", " ").title(), ""))
        beats.append(
            {
                "id": beat_id,
                "title": title,
                "purpose": purpose,
                "duration_s": secs(sum(c.duration_frames for c in main)),
                "shots": cards,
            }
        )
    return {
        "edit_id": edit_id,
        "version": len(meta.edit_ids),
        "can_undo": len(meta.edit_ids) > 1,
        "duration_s": secs(t.duration_frames),
        "preview_is_current": meta.preview_edit_id == edit_id,
        "beats": beats,
    }


def _sources(store: ProjectStore, timeline: Timeline) -> dict[str, SourceInfo]:
    out = {}
    for a in {c.asset_id for c in timeline.all_clips()}:
        rec = store.load_asset(a)
        out[a] = RenderSource(Path(rec.source_path), stages.load_derived(store, rec)[0]).info
    return out


def apply(store: ProjectStore, base_edit_id: str, ops: list[dict]) -> tuple[str, list[str]]:
    """Apply ops to an edit and save the result as a new immutable version."""
    t = load_timeline(store, base_edit_id)
    result = apply_ops(t, ops)
    new_id = f"edit_{uuid.uuid4().hex[:12]}"
    new = result.timeline.model_copy(update={"edit_version_id": new_id})
    sources = _sources(store, t)
    require_valid(new, sources)
    store.write_json(store.edit_path(new_id), new)
    return new_id, result.excluded_moment_ids


def copy_version(store: ProjectStore, edit_id: str) -> str:
    """Undo: a new version whose timeline is an earlier one's."""
    t = load_timeline(store, edit_id)
    new_id = f"edit_{uuid.uuid4().hex[:12]}"
    store.write_json(store.edit_path(new_id), t.model_copy(update={"edit_version_id": new_id}))
    return new_id


def thumbnail(store: ProjectStore, asset_id: str, seconds: float) -> Path:
    rec = store.load_asset(asset_id)
    _, derived = stages.load_derived(store, rec)
    ms = int(seconds * 1000)
    out = store.root / "cache" / "thumbs" / f"{asset_id}_{ms}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        at = derived.time_map.to_proxy(Fraction(ms, 1000))
        tmp = out.with_name(f".tmp-{uuid.uuid4().hex[:8]}-{out.name}")  # parallel requests for one thumb
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-ss",
                f"{float(at):.3f}",
                "-i",
                str(derived.proxy),
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                "-q:v",
                "4",
                str(tmp),
            ],
            check=True,
            timeout=60,
        )
        tmp.replace(out)
    return out
