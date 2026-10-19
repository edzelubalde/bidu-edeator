from fractions import Fraction

import pytest
from pydantic import TypeAdapter

from bidu_timeline import EditOp, OpError, RationalTime, apply_ops, validate_timeline


def ops(*raw):
    return [TypeAdapter(EditOp).validate_python(r) for r in raw]


def starts(t, track):
    return {c.id: c.timeline_start_frame for c in (t.video(track) if track[0] == "V" else t.audio(track))}


def test_move_card_ripples_and_keeps_overlay_and_audio_attached(three_beats, sources):
    t = apply_ops(three_beats, ops({"op": "move_card", "beat_id": "outro", "to_index": 0})).timeline
    assert t.beat_order() == ["outro", "intro", "food"]
    assert starts(t, "V1") == {"c4": 0, "c1": 30, "c2": 90, "c3": 180}
    # Overlay was 30 frames into c2; it still is.
    assert starts(t, "V2") == {"o1": 120}
    assert starts(t, "A1") == {"c4_a": 0, "c1_a": 30, "c2_a": 90, "c3_a": 180}
    assert validate_timeline(t, sources).ok


def test_move_card_keeps_clip_order_within_beat(three_beats):
    t = apply_ops(three_beats, ops({"op": "move_card", "beat_id": "food", "to_index": 99})).timeline
    assert [c.id for c in t.video("V1")] == ["c1", "c4", "c2", "c3"]


def test_trim_shortens_linked_audio_and_clips_overlay(three_beats, sources):
    # c2 was 3 s; trim it to 1.5 s starting 0.5 s later.
    t = apply_ops(three_beats, ops({
        "op": "trim_clip", "clip_id": "c2",
        "source_in": {"value": 165, "rate_num": 30},
        "source_duration": {"value": 45, "rate_num": 30},
    })).timeline
    c2 = next(c for c in t.video("V1") if c.id == "c2")
    a2 = next(c for c in t.audio("A1") if c.id == "c2_a")
    assert (c2.duration_frames, a2.duration_frames) == (45, 45)
    assert a2.source_in == c2.source_in
    # Overlay starts 30 frames in and must now end with c2 (frame 105).
    o1 = t.video("V2")[0]
    assert (o1.timeline_start_frame, o1.timeline_end_frame) == (90, 105)
    assert o1.source_duration.seconds == Fraction(1, 2)
    assert t.duration_frames == 240 - 45
    assert validate_timeline(t, sources).ok


def test_swap_replaces_source_and_audio(three_beats, sources):
    t = apply_ops(three_beats, ops({
        "op": "swap_moment", "clip_id": "c1", "asset_id": "asset_b", "moment_id": "m9",
        "source_in": {"value": 0, "rate_num": 30}, "source_duration": {"value": 90, "rate_num": 30},
    })).timeline
    c1 = t.video("V1")[0]
    assert (c1.asset_id, c1.moment_id, c1.duration_frames) == ("asset_b", "m9", 90)
    assert t.audio("A1")[0].asset_id == "asset_b"
    assert validate_timeline(t, sources).ok


def test_locked_clip_cannot_be_trimmed_or_excluded(three_beats):
    locked = apply_ops(three_beats, ops({"op": "set_lock", "clip_id": "c3", "source": True})).timeline
    assert next(c for c in locked.video("V1") if c.id == "c3").locks.source
    with pytest.raises(OpError) as e:
        apply_ops(locked, ops({"op": "trim_clip", "clip_id": "c3",
                               "source_in": {"value": 0, "rate_num": 30},
                               "source_duration": {"value": 30, "rate_num": 30}}))
    assert e.value.code == "CLIP_LOCKED"
    with pytest.raises(OpError):
        apply_ops(locked, ops({"op": "exclude_moment", "moment_id": "m3"}))


def test_remove_card_drops_anchored_items(three_beats, sources):
    t = apply_ops(three_beats, ops({"op": "remove_card", "beat_id": "food"})).timeline
    assert [c.id for c in t.video("V1")] == ["c1", "c4"]
    assert t.video("V2") == [] and [a.id for a in t.audio("A1")] == ["c1_a", "c4_a"]
    assert t.duration_frames == 90
    assert validate_timeline(t, sources).ok


def test_exclude_moment_reports_exclusion(three_beats, sources):
    result = apply_ops(three_beats, ops({"op": "exclude_moment", "moment_id": "m5"},
                                        {"op": "exclude_moment", "moment_id": "m4"}))
    assert result.excluded_moment_ids == ["m5", "m4"]
    assert result.timeline.video("V2") == []
    assert [c.id for c in result.timeline.video("V1")] == ["c1", "c2", "c3"]
    assert validate_timeline(result.timeline, sources).ok


def test_ops_do_not_mutate_input(three_beats):
    before = three_beats.model_dump()
    apply_ops(three_beats, ops({"op": "remove_card", "beat_id": "intro"}))
    assert three_beats.model_dump() == before


def test_unknown_targets_raise(three_beats):
    for raw in ({"op": "move_card", "beat_id": "nope", "to_index": 0},
                {"op": "remove_clip", "clip_id": "nope"}):
        with pytest.raises(OpError):
            apply_ops(three_beats, ops(raw))


def test_rational_time_in_ops_accepts_other_rates(three_beats, sources):
    t = apply_ops(three_beats, ops({
        "op": "trim_clip", "clip_id": "c4",
        "source_in": RationalTime(value=20 * 25, rate_num=25).model_dump(),
        "source_duration": RationalTime(value=25, rate_num=25).model_dump(),
    })).timeline
    assert t.video("V1")[-1].duration_frames == 30
    assert validate_timeline(t, sources).ok
