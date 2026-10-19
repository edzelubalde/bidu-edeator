"""Projects and background jobs for the web app.

Each project is a folder under the data directory holding a ``ProjectStore``
plus ``project.json``. Video work never runs inside a request: uploads queue a
check of the file, and "make", "preview" and "export" jobs run on a worker
thread, updating counts the page polls.
"""

from __future__ import annotations

import shutil
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from bidu_media.errors import MediaError
from bidu_timeline import InvalidTimeline
from bidu_workflows import stages
from bidu_workflows.store import AssetRecord, ProjectStore

Status = Literal[
    "empty",
    "uploading",
    "ready",
    "analyzing",
    "planning",
    "rendering",
    "ready_to_edit",
    "exporting",
    "completed",
    "needs_attention",
]


def now() -> str:
    return datetime.now(UTC).isoformat()


class Job(BaseModel):
    kind: Literal["make", "preview", "export"]
    stage: str
    completed: int = 0
    total: int = 0
    error: str | None = None
    started_at: str
    finished_at: str | None = None


class ProjectMeta(BaseModel):
    id: str
    name: str
    created_at: str
    target_seconds: int = 60
    status: Status = "empty"
    asset_ids: list[str] = []
    edit_ids: list[str] = []
    """Every edit version, oldest first. The last one is current."""
    excluded_moment_ids: list[str] = []
    preview_edit_id: str | None = None
    export_edit_id: str | None = None
    notes: list[str] = []
    job: Job | None = None

    @property
    def current_edit_id(self) -> str | None:
        return self.edit_ids[-1] if self.edit_ids else None


class Busy(Exception):
    pass


