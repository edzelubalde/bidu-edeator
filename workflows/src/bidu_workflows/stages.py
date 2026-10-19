"""The pipeline stages as plain functions.

Temporal activities wrap these one to one (see ``activities.py``), and the CLI
calls them directly. Each stage is idempotent: its output is keyed by its
inputs, so running it twice returns the stored result.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from pydantic import BaseModel

from bidu_analysis.analyze import analyze_asset, input_hash
from bidu_analysis.models import AssetAnalysis
from bidu_analysis.planning import ChronologicalPlanner, LockedShot, assemble, solve
from bidu_analysis.transcribe import Transcriber, default_transcriber
from bidu_analysis.video_model import VideoModel, default_video_model
from bidu_media.errors import MediaError
from bidu_media.normalize import TRANSFORM_VERSION, Derived, normalize
from bidu_media.probe import Probe, probe
from bidu_media.render import PROFILES, RenderSource, render
from bidu_media.verify import verify_output
from bidu_timeline import Timeline, require_valid

from .store import AssetRecord, ProjectStore, sha256_file


class IngestResult(BaseModel):
    asset_id: str
    ok: bool
    reused: bool = False
    error: dict | None = None


class AnalyzeResult(BaseModel):
    asset_id: str
    run_id: str
    moments: int
    transcript_segments: int
    reused: bool = False


class PlanResult(BaseModel):
    edit_id: str
    proposal_id: str
    total_ms: int
    shots: int
    gaps: list[str]
    conflicts: list[str]


class RenderSummary(BaseModel):
    edit_id: str
    output: str
    profile: str
    segments_rendered: int
    segments_reused: int
    ok: bool
    errors: list[str]
    review: list[str]


def register_folder(store: ProjectStore, folder: Path) -> list[AssetRecord]:
    """Create asset records for every file in ``folder`` (the upload step, locally)."""
    known = {a.source_path: a for a in store.assets()}
    out = []
    for path in sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")):
        rec = known.get(str(path.resolve()))
        if rec is None:
            rec = AssetRecord(
                id=f"asset_{uuid.uuid4().hex[:12]}", label=path.name, source_path=str(path.resolve())
            )
            store.save_asset(rec)
        out.append(rec)
    return out


def _probe_path(store: ProjectStore, sha: str) -> Path:
    return store.derived_dir(sha, TRANSFORM_VERSION) / "probe.json"


def _derived_path(store: ProjectStore, sha: str) -> Path:
    return store.derived_dir(sha, TRANSFORM_VERSION) / "derived.json"


def ingest(store: ProjectStore, asset_id: str) -> IngestResult:
    """Validate, probe and normalize one asset. A bad file fails alone."""
    asset = store.load_asset(asset_id)
    src = Path(asset.source_path)
    try:
        sha = asset.sha256 or sha256_file(src)
        reused = _derived_path(store, sha).exists()
        if reused:
            p = Probe.model_validate_json(_probe_path(store, sha).read_text())
        else:
            p = probe(src)
            derived = normalize(src, p, store.derived_dir(sha, TRANSFORM_VERSION))
            # Publish the records only after every derived file exists.
            store.write_json(_probe_path(store, sha), p)
            store.write_json(_derived_path(store, sha), derived)
        asset = asset.model_copy(
            update={
                "sha256": sha,
                "status": "ready",
                "error": None,
                "capture_time": p.capture.get("com.apple.quicktime.creationdate")
                or p.capture.get("creation_time"),
            }
        )
        store.save_asset(asset)
        return IngestResult(asset_id=asset_id, ok=True, reused=reused)
    except MediaError as e:
        store.save_asset(
            asset.model_copy(update={"status": "needs_attention", "error": e.to_public(asset_id)})
        )
        return IngestResult(asset_id=asset_id, ok=False, error=e.to_public(asset_id))


def load_derived(store: ProjectStore, asset: AssetRecord) -> tuple[Probe, Derived]:
    sha = asset.sha256 or ""
    return (
        Probe.model_validate_json(_probe_path(store, sha).read_text()),
        Derived.model_validate_json(_derived_path(store, sha).read_text()),
    )


def analyze(
    store: ProjectStore,
    asset_id: str,
    *,
    transcriber: Transcriber | None = None,
    model: VideoModel | None = None,
) -> AnalyzeResult:
    asset = store.load_asset(asset_id)
    transcriber = transcriber or default_transcriber()
    model = model or default_video_model()
    digest = input_hash(asset.sha256 or "", TRANSFORM_VERSION, model, transcriber)
    path = store.analysis_path(digest)
    if path.exists():
        result = AssetAnalysis.model_validate_json(path.read_text())
        reused = True
    else:
        _, derived = load_derived(store, asset)
        try:
            result = analyze_asset(
                asset_id=asset_id,
                asset_sha=asset.sha256 or "",
                transform_version=TRANSFORM_VERSION,
                proxy=derived.proxy,
                analysis_audio=derived.analysis_audio,
                time_map=derived.time_map,
                transcriber=transcriber,
                model=model,
            )
        finally:
            release = getattr(model, "release", None)
            if release:
                release(derived.proxy)
        store.write_json(path, result)
        reused = False
    store.write_text(store.analysis_index(asset_id), digest)
    return AnalyzeResult(
        asset_id=asset_id,
        run_id=result.run.id,
        moments=len(result.moments),
        transcript_segments=len(result.transcript),
        reused=reused,
    )


def load_analysis(store: ProjectStore, asset_id: str) -> AssetAnalysis:
    digest = store.analysis_index(asset_id).read_text().strip()
    return AssetAnalysis.model_validate_json(store.analysis_path(digest).read_text())


def plan(
    store: ProjectStore,
    asset_ids: list[str],
    target_seconds: int,
    *,
    excluded: set[str] = frozenset(),
    locked: list[LockedShot] = (),
) -> PlanResult:
    assets = [store.load_asset(a) for a in asset_ids]
    analyses = [load_analysis(store, a.id) for a in assets]
    moments = {m.moment_id: m for an in analyses for m in an.moments}
    transcript = {t.id: t for an in analyses for t in an.transcript}
    order = [a.id for a in sorted(assets, key=lambda a: (a.capture_time or "", a.label))]

    proposals = ChronologicalPlanner().propose(
        list(moments.values()), order, target_seconds * 1000, set(excluded), None
    )
    if not proposals:
        raise MediaError("NO_USABLE_FOOTAGE", "None of the footage could be used for a story.")
    proposal = proposals[0]
    shot_plan = solve(proposal, moments, transcript, excluded=set(excluded), locked=list(locked))

    probes = {a.id: load_derived(store, a)[0] for a in assets}
    edit_id = f"edit_{uuid.uuid4().hex[:12]}"
    timeline = assemble(
        shot_plan,
        project_id=store.root.name,
        edit_version_id=edit_id,
        has_audio={k: p.audio is not None for k, p in probes.items()},
    )
    require_valid(
        timeline, {k: RenderSource(Path(store.load_asset(k).source_path), p).info for k, p in probes.items()}
    )
    store.write_json(store.edit_path(edit_id), timeline)
    store.write_json(store.root / "proposals" / f"{proposal.id}.json", proposal)
    store.write_json(store.root / "shot_plans" / f"{edit_id}.json", shot_plan)
    return PlanResult(
        edit_id=edit_id,
        proposal_id=proposal.id,
        total_ms=shot_plan.total_ms,
        shots=len(shot_plan.shots),
        gaps=proposal.gaps,
        conflicts=shot_plan.conflicts,
    )


def render_edit(store: ProjectStore, edit_id: str, profile: str = "preview") -> RenderSummary:
    timeline = Timeline.model_validate_json(store.edit_path(edit_id).read_text())
    asset_ids = {c.asset_id for c in timeline.all_clips()}
    sources = {}
    for a in asset_ids:
        rec = store.load_asset(a)
        sources[a] = RenderSource(Path(rec.source_path), load_derived(store, rec)[0])
    prof = PROFILES[profile]
    out = store.render_dir() / f"{edit_id}-{profile}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    result = render(timeline, sources, out, profile=prof, cache_dir=store.segment_cache())
    check = verify_output(out, timeline, prof)
    return RenderSummary(
        edit_id=edit_id,
        output=str(out),
        profile=profile,
        segments_rendered=result.segments_rendered,
        segments_reused=result.segments_reused,
        ok=check.ok,
        errors=check.errors,
        review=check.review,
    )
