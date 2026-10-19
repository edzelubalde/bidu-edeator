"""Stages 4-7 for one asset: segment, transcribe, understand, consolidate."""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

from bidu_media.normalize import TimeMap

from .models import AnalysisRun, AssetAnalysis, Evidence, Moment, RangeMs, TranscriptSegment, Word
from .quality import measure
from .segment import detect_cuts, make_windows
from .transcribe import Transcriber
from .video_model import PROMPT_VERSION, SCHEMA_VERSION, VideoModel, WindowUsage

MIN_MOMENT_MS = 500
MERGE_IOU = 0.5


def input_hash(asset_sha: str, transform_version: str, model: VideoModel, transcriber: Transcriber) -> str:
    """Analysis is reusable only when every input that shapes it matches (spec section 4)."""
    key = "|".join(
        [asset_sha, transform_version, model.model_id, PROMPT_VERSION, SCHEMA_VERSION, transcriber.model_id]
    )
    return hashlib.sha256(key.encode()).hexdigest()


def analyze_asset(
    *,
    asset_id: str,
    asset_sha: str,
    transform_version: str,
    proxy: Path,
    analysis_audio: Path | None,
    time_map: TimeMap,
    transcriber: Transcriber,
    model: VideoModel,
) -> AssetAnalysis:
    digest = input_hash(asset_sha, transform_version, model, transcriber)
    run_id = f"run_{digest[:16]}"
    duration_ms = int(time_map.source_duration_seconds * 1000)

    # Transcripts and windows are produced in proxy time, then mapped once.
    proxy_transcript = transcriber.transcribe(analysis_audio, asset_id, run_id) if analysis_audio else []
    cuts = detect_cuts(proxy)
    windows = make_windows(int(time_map.to_proxy(time_map.source_duration_seconds) * 1000), cuts)

    raw_output: list[dict] = []
    errors: list[str] = []
    usage = WindowUsage()
    found: list[tuple[int, int, object]] = []
    for w in windows:
        result, used = model.analyze_window(proxy, w, proxy_transcript, cuts)
        usage.input_tokens += used.input_tokens
        usage.output_tokens += used.output_tokens
        raw_output.append({"window_ms": list(w), **result.model_dump()})
        w_start, w_end = w
        for raw in result.moments:
            s = w_start + int(raw.start_s * 1000)
            e = min(w_end, w_start + int(raw.end_s * 1000))
            if e - s < MIN_MOMENT_MS:
                errors.append(
                    f"window {w}: dropped moment {raw.start_s}-{raw.end_s}s (outside window or too short)"
                )
                continue
            found.append((s, e, raw))

    transcript = [_to_source(seg, time_map) for seg in proxy_transcript]
    moments: list[Moment] = []
    for s, e, raw in _merge_window_seams(found):
        src_in = _ms(time_map.to_source(Fraction(s, 1000)))
        src_out = min(_ms(time_map.to_source(Fraction(e, 1000))), duration_ms)
        if src_out - src_in < MIN_MOMENT_MS:
            continue
        rng = RangeMs(in_=src_in, out=src_out)
        spoken = [t for t in transcript if min(t.end_ms, src_out) - max(t.start_ms, src_in) > 0]
        evidence = [Evidence(type="video", start_ms=src_in, end_ms=src_out)]
        evidence += [Evidence(type="transcript", start_ms=t.start_ms, end_ms=t.end_ms) for t in spoken]
        speech_score = None
        if spoken:
            speech_score = 0.5 if any(t.uncertain for t in spoken) else 0.9
        moments.append(
            Moment(
                moment_id=f"{run_id}-m{len(moments):04d}",
                asset_id=asset_id,
                analysis_run_id=run_id,
                source_range_ms=rng,
                observations=raw.observations + [f'Sign or text on screen: "{t}"' for t in raw.visible_text],
                evidence=evidence,
                inferences=raw.inferences,
                transcript_segment_ids=[t.id for t in spoken],
                shot_role=raw.shot_role,
                has_speech=bool(spoken),
                relevance=raw.relevance,
                quality=measure(proxy, s, e, speech=speech_score),
                location=raw.location_evidence,
            )
        )

    run = AnalysisRun(
        id=run_id,
        asset_id=asset_id,
        input_hash=digest,
        model_id=model.model_id,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        status="completed",
        windows=windows,
        usage={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "source_seconds": duration_ms / 1000,
        },
        raw_output=raw_output,
        validation_errors=errors,
    )
    return AssetAnalysis(run=run, transcript=transcript, moments=moments)


def _ms(seconds: Fraction) -> int:
    return int(round(seconds * 1000))


def _to_source(seg: TranscriptSegment, tm: TimeMap) -> TranscriptSegment:
    def conv(ms: int) -> int:
        return _ms(tm.to_source(Fraction(ms, 1000)))

    return seg.model_copy(
        update={
            "start_ms": conv(seg.start_ms),
            "end_ms": conv(seg.end_ms),
            "words": [
                Word(start_ms=conv(w.start_ms), end_ms=conv(w.end_ms), text=w.text, probability=w.probability)
                for w in seg.words
            ],
        }
    )


def _merge_window_seams(found):
    """Overlapping windows see the same moment twice; keep one, joined over both spans."""
    merged: list[list] = []
    for s, e, raw in sorted(found, key=lambda x: (x[0], x[1])):
        if merged:
            ps, pe, praw = merged[-1]
            inter = min(pe, e) - max(ps, s)
            union = max(pe, e) - min(ps, s)
            if inter > 0 and inter / union >= MERGE_IOU:
                keep = praw if praw.relevance >= raw.relevance else raw
                obs = list(dict.fromkeys(praw.observations + raw.observations))
                merged[-1] = [min(ps, s), max(pe, e), keep.model_copy(update={"observations": obs})]
                continue
        merged.append([s, e, raw])
    return [tuple(m) for m in merged]
