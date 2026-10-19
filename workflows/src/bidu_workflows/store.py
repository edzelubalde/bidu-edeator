"""Project storage for local runs.

Stands in for Postgres + S3 until the API lands: same layout rules. Files are
named by content and transform version, written to a temporary name and then
renamed (the "verify, then publish" step), so retries never leave duplicates
and a re-run reuses finished stages.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


class AssetRecord(BaseModel):
    id: str
    label: str
    """The uploaded filename; a display label only, never used as a storage key."""
    source_path: str
    sha256: str | None = None
    status: str = "uploaded"
    error: dict | None = None
    capture_time: str | None = None


class ProjectStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    # --- paths ----------------------------------------------------------------
    def asset_path(self, asset_id: str) -> Path:
        return self.root / "assets" / f"{asset_id}.json"

    def derived_dir(self, sha: str, transform_version: str) -> Path:
        return self.root / "derived" / sha[:2] / sha / transform_version

    def analysis_path(self, input_hash: str) -> Path:
        return self.root / "analysis" / f"{input_hash}.json"

    def analysis_index(self, asset_id: str) -> Path:
        return self.root / "analysis" / "by-asset" / f"{asset_id}.txt"

    def edit_path(self, edit_id: str) -> Path:
        return self.root / "edits" / f"{edit_id}.json"

    def render_dir(self) -> Path:
        return self.root / "renders"

    def segment_cache(self) -> Path:
        return self.root / "cache" / "segments"

    # --- records ----------------------------------------------------------------
    def write_json(self, path: Path, model: BaseModel | dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".tmp-{os.getpid()}-{path.name}")
        text = (
            model.model_dump_json(indent=2, by_alias=True)
            if isinstance(model, BaseModel)
            else json.dumps(model, indent=2)
        )
        tmp.write_text(text)
        tmp.replace(path)

    def write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".tmp-{os.getpid()}-{path.name}")
        tmp.write_text(text)
        tmp.replace(path)

    def save_asset(self, asset: AssetRecord) -> None:
        self.write_json(self.asset_path(asset.id), asset)

    def load_asset(self, asset_id: str) -> AssetRecord:
        return AssetRecord.model_validate_json(self.asset_path(asset_id).read_text())

    def assets(self) -> list[AssetRecord]:
        folder = self.root / "assets"
        if not folder.exists():
            return []
        return [AssetRecord.model_validate_json(p.read_text()) for p in sorted(folder.glob("*.json"))]
