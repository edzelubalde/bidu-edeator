import shutil
from pathlib import Path

import pytest
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from bidu_media.testing import generate
from bidu_workflows import stages
from bidu_workflows.store import ProjectStore
from bidu_workflows.worker import make_worker
from bidu_workflows.workflow import TASK_QUEUE, SliceInput, SliceWorkflow

pytestmark = [pytest.mark.ffmpeg, pytest.mark.temporal]
FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "generated"


async def test_slice_workflow_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("BIDU_TRANSCRIBER", "none")
    monkeypatch.setenv("BIDU_VIDEO_MODEL", "offline")
    shoot = tmp_path / "shoot"
    shoot.mkdir()
    for p in generate(FIXTURES, ["landscape_h264_30.mp4", "silent_720p_25.mp4", "unsupported_mpeg4.mp4"]):
        shutil.copy(p, shoot / p.name)
    store = ProjectStore(tmp_path / "project")
    ids = [a.id for a in stages.register_folder(store, shoot)]

    try:
        env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except Exception as e:  # The test server binary is downloaded on first use.
        pytest.skip(f"Temporal test server unavailable: {e}")
    async with env:
        async with make_worker(env.client):
            result = await env.client.execute_workflow(
                SliceWorkflow.run,
                SliceInput(project_dir=str(store.root), asset_ids=ids, target_seconds=10),
                id="slice-test",
                task_queue=TASK_QUEUE,
            )
    # The MPEG-4 file fails alone; the rest still becomes a rough cut.
    assert result.status == "needs_attention"
    bad = [r for r in result.ingested if not r.ok]
    assert len(bad) == 1 and bad[0].error["code"] == "UNSUPPORTED_CODEC"
    assert result.render and result.render.ok
    assert result.plan.total_ms == 10_000
    assert Path(result.render.output).exists()