class Projects:
    def __init__(self, root: Path, *, workers: int = 2):
        self.root = root
        (root / "projects").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._checks = ThreadPoolExecutor(workers, thread_name_prefix="check")
        self._jobs = ThreadPoolExecutor(1, thread_name_prefix="job")
        self._pending_checks: dict[str, int] = {}

    # --- storage -----------------------------------------------------------
    def dir(self, project_id: str) -> Path:
        if not project_id.startswith("prj_") or "/" in project_id or ".." in project_id:
            raise KeyError(project_id)
        return self.root / "projects" / project_id

    def store(self, project_id: str) -> ProjectStore:
        return ProjectStore(self.dir(project_id))

    def get(self, project_id: str) -> ProjectMeta:
        path = self.dir(project_id) / "project.json"
        if not path.exists():
            raise KeyError(project_id)
        return ProjectMeta.model_validate_json(path.read_text())

    def save(self, meta: ProjectMeta) -> None:
        self.store(meta.id).write_json(self.dir(meta.id) / "project.json", meta)

    def update(self, project_id: str, **changes) -> ProjectMeta:
        with self._lock:
            meta = self.get(project_id).model_copy(update=changes)
            self.save(meta)
            return meta

    def list(self) -> list[ProjectMeta]:
        out = []
        for p in (self.root / "projects").iterdir():
            if (p / "project.json").exists():
                out.append(ProjectMeta.model_validate_json((p / "project.json").read_text()))
        return sorted(out, key=lambda m: m.created_at, reverse=True)

    def create(self, name: str, target_seconds: int) -> ProjectMeta:
        meta = ProjectMeta(
            id=f"prj_{uuid.uuid4().hex[:12]}",
            name=name.strip() or "Untitled vlog",
            created_at=now(),
            target_seconds=target_seconds,
        )
        self.dir(meta.id).mkdir(parents=True)
        self.save(meta)
        return meta

    def delete(self, project_id: str) -> None:
        self.get(project_id)
        shutil.rmtree(self.dir(project_id))

    def storage_bytes(self, project_id: str) -> int:
        return sum(p.stat().st_size for p in self.dir(project_id).rglob("*") if p.is_file())

    # --- uploads -------------------------------------------------------------
    def add_upload(self, project_id: str, filename: str, src) -> AssetRecord:
        """Save an uploaded file under a generated name; the filename is only a label."""
        meta = self.get(project_id)
        asset_id = f"asset_{uuid.uuid4().hex[:12]}"
        suffix = Path(filename).suffix.lower()
        suffix = suffix if suffix in {".mp4", ".mov", ".m4v"} else ".bin"
        dest_dir = self.dir(project_id) / "uploads"
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / f"{asset_id}{suffix}"
        tmp = dest.with_name(f".part-{dest.name}")
        with tmp.open("wb") as out:
            shutil.copyfileobj(src, out, 8 * 1024 * 1024)
        tmp.replace(dest)
        rec = AssetRecord(
            id=asset_id, label=Path(filename).name[:200], source_path=str(dest), status="checking"
        )
        store = self.store(project_id)
        store.save_asset(rec)
        with self._lock:
            meta = self.get(project_id)
            self.update(project_id, asset_ids=[*meta.asset_ids, asset_id], status="uploading")
            self._pending_checks[project_id] = self._pending_checks.get(project_id, 0) + 1
        self._checks.submit(self._check, project_id, asset_id)
        return rec

    def _check(self, project_id: str, asset_id: str) -> None:
        try:
            stages.ingest(self.store(project_id), asset_id)
        except Exception:  # A broken file must never take the server down.
            traceback.print_exc()
            store = self.store(project_id)
            rec = store.load_asset(asset_id)
            error = {
                "code": "MEDIA_DECODE_FAILED",
                "message": "This file could not be read.",
                "retryable": True,
            }
            store.save_asset(rec.model_copy(update={"status": "needs_attention", "error": error}))
        finally:
            with self._lock:
                self._pending_checks[project_id] -= 1
                meta = self.get(project_id)
                if self._pending_checks[project_id] == 0 and meta.status == "uploading":
                    self.update(project_id, status="ready")

    def remove_asset(self, project_id: str, asset_id: str) -> None:
        with self._lock:
            meta = self.get(project_id)
            if asset_id not in meta.asset_ids:
                raise KeyError(asset_id)
            self.update(project_id, asset_ids=[a for a in meta.asset_ids if a != asset_id])

    def assets(self, project_id: str) -> list[AssetRecord]:
        store = self.store(project_id)
        return [store.load_asset(a) for a in self.get(project_id).asset_ids]

    # --- jobs ----------------------------------------------------------------
    def _start(self, project_id: str, kind: str, stage: str, status: Status, fn) -> ProjectMeta:
        with self._lock:
            meta = self.get(project_id)
            if meta.job and meta.job.finished_at is None:
                raise Busy(meta.job.stage)
            meta = self.update(project_id, status=status, job=Job(kind=kind, stage=stage, started_at=now()))
        self._jobs.submit(self._run, project_id, fn)
        return meta

    def _run(self, project_id: str, fn) -> None:
        try:
            fn()
        except (MediaError, InvalidTimeline) as e:
            msg = e.message if isinstance(e, MediaError) else "The edit could not be built."
            self._finish(project_id, "needs_attention", error=msg)
        except Exception:
            traceback.print_exc()
            self._finish(project_id, "needs_attention", error="Something went wrong while processing.")

    def _progress(self, project_id: str, **job_changes) -> None:
        with self._lock:
            meta = self.get(project_id)
            if meta.job:
                self.update(project_id, job=meta.job.model_copy(update=job_changes))

    def _finish(self, project_id: str, status: Status, error: str | None = None, **changes) -> None:
        with self._lock:
            meta = self.get(project_id)
            job = meta.job.model_copy(update={"finished_at": now(), "error": error}) if meta.job else None
            self.update(project_id, status=status, job=job, **changes)

    def make(self, project_id: str, target_seconds: int) -> ProjectMeta:
        """Analyze every usable clip, plan the story, and render a preview."""
        self.update(project_id, target_seconds=target_seconds)

        def work():
            store = self.store(project_id)
            meta = self.get(project_id)
            ready = [a.id for a in self.assets(project_id) if a.status == "ready"]
            if not ready:
                raise MediaError("NO_USABLE_FOOTAGE", "None of the uploaded clips can be used yet.")
            self._progress(project_id, stage="Watching your clips", completed=0, total=len(ready))
            for i, a in enumerate(ready, 1):
                stages.analyze(store, a)
                self._progress(project_id, completed=i)
            self.update(project_id, status="planning")
            self._progress(project_id, stage="Planning the story", completed=0, total=1)
            plan = stages.plan(store, ready, meta.target_seconds, excluded=set(meta.excluded_moment_ids))
            self.update(
                project_id,
                edit_ids=[*self.get(project_id).edit_ids, plan.edit_id],
                notes=plan.gaps + plan.conflicts,
                status="rendering",
            )
            self._render_preview(project_id, plan.edit_id)

        return self._start(project_id, "make", "Getting started", "analyzing", work)

    def _render_preview(self, project_id: str, edit_id: str) -> None:
        """Render previews until the newest edit has one; edits made meanwhile are picked up."""
        while True:
            self._progress(project_id, stage="Rendering your preview", completed=0, total=1)
            summary = stages.render_edit(self.store(project_id), edit_id, "preview")
            if not summary.ok:
                raise MediaError("RENDER_FAILED", "The preview did not render correctly.")
            with self._lock:
                meta = self.get(project_id)
                if meta.current_edit_id == edit_id:
                    notes = [n for n in meta.notes if not n.startswith("Review:")]
                    self._finish(
                        project_id,
                        "ready_to_edit",
                        preview_edit_id=edit_id,
                        notes=notes + [f"Review: {r}" for r in summary.review],
                    )
                    return
                self.update(project_id, preview_edit_id=edit_id)
                edit_id = meta.current_edit_id

    def editing_allowed(self, project_id: str) -> bool:
        job = self.get(project_id).job
        return job is None or job.finished_at is not None or job.kind == "preview"

    def preview(self, project_id: str) -> ProjectMeta:
        with self._lock:
            meta = self.get(project_id)
            if meta.current_edit_id is None:
                raise KeyError("no edit")
            if meta.job and meta.job.finished_at is None and meta.job.kind == "preview":
                return meta  # The running preview loop will pick up the newest edit.
            edit_id = meta.current_edit_id
            return self._start(
                project_id,
                "preview",
                "Rendering your preview",
                "rendering",
                lambda: self._render_preview(project_id, edit_id),
            )

    def export(self, project_id: str) -> ProjectMeta:
        edit_id = self.get(project_id).current_edit_id
        if edit_id is None:
            raise KeyError("no edit")

        def work():
            self._progress(project_id, stage="Rendering full HD video", completed=0, total=1)
            summary = stages.render_edit(self.store(project_id), edit_id, "delivery")
            if not summary.ok:
                raise MediaError("RENDER_FAILED", "The video did not render correctly.")
            self._finish(project_id, "completed", export_edit_id=edit_id)

        return self._start(project_id, "export", "Rendering full HD video", "exporting", work)

    def shutdown(self) -> None:
        self._checks.shutdown(wait=False, cancel_futures=True)
        self._jobs.shutdown(wait=False, cancel_futures=True)
