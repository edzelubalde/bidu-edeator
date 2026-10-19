from fractions import Fraction

import pytest

from bidu_timeline import Rate, RationalTime


def test_ntsc_rate_round_trips_without_drift():
    ntsc = Rate.parse("30000/1001")
    # One hour of 29.97 frames lands exactly back on the same frame count.
    frames = 107_892
    assert ntsc.seconds_to_frames(ntsc.frames_to_seconds(frames)) == frames
    assert ntsc.fps == Fraction(30000, 1001)


@pytest.mark.parametrize("text,expected", [("30", Fraction(30)), ("60000/1001", Fraction(60000, 1001)), ("25/1", Fraction(25))])
def test_parse(text, expected):
    assert Rate.parse(text).fps == expected


def test_rescale_between_rates_snaps_to_nearest_frame():
    t = RationalTime(value=546, rate_num=30)  # 18.2 s
    assert t.ms == 18200
    at_2997 = t.rescaled(Rate.parse("30000/1001"))
    assert abs(at_2997.seconds - t.seconds) <= Fraction(1001, 60000)


def test_from_ms_and_addition_across_rates():
    a = RationalTime.from_ms(1000, Rate(numerator=30))
    b = RationalTime.from_ms(400, Rate(numerator=25))
    assert (a + b).seconds == Fraction(7, 5)
    # Half a frame rounds up rather than drifting.
    assert RationalTime.from_ms(500, Rate(numerator=25)).value == 13
