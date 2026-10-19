"""The vertical-slice workflow: ingest -> analyze -> plan -> render.

Durable by construction: each stage is an activity whose result Temporal
records, so a worker crash resumes after the last finished stage, and the
stages themselves reuse stored outputs if an activity is retried.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import AssetInput, PlanInput, RenderInput
    from .stages import AnalyzeResult, IngestResult, PlanResult, RenderSummary

TASK_QUEUE = "bidu-slice"

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=5,
)


class SliceInput(BaseModel):
    project_dir: str
    asset_ids: list[str]
    target_seconds: int = 60
    profile: str = "preview"


class SliceResult(BaseModel):
    status: str
    """completed | needs_attention"""
    ingested: list[IngestResult]
    analyzed: list[AnalyzeResult]
    plan: PlanResult | None = None
    render: RenderSummary | None = None


@workflow.defn(name="SliceWorkflow")
class SliceWorkflow:
    def __init__(self) -> None:
        self._stage = "queued"
        self._completed = 0
        self._total = 0

    @workflow.query
    def progress(self) -> dict:
        """Completed and total counts for the current stage, never a guessed countdown."""
        return {"stage": self._stage, "completed": self._completed, "total": self._total}

    async def _each(self, stage: str, activity: str, inputs: list[AssetInput], minutes: int, result_type):
        self._stage, self._completed, self._total = stage, 0, len(inputs)

        async def one(inp: AssetInput):
            out = await workflow.execute_activity(
                activity,
                inp,
                start_to_close_timeout=timedelta(minutes=minutes),
                retry_policy=RETRY,
                result_type=result_type,
            )
            self._completed += 1
            return out

        return await asyncio.gather(*(one(i) for i in inputs))

    @workflow.run
    async def run(self, inp: SliceInput) -> SliceResult:
        assets = [AssetInput(project_dir=inp.project_dir, asset_id=a) for a in inp.asset_ids]
        ingested = await self._each("analyzing", "ingest_asset", assets, 60, IngestResult)
        ready = [AssetInput(project_dir=inp.project_dir, asset_id=r.asset_id) for r in ingested if r.ok]
        if not ready:
            self._stage = "needs_attention"
            return SliceResult(status="needs_attention", ingested=ingested, analyzed=[])

        analyzed = await self._each("analyzing", "analyze_asset", ready, 120, AnalyzeResult)

        self._stage = "planning"
        plan = await workflow.execute_activity(
            "plan_edit",
            PlanInput(
                project_dir=inp.project_dir,
                asset_ids=[a.asset_id for a in ready],
                target_seconds=inp.target_seconds,
            ),
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=RETRY,
            result_type=PlanResult,
        )

        self._stage = "rendering"
        render = await workflow.execute_activity(
            "render_edit",
            RenderInput(project_dir=inp.project_dir, edit_id=plan.edit_id, profile=inp.profile),
            start_to_close_timeout=timedelta(hours=1),
            retry_policy=RETRY,
            result_type=RenderSummary,
        )
        failed = any(not r.ok for r in ingested) or not render.ok
        self._stage = "needs_attention" if failed else "completed"
        return SliceResult(status=self._stage, ingested=ingested, analyzed=analyzed, plan=plan, render=render)
