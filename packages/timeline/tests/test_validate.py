from fractions import Fraction

from bidu_timeline import AudioClip, RationalTime, validate_timeline
from bidu_timeline.builders import _replace


def test_fixture_is_valid(three_beats, sources):
    report = validate_timeline(three_beats, sources)
    assert report.ok, report.issues
    assert report.warnings == []
    assert three_beats.duration_frames == 240


def test_foreign_asset_is_rejected(three_beats, sources):
    del sources["asset_b"]
    report = validate_timeline(three_beats, sources)
    assert "ASSET_NOT_IN_PROJECT" in report.codes()


def test_source_past_end_of_file(three_beats, sources):
    v1 = three_beats.video("V1")
    v1[0] = v1[0].model_copy(update={"source_in": RationalTime(value=59 * 30, rate_num=30)})
    report = validate_timeline(_replace(three_beats, v1=v1), sources)
    assert "SOURCE_OUT_OF_BOUNDS" in report.codes()


def test_source_and_timeline_duration_must_agree(three_beats, sources):
    v1 = three_beats.video("V1")
    v1[0] = v1[0].model_copy(update={"source_duration": RationalTime(value=62, rate_num=30)})
    assert "DURATION_MISMATCH" in validate_timeline(_replace(three_beats, v1=v1), sources).codes()
    # One frame of difference is inside tolerance.
    v1[0] = v1[0].model_copy(update={"source_duration": RationalTime(value=61, rate_num=30)})
    assert validate_timeline(_replace(three_beats, v1=v1), sources).ok


def test_overlap_and_gap(three_beats, sources):
    v1 = three_beats.video("V1")
    v1[1] = v1[1].model_copy(update={"timeline_start_frame": 50})
    assert "TRACK_OVERLAP" in validate_timeline(_replace(three_beats, v1=v1), sources).codes()
    v1 = three_beats.video("V1")
    v1[0] = v1[0].model_copy(update={"timeline_start_frame": 0, "duration_frames": 50,
                                     "source_duration": RationalTime(value=50, rate_num=30)})
    report = validate_timeline(_replace(three_beats, v1=v1), sources)
    assert report.ok and "GAP" in report.codes()


def test_linked_audio_out_of_sync(three_beats, sources):
    a1 = three_beats.audio("A1")
    a1[0] = a1[0].model_copy(update={"timeline_start_frame": 5})
    a1[0] = a1[0].model_copy(update={"duration_frames": 55,
                                     "source_duration": RationalTime(value=55, rate_num=30)})
    assert "LINK_OUT_OF_SYNC" in validate_timeline(_replace(three_beats, a1=a1), sources).codes()


def test_audio_from_silent_asset(three_beats, sources):
    a1 = [*three_beats.audio("A1"), AudioClip(
        id="amb", asset_id="asset_c", beat_id="food", timeline_start_frame=0, duration_frames=30,
        source_in=RationalTime(value=0, rate_num=30), source_duration=RationalTime(value=30, rate_num=30),
        role="ambience")]
    codes = validate_timeline(_replace(three_beats, a1=a1), sources).codes()
    assert "SOURCE_HAS_NO_AUDIO" in codes and "TRACK_OVERLAP" in codes


def test_timeline_json_round_trip(three_beats):
    from bidu_timeline import Timeline
    again = Timeline.model_validate_json(three_beats.model_dump_json())
    assert again == three_beats
    assert three_beats.video("V1")[0].source_in.seconds == Fraction(10)
