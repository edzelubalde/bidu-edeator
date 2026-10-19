from fractions import Fraction

from bidu_analysis.models import Evidence, Moment, Quality, RangeMs, TranscriptSegment
from bidu_analysis.planning import ChronologicalPlanner, LockedShot, assemble, solve
from bidu_analysis.segment import make_windows
from bidu_timeline import SourceInfo, validate_timeline

GOOD = Quality(sharpness=0.8, exposure=0.8)


def moment(mid, asset, a, b, *, speech=None, relevance=0.5):
    return Moment(
        moment_id=mid,
        asset_id=asset,
        analysis_run_id="run",
        source_range_ms=RangeMs(in_=a, out=b),
        observations=[f"{mid} observation"],
        evidence=[Evidence(type="video", start_ms=a, end_ms=b)],
        transcript_segment_ids=speech or [],
        has_speech=bool(speech),
        relevance=relevance,
        shot_role="dialogue" if speech else "other",
        quality=GOOD,
    )


def seg(sid, asset, a, b, text):
    return TranscriptSegment(id=sid, asset_id=asset, analysis_run_id="run", start_ms=a, end_ms=b, text=text)


def shoot():
    """Two clips: a talking clip with three sentences, and a silent b-roll clip."""
    transcript = {
        "t1": seg("t1", "talk", 1_000, 4_000, "Andito na tayo sa market."),
        "t2": seg("t2", "talk", 4_500, 9_000, "Ang daming food stalls, grabe."),
        "t3": seg("t3", "talk", 9_500, 12_000, "Let's try the isaw."),
    }
    moments = {
        m.moment_id: m
        for m in [
            moment("m_talk", "talk", 0, 12_500, speech=["t1", "t2", "t3"], relevance=0.9),
            moment("m_broll1", "broll", 0, 6_000, relevance=0.6),
            moment("m_broll2", "broll", 6_000, 14_000, relevance=0.5),
            moment("m_broll3", "broll", 14_000, 20_000, relevance=0.4),
        ]
    }
    return moments, transcript


def plan_for(target_ms, **kw):
    moments, transcript = shoot()
    proposal = ChronologicalPlanner().propose(
        list(moments.values()), ["talk", "broll"], target_ms, kw.get("excluded", set()), None
    )[0]
    return proposal, solve(proposal, moments, transcript, **kw), moments, transcript


def test_windows_cover_everything_and_overlap():
    windows = make_windows(130_000, cuts=[44_000, 90_000])
    assert windows[0][0] == 0 and windows[-1][1] == 130_000
    assert windows[0][1] == 44_000  # snapped to the nearby cut
    for (_, b), (c, _) in zip(windows, windows[1:], strict=False):
        assert c < b  # overlap, so nothing falls between windows
    assert make_windows(20_000, []) == [(0, 20_000)]


def test_speech_is_never_cut_mid_phrase():
    _, plan, _, transcript = plan_for(20_000)
    talk = [s for s in plan.shots if s.asset_id == "talk"]
    assert talk
    for shot in talk:
        for t in transcript.values():
            inside = shot.in_ms <= t.start_ms and t.end_ms <= shot.out_ms
            outside = t.end_ms <= shot.in_ms or t.start_ms >= shot.out_ms
            assert inside or outside, (shot, t)


def test_runtime_hits_target_without_repeats():
    _, plan, _, _ = plan_for(20_000)
    assert abs(plan.total_ms - 20_000) <= 1_500
    primaries = [s for s in plan.shots if s.overlay_offset_ms is None]
    assert len({s.moment_id for s in plan.shots}) == len(plan.shots)
    assert sum(s.ms for s in primaries) == plan.total_ms


def test_long_dialogue_gets_a_cutaway():
    _, plan, _, _ = plan_for(20_000)
    overlays = [s for s in plan.shots if s.overlay_offset_ms is not None]
    assert overlays and overlays[0].asset_id == "broll"
    assert overlays[0].reason.startswith("Cutaway over dialogue")


def test_exclusions_are_hard_constraints():
    _, plan, _, _ = plan_for(20_000, excluded={"m_broll1", "m_talk"})
    assert {s.moment_id for s in plan.shots}.isdisjoint({"m_broll1", "m_talk"})


def test_locked_shot_survives_regeneration_and_conflicts_are_reported():
    lock = LockedShot(
        clip_id="clip_009",
        beat_id="beat_from_old_outline",
        moment_id="m_broll3",
        asset_id="broll",
        in_ms=15_000,
        out_ms=17_000,
    )
    _, plan, _, _ = plan_for(20_000, locked=[lock])
    kept = [s for s in plan.shots if s.locked]
    assert [(s.in_ms, s.out_ms) for s in kept] == [(15_000, 17_000)]
    assert any("Locked clip clip_009" in c for c in plan.conflicts)


def test_insufficient_footage_offers_a_shorter_edit():
    proposal, plan, _, _ = plan_for(120_000)
    assert proposal.target_ms < 120_000
    assert any("not enough" in g for g in proposal.gaps)


def test_solver_is_deterministic():
    assert plan_for(20_000)[1] == plan_for(20_000)[1]


def test_assembled_timeline_is_valid():
    _, plan, _, _ = plan_for(20_000)
    t = assemble(plan, project_id="p", edit_version_id="e", has_audio={"talk": True, "broll": False})
    report = validate_timeline(
        t,
        {
            "talk": SourceInfo(duration_seconds=Fraction(13)),
            "broll": SourceInfo(duration_seconds=Fraction(20), has_audio=False),
        },
    )
    assert report.ok, report.errors
    assert t.video("V2") and all(a.asset_id == "talk" for a in t.audio("A1"))
    # Dialogue keeps playing under the cutaway: the talk audio spans the overlay.
    o = t.video("V2")[0]
    assert any(
        a.timeline_start_frame <= o.timeline_start_frame and o.timeline_end_frame <= a.timeline_end_frame
        for a in t.audio("A1")
    )
