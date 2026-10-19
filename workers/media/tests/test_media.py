from fractions import Fraction

import pytest

from bidu_media.errors import MediaError
from bidu_media.ffmpeg import ffprobe_json
from bidu_media.normalize import normalize
from bidu_media.probe import probe
from bidu_media.render import PREVIEW, RenderSource, render
from bidu_media.testing import read_frame_codes
from bidu_media.verify import verify_output
from bidu_timeline import apply_ops, empty_timeline
from bidu_timeline.builders import overlay_shot, primary_shot

pytestmark = pytest.mark.ffmpeg


def test_probe_reads_real_streams(fixtures):
    land = probe(fixtures["landscape_h264_30.mp4"])
    assert land.video.codec == "h264" and land.video.nominal_rate.fps == 30
    assert not land.video.variable_frame_rate and land.audio is not None
    assert land.usable_duration_seconds == pytest.approx(12, abs=0.05)

    portrait = probe(fixtures["portrait_rotated.mov"])
    assert portrait.video.rotation in (90, 270)
    assert portrait.video.display_size == (1080, 1920) and portrait.video.is_portrait

    assert probe(fixtures["vfr_h264.mp4"]).video.variable_frame_rate
    hdr = probe(fixtures["hevc_hlg_2997.mov"])
    assert hdr.video.is_hdr and hdr.video.nominal_rate.fps == Fraction(30000, 1001)
    assert probe(fixtures["silent_720p_25.mp4"]).audio is None


def test_unsupported_inputs_fail_with_specific_codes(fixtures, tmp_path):
    with pytest.raises(MediaError) as e:
        probe(fixtures["unsupported_mpeg4.mp4"])
    assert e.value.code == "UNSUPPORTED_CODEC" and "H.264" in e.value.message

    fake = tmp_path / "holiday.mp4"
    fake.write_text("not really a video")
    with pytest.raises(MediaError) as e:
        probe(fake)
    assert e.value.code == "MEDIA_DECODE_FAILED"
    assert str(tmp_path) not in e.value.message


def test_proxy_frames_map_back_to_source_frames(fixtures, tmp_path):
    src = fixtures["landscape_h264_30.mp4"]
    derived = normalize(src, probe(src), tmp_path / "land")
    codes = read_frame_codes(derived.proxy)
    assert len(codes) == 360
    assert codes == list(range(360))
    assert derived.analysis_audio and derived.waveform and len(derived.thumbnails) == 6


def test_vfr_proxy_stays_within_one_source_frame(fixtures, tmp_path):
    src = fixtures["vfr_h264.mp4"]
    p = probe(src)
    derived = normalize(src, p, tmp_path / "vfr")
    codes = read_frame_codes(derived.proxy)
    for k, code in enumerate(codes):
        # Source frames exist every 1/30 s for 3 s, then every 1/10 s.
        spacing = 1 if k < 90 else 3
        source_t = derived.time_map.to_source(Fraction(k, 30))
        assert abs(code - source_t * 30) <= spacing, (k, code)


def test_portrait_and_hdr_proxies(fixtures, tmp_path):
    src = fixtures["portrait_rotated.mov"]
    d = normalize(src, probe(src), tmp_path / "portrait")
    s = ffprobe_json(d.proxy, "-show_streams", "-select_streams", "v")["streams"][0]
    assert (s["width"], s["height"]) == (720, 1280)

    src = fixtures["hevc_hlg_2997.mov"]
    d = normalize(src, probe(src), tmp_path / "hdr")
    s = ffprobe_json(d.proxy, "-show_streams", "-select_streams", "v")["streams"][0]
    assert s["color_transfer"] == "bt709" and s["r_frame_rate"] == "30/1"


@pytest.fixture
def sources(fixtures):
    return {
        "land": RenderSource(fixtures["landscape_h264_30.mp4"], probe(fixtures["landscape_h264_30.mp4"])),
        "hdr": RenderSource(fixtures["hevc_hlg_2997.mov"], probe(fixtures["hevc_hlg_2997.mov"])),
        "portrait": RenderSource(fixtures["portrait_rotated.mov"], probe(fixtures["portrait_rotated.mov"])),
        "silent": RenderSource(fixtures["silent_720p_25.mp4"], probe(fixtures["silent_720p_25.mp4"])),
    }


