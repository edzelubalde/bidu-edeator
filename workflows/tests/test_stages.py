import shutil
from pathlib import Path

import pytest

from bidu_media.testing import generate
from bidu_workflows import stages
from bidu_workflows.store import ProjectStore

pytestmark = pytest.mark.ffmpeg
FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "generated"


def test_rerun_reuses_every_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("BIDU_TRANSCRIBER", "none")
    monkeypatch.setenv("BIDU_VIDEO_MODEL", "offline")
    shoot = tmp_path / "shoot"
    shoot.mkdir()
    for p in generate(FIXTURES, ["landscape_h264_30.mp4", "vfr_h264.mp4"]):
        shutil.copy(p, shoot / p.name)
    store = ProjectStore(tmp_path / "project")

    ids = [a.id for a in stages.register_folder(store, shoot)]
    assert all(stages.ingest(store, a).ok for a in ids)
    first = [stages.analyze(store, a) for a in ids]
    plan = stages.plan(store, ids, 12)
    out = stages.render_edit(store, plan.edit_id)
    assert out.ok, out.errors

    # Registering the same folder again does not create duplicate assets,
    # and every stage returns the stored result.
    assert [a.id for a in stages.register_folder(store, shoot)] == ids
    assert all(stages.ingest(store, a).reused for a in ids)
    again = [stages.analyze(store, a) for a in ids]
    assert all(r.reused for r in again) and [r.run_id for r in again] == [r.run_id for r in first]
    rerender = stages.render_edit(store, plan.edit_id)
    assert rerender.segments_rendered == 0
