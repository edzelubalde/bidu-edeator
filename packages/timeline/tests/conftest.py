from fractions import Fraction

import pytest

from bidu_timeline import SourceInfo, empty_timeline
from bidu_timeline.builders import overlay_shot, primary_shot


@pytest.fixture
def sources():
    return {
        "asset_a": SourceInfo(duration_seconds=Fraction(60)),
        "asset_b": SourceInfo(duration_seconds=Fraction(30)),
        "asset_c": SourceInfo(duration_seconds=Fraction(20), has_audio=False),
    }


@pytest.fixture
def three_beats():
    """intro (2 s) | food (3 s + 2 s, with an overlay) | outro (1 s) at 30 fps."""
    t = empty_timeline("proj", "edit_1")
    t = primary_shot(
        t,
        clip_id="c1",
        asset_id="asset_a",
        beat_id="intro",
        source_in_seconds=Fraction(10),
        duration_seconds=Fraction(2),
        moment_id="m1",
    )
    t = primary_shot(
        t,
        clip_id="c2",
        asset_id="asset_b",
        beat_id="food",
        source_in_seconds=Fraction(5),
        duration_seconds=Fraction(3),
        moment_id="m2",
    )
    t = primary_shot(
        t,
        clip_id="c3",
        asset_id="asset_a",
        beat_id="food",
        source_in_seconds=Fraction(30),
        duration_seconds=Fraction(2),
        moment_id="m3",
    )
    t = primary_shot(
        t,
        clip_id="c4",
        asset_id="asset_b",
        beat_id="outro",
        source_in_seconds=Fraction(20),
        duration_seconds=Fraction(1),
        moment_id="m4",
    )
    # B-roll over the first food shot, 1 s in, 1.5 s long; dialogue from c2 continues under it.
    t = overlay_shot(
        t,
        clip_id="o1",
        asset_id="asset_c",
        beat_id="food",
        timeline_start_frame=90,
        source_in_seconds=Fraction(4),
        duration_seconds=Fraction(3, 2),
        moment_id="m5",
    )
    return t
