"""Temporal activities: thin wrappers over ``stages`` that map errors to retry policy."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from bidu_media.errors import MediaError

from . import stages
from .store import ProjectStore


class AssetInput(BaseModel):
    project_dir: str
    asset_id: str


class PlanInput(BaseModel):
    project_dir: str
    asset_ids: list[str]
    target_seconds: int
    excluded_moment_ids: list[str] = []


class RenderInput(BaseModel):
    project_dir: str
    edit_id: str
    profile: str = "preview"


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except MediaError as e:
        # Permanent media problems must not burn retries; transient ones may.
        raise ApplicationError(e.message, e.to_public(), type=e.code, non_retryable=not e.retryable) from e


@activity.defn(name="ingest_asset")
def ingest_asset(inp: AssetInput) -> stages.IngestResult:
    return _call(stages.ingest, ProjectStore(Path(inp.project_dir)), inp.asset_id)


@activity.defn(name="analyze_asset")
def analyze_asset(inp: AssetInput) -> stages.AnalyzeResult:
    return _call(stages.analyze, ProjectStore(Path(inp.project_dir)), inp.asset_id)


@activity.defn(name="plan_edit")
def plan_edit(inp: PlanInput) -> stages.PlanResult:
    return _call(
        stages.plan,
        ProjectStore(Path(inp.project_dir)),
        inp.asset_ids,
        inp.target_seconds,
        excluded=set(inp.excluded_moment_ids),
    )


@activity.defn(name="render_edit")
def render_edit(inp: RenderInput) -> stages.RenderSummary:
    return _call(stages.render_edit, ProjectStore(Path(inp.project_dir)), inp.edit_id, inp.profile)


ALL = [ingest_asset, analyze_asset, plan_edit, render_edit]
