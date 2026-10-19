import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bidu_api.app import create_app
from bidu_media.testing import generate

FIXTURES = Path(__file__).resolve().parents[3] / "evals" / "fixtures" / "generated"
pytestmark = pytest.mark.ffmpeg


def wait(client, pid, until, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = client.get(f"/v1/projects/{pid}").json()
        if p["status"] in until and (p["job"] is None or p["job"]["finished_at"]):
            return p
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {until}: {p}")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BIDU_TRANSCRIBER", "none")
    monkeypatch.setenv("BIDU_VIDEO_MODEL", "offline")
    with TestClient(create_app(tmp_path / "data", web_dir=None)) as c:
        yield c


def test_upload_make_edit_undo_and_download(client):
    files = generate(FIXTURES, ["landscape_h264_30.mp4", "vfr_h264.mp4", "unsupported_mpeg4.mp4"])
    pid = client.post("/v1/projects", json={"name": "Market day", "target_seconds": 15}).json()["id"]
    for f in files:
        with f.open("rb") as fh:
            r = client.post(f"/v1/projects/{pid}/files", files={"file": (f.name, fh, "video/mp4")})
        assert r.status_code == 201
    p = wait(client, pid, {"ready"})
    by_name = {a["file"]: a for a in p["assets"]}
    assert by_name["unsupported_mpeg4.mp4"]["status"] == "needs_attention"
    assert "H.264" in by_name["unsupported_mpeg4.mp4"]["error"]

    assert client.post(f"/v1/projects/{pid}/make", json={"target_seconds": 15}).status_code == 202
    p = wait(client, pid, {"ready_to_edit", "needs_attention"})
    assert p["status"] == "ready_to_edit", p
    assert client.get(f"/v1/projects/{pid}/preview.mp4").headers["content-type"] == "video/mp4"

    board = client.get(f"/v1/projects/{pid}/storyboard").json()
    assert board["duration_s"] == pytest.approx(15, abs=1.5)
    first = board["beats"][0]["shots"][0]
    assert client.get(first["thumb_url"]).headers["content-type"] == "image/jpeg"

    # Remove the first shot: a new version, shorter, and the preview re-renders.
    ops = [{"op": "remove_clip", "clip_id": first["clip_id"]}]
    r = client.patch(f"/v1/projects/{pid}/storyboard", json={"base_edit_id": board["edit_id"], "ops": ops})
    assert r.status_code == 200, r.json()
    edited = r.json()
    assert edited["version"] == 2 and edited["duration_s"] < board["duration_s"]
    # A stale base is refused rather than merged.
    r = client.patch(f"/v1/projects/{pid}/storyboard", json={"base_edit_id": board["edit_id"], "ops": []})
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_REVISION"
    wait(client, pid, {"ready_to_edit"})

    undone = client.post(f"/v1/projects/{pid}/undo").json()
    assert undone["version"] == 3 and undone["duration_s"] == board["duration_s"]
    wait(client, pid, {"ready_to_edit"})

    assert client.post(f"/v1/projects/{pid}/export").status_code == 202
    p = wait(client, pid, {"completed", "needs_attention"})
    assert p["status"] == "completed" and p["has_export"]
    r = client.get(f"/v1/projects/{pid}/download")
    assert r.status_code == 200 and "Market%20day%20-%20rough%20cut.mp4" in r.headers["content-disposition"]


def test_bad_requests_get_plain_errors(client):
    assert client.get("/v1/projects/prj_nope").json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/v1/projects/../../etc").status_code == 404
    pid = client.post("/v1/projects", json={"name": "x"}).json()["id"]
    r = client.post(f"/v1/projects/{pid}/make", json={"target_seconds": 5})
    assert r.status_code == 422