def _two_shots():
    t = empty_timeline("p", "e1")
    t = primary_shot(
        t,
        clip_id="s1",
        asset_id="land",
        beat_id="b1",
        source_in_seconds=Fraction(2),
        duration_seconds=Fraction(1),
        audio_fade_frames=3,
    )
    t = primary_shot(
        t,
        clip_id="s2",
        asset_id="land",
        beat_id="b2",
        source_in_seconds=Fraction(8),
        duration_seconds=Fraction(1),
        audio_fade_frames=3,
    )
    return t


def test_render_is_frame_accurate_and_reuses_segments(sources, tmp_path):
    t = _two_shots()
    out = tmp_path / "cut.mp4"
    result = render(t, sources, out, profile=PREVIEW, cache_dir=tmp_path / "cache")
    assert result.segments_rendered == 2
    codes = read_frame_codes(out)
    assert codes == list(range(60, 90)) + list(range(240, 270))
    check = verify_output(out, t, PREVIEW)
    assert check.ok, check.errors

    # Swap the order: both segments already exist, nothing re-encodes.
    moved = apply_ops(t, [{"op": "move_card", "beat_id": "b2", "to_index": 0}]).timeline
    again = render(moved, sources, tmp_path / "moved.mp4", profile=PREVIEW, cache_dir=tmp_path / "cache")
    assert (again.segments_rendered, again.segments_reused) == (0, 2)
    assert read_frame_codes(tmp_path / "moved.mp4") == list(range(240, 270)) + list(range(60, 90))


def test_overlay_and_mixed_sources(sources, tmp_path):
    t = _two_shots()
    # HDR overlay for the last half of the first shot; dialogue from s1 keeps playing.
    t = overlay_shot(
        t,
        clip_id="o1",
        asset_id="hdr",
        beat_id="b1",
        timeline_start_frame=15,
        source_in_seconds=Fraction(1),
        duration_seconds=Fraction(1, 2),
    )
    t = primary_shot(
        t,
        clip_id="s3",
        asset_id="portrait",
        beat_id="b3",
        source_in_seconds=Fraction(0),
        duration_seconds=Fraction(1),
    )
    t = primary_shot(
        t,
        clip_id="s4",
        asset_id="silent",
        beat_id="b3",
        source_in_seconds=Fraction(1),
        duration_seconds=Fraction(1),
        with_audio=False,
    )
    out = tmp_path / "mixed.mp4"
    render(t, sources, out, profile=PREVIEW, cache_dir=tmp_path / "cache")
    codes = read_frame_codes(out)
    assert codes[:15] == list(range(60, 75))
    # Overlay frames show the HDR clip from 1 s in (29.97 fps source, within one frame).
    for k, code in enumerate(codes[15:30]):
        assert abs(code - (30 + k)) <= 1
    assert codes[30:60] == list(range(240, 270))
    check = verify_output(out, t, PREVIEW)
    assert check.ok, check.errors


def test_timeline_starting_with_a_silent_clip_keeps_full_length(sources, tmp_path):
    # Regression: audio that starts mid-timeline used to shorten the mix, and the
    # final mux cut the picture down to the audio's length.
    t = empty_timeline("p", "e1")
    t = primary_shot(
        t,
        clip_id="s1",
        asset_id="silent",
        beat_id="b1",
        source_in_seconds=Fraction(0),
        duration_seconds=Fraction(2),
        with_audio=False,
    )
    t = primary_shot(
        t,
        clip_id="s2",
        asset_id="land",
        beat_id="b2",
        source_in_seconds=Fraction(1),
        duration_seconds=Fraction(1),
    )
    out = tmp_path / "late_audio.mp4"
    render(t, sources, out, profile=PREVIEW, cache_dir=tmp_path / "cache")
    check = verify_output(out, t, PREVIEW)
    assert check.ok, check.errors
    assert check.frames == 90
    assert check.duration_seconds == pytest.approx(3, abs=0.05)
