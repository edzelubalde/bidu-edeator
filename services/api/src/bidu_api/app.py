"""HTTP API for the web app, and the server that hosts the built pages.

Run it with ``uv run bidu-web`` and open http://localhost:8000.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bidu_media.errors import MediaError
from bidu_timeline import InvalidTimeline, OpError
from bidu_workflows import stages

from . import storyboard
from .projects import Busy, Projects

DATA_DIR = Path(os.environ.get("BIDU_DATA", "data")).resolve()
WEB_DIR = Path(os.environ.get("BIDU_WEB_DIR", Path(__file__).resolve().parents[4] / "apps" / "web" / "out"))
MIN_SECONDS, MAX_SECONDS = 15, 600


class NewProject(BaseModel):
    name: str = Field(default="", max_length=120)
    target_seconds: int = Field(default=60, ge=MIN_SECONDS, le=MAX_SECONDS)


class MakeRequest(BaseModel):
    target_seconds: int = Field(ge=MIN_SECONDS, le=MAX_SECONDS)


class EditRequest(BaseModel):
    base_edit_id: str
    ops: list[dict]


def create_app(data_dir: Path = DATA_DIR, web_dir: Path | None = WEB_DIR) -> FastAPI:
    projects = Projects(data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        projects.shutdown()

    app = FastAPI(title="Bidu vlog editor", lifespan=lifespan)
    app.state.projects = projects

    def error(status: int, code: str, message: str):
        return HTTPException(status, detail={"code": code, "message": message})

    def get_meta(project_id: str):
        try:
            return projects.get(project_id)
        except KeyError:
            raise error(404, "NOT_FOUND", "That project does not exist.") from None

    def project_view(project_id: str) -> dict:
        meta = get_meta(project_id)
        return {
            **meta.model_dump(exclude={"edit_ids", "excluded_moment_ids"}),
            "current_edit_id": meta.current_edit_id,
            "versions": len(meta.edit_ids),
            "has_preview": meta.preview_edit_id is not None,
            "preview_is_current": meta.preview_edit_id is not None
            and meta.preview_edit_id == meta.current_edit_id,
            "has_export": meta.export_edit_id is not None and meta.export_edit_id == meta.current_edit_id,
            "storage_bytes": projects.storage_bytes(project_id),
            "assets": [
                {"id": a.id, "file": a.label, "status": a.status, "error": (a.error or {}).get("message")}
                for a in projects.assets(project_id)
            ],
        }

    @app.exception_handler(HTTPException)
    async def http_error(_req: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "ERROR", "message": str(exc.detail)}
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.get("/v1/projects")
    def list_projects():
        return [project_view(m.id) for m in projects.list()]

    @app.post("/v1/projects", status_code=201)
    def create_project(body: NewProject):
        return project_view(projects.create(body.name, body.target_seconds).id)

    @app.get("/v1/projects/{project_id}")
    def get_project(project_id: str):
        return project_view(project_id)

    @app.delete("/v1/projects/{project_id}", status_code=204)
    def delete_project(project_id: str):
        meta = get_meta(project_id)
        if meta.job and meta.job.finished_at is None:
            raise error(409, "BUSY", "Wait for the current step to finish before deleting.")
        projects.delete(project_id)

    @app.post("/v1/projects/{project_id}/files", status_code=201)
    def upload(project_id: str, file: Annotated[UploadFile, File()]):
        get_meta(project_id)
        rec = projects.add_upload(project_id, file.filename or "clip.mp4", file.file)
        return {"id": rec.id, "file": rec.label, "status": rec.status}

    @app.delete("/v1/projects/{project_id}/files/{asset_id}", status_code=204)
    def remove_file(project_id: str, asset_id: str):
        get_meta(project_id)
        try:
            projects.remove_asset(project_id, asset_id)
        except KeyError:
            raise error(404, "NOT_FOUND", "That file is not in this project.") from None

    def start(fn, *args):
        try:
            fn(*args)
        except Busy as e:
            raise error(409, "BUSY", f"Still working on: {e}.") from None
        except KeyError:
            raise error(409, "NO_EDIT", "Make a rough cut first.") from None

    @app.post("/v1/projects/{project_id}/make", status_code=202)
    def make(project_id: str, body: MakeRequest):
        get_meta(project_id)
        start(projects.make, project_id, body.target_seconds)
        return project_view(project_id)

    @app.post("/v1/projects/{project_id}/export", status_code=202)
    def export(project_id: str):
        get_meta(project_id)
        start(projects.export, project_id)
        return project_view(project_id)

    def base_url(project_id: str) -> str:
        return f"/v1/projects/{project_id}"

    @app.get("/v1/projects/{project_id}/storyboard")
    def get_storyboard(project_id: str):
        meta = get_meta(project_id)
        board = storyboard.build(projects.store(project_id), meta, base_url(project_id))
        if board is None:
            raise error(404, "NO_EDIT", "No rough cut yet.")
        return board

    @app.patch("/v1/projects/{project_id}/storyboard")
    def edit(project_id: str, body: EditRequest):
        meta = get_meta(project_id)
        if not projects.editing_allowed(project_id):
            raise error(409, "BUSY", "Wait for the current step to finish.")
        if body.base_edit_id != meta.current_edit_id:
            raise error(409, "STALE_REVISION", "This storyboard changed. Reload to see the latest version.")
        store = projects.store(project_id)
        try:
            new_id, excluded = storyboard.apply(store, body.base_edit_id, body.ops)
        except OpError as e:
            raise error(422, e.code, str(e).split(": ", 1)[-1]) from None
        except InvalidTimeline as e:
            raise error(422, "INVALID_EDIT_PLAN", _plain(e)) from None
        except ValueError as e:
            raise error(422, "INVALID_EDIT_PLAN", "That change is not valid.") from e
        projects.update(
            project_id,
            edit_ids=[*meta.edit_ids, new_id],
            excluded_moment_ids=[*meta.excluded_moment_ids, *excluded],
        )
        start(projects.preview, project_id)
        return storyboard.build(store, get_meta(project_id), base_url(project_id))

    @app.post("/v1/projects/{project_id}/undo")
    def undo(project_id: str):
        meta = get_meta(project_id)
        if len(meta.edit_ids) < 2:
            raise error(409, "NOTHING_TO_UNDO", "There is nothing to undo.")
        if not projects.editing_allowed(project_id):
            raise error(409, "BUSY", "Wait for the current step to finish.")
        store = projects.store(project_id)
        new_id = storyboard.copy_version(store, meta.edit_ids[-2])
        projects.update(project_id, edit_ids=[*meta.edit_ids, new_id])
        start(projects.preview, project_id)
        return storyboard.build(store, get_meta(project_id), base_url(project_id))

    def render_file(project_id: str, edit_id: str | None, profile: str) -> Path:
        if edit_id is None:
            raise error(404, "NOT_READY", "This video is not ready yet.")
        path = projects.store(project_id).render_dir() / f"{edit_id}-{profile}.mp4"
        if not path.exists():
            raise error(404, "NOT_READY", "This video is not ready yet.")
        return path

    @app.get("/v1/projects/{project_id}/preview.mp4")
    def preview(project_id: str):
        meta = get_meta(project_id)
        return FileResponse(render_file(project_id, meta.preview_edit_id, "preview"), media_type="video/mp4")

    @app.get("/v1/projects/{project_id}/download")
    def download(project_id: str):
        meta = get_meta(project_id)
        path = render_file(project_id, meta.export_edit_id, "delivery")
        name = re.sub(r"[^A-Za-z0-9 _-]+", "", meta.name).strip() or "vlog"
        return FileResponse(path, media_type="video/mp4", filename=f"{name} - rough cut.mp4")

    @app.get("/v1/projects/{project_id}/thumbs/{asset_id}")
    def thumb(project_id: str, asset_id: str, t: float = 0):
        meta = get_meta(project_id)
        if asset_id not in meta.asset_ids:
            raise error(404, "NOT_FOUND", "Unknown clip.")
        try:
            path = storyboard.thumbnail(projects.store(project_id), asset_id, max(0.0, t))
        except (FileNotFoundError, MediaError, subprocess.CalledProcessError):
            raise error(404, "NOT_READY", "Thumbnail not ready.") from None
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/v1/projects/{project_id}/assets/{asset_id}/video")
    def source_video(project_id: str, asset_id: str):
        """The browser-friendly proxy of an uploaded clip, for checking a shot in context."""
        meta = get_meta(project_id)
        if asset_id not in meta.asset_ids:
            raise error(404, "NOT_FOUND", "Unknown clip.")
        store = projects.store(project_id)
        try:
            _, derived = stages.load_derived(store, store.load_asset(asset_id))
        except FileNotFoundError:
            raise error(404, "NOT_READY", "This clip is still being checked.") from None
        return FileResponse(derived.proxy, media_type="video/mp4")

    if web_dir is not None and Path(web_dir).exists():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


def _plain(e: InvalidTimeline) -> str:
    codes = {i.code for i in e.report.errors}
    if "SOURCE_OUT_OF_BOUNDS" in codes:
        return "That goes past the start or end of the original clip."
    if "NON_POSITIVE_SOURCE" in codes or "DURATION_MISMATCH" in codes:
        return "That would make the shot too short."
    return "That change would break the edit."


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="bidu-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"Open http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port)
