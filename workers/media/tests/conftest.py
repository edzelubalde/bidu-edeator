import shutil
from pathlib import Path

import pytest

from bidu_media.testing import generate

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = ROOT / "evals" / "fixtures" / "generated"


def pytest_collection_modifyitems(items):
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    skip = pytest.mark.skip(reason="ffmpeg/ffprobe not installed")
    for item in items:
        if "ffmpeg" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def fixtures() -> dict[str, Path]:
    return {p.name: p for p in generate(FIXTURE_DIR)}
