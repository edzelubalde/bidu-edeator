"""Shot selection: code, not the model, picks exact frames (spec section 5, "Select shots globally").

Given a beat plan, the solver assigns source in/out ranges under hard
constraints: total runtime, exclusions, locks and source bounds. It cuts
speech only at transcript boundaries, penalizes repeats and jumps between
clips, and lays supporting footage over long dialogue instead of cutting it.
Same inputs always give the same plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Moment, TranscriptSegment
from .beats import rank
from .models import LockedShot, Proposal, Shot, ShotPlan

MIN_SHOT_MS = 1_500
MAX_VISUAL_MS = 6_000
MAX_SPEECH_MS = 15_000
LEAD_IN_MS = 150
TAIL_MS = 250
OVERLAY_MIN_DIALOGUE_MS = 5_000
OVERLAY_MAX_MS = 3_000
NEAR_MS = 2_000


@dataclass
class _Used:
    ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    moments: set[str] = field(default_factory=set)

    def add(self, asset_id: str, moment_id: str, a: int, b: int) -> None:
        self.ranges.setdefault(asset_id, []).append((a, b))
        self.moments.add(moment_id)

    def overlaps(self, asset_id: str, a: int, b: int, pad: int = 0) -> bool:
        return any(min(b + pad, y) - max(a - pad, x) > 0 for x, y in self.ranges.get(asset_id, []))


def solve(
    proposal: Proposal,
    moments: dict[str, Moment],
    transcript: dict[str, TranscriptSegment],
    *,
    excluded: set[str] = frozenset(),
    locked: list[LockedShot] = (),
) -> ShotPlan:
    used = _Used()
    conflicts: list[str] = []
    beat_ids = [b.id for b in proposal.beats]
    locked_by_beat: dict[str, list[LockedShot]] = {}
    for lock in locked:
        beat = lock.beat_id
        if beat not in beat_ids:
            conflicts.append(
                f"Locked clip {lock.clip_id} was in a section this outline does not have; "
                "it was kept in the last section."
            )
            beat = beat_ids[-1]
        if lock.moment_id in excluded:
            conflicts.append(f"Locked clip {lock.clip_id} uses excluded footage; the lock wins.")
        locked_by_beat.setdefault(beat, []).append(lock)
        used.add(lock.asset_id, lock.moment_id or lock.clip_id, lock.in_ms, lock.out_ms)

    shots: list[Shot] = []
    carry = 0
    for beat in proposal.beats:
        remaining = beat.budget_ms + carry
        beat_shots: list[Shot] = []
        for lock in locked_by_beat.get(beat.id, []):
            beat_shots.append(
                Shot(
                    beat_id=beat.id,
                    moment_id=lock.moment_id or lock.clip_id,
                    asset_id=lock.asset_id,
                    in_ms=lock.in_ms,
                    out_ms=lock.out_ms,
                    reason="Locked by you",
                    locked=True,
                )
            )
            remaining -= lock.out_ms - lock.in_ms

        candidates = [
            moments[mid] for mid in beat.candidate_moment_ids if mid in moments and mid not in excluded
        ]
        while remaining >= MIN_SHOT_MS:
            prev = beat_shots[-1] if beat_shots else (shots[-1] if shots else None)
            best: tuple[float, Shot] | None = None
            for m in candidates:
                if m.moment_id in used.moments:
                    continue
                shot = _cut(m, transcript, remaining, beat.id)
                if shot is None or used.overlaps(m.asset_id, shot.in_ms, shot.out_ms):
                    continue
                score = rank(m)
                if prev is not None and prev.asset_id == m.asset_id:
                    score -= 0.05
                    if abs(shot.in_ms - prev.out_ms) < NEAR_MS:
                        score -= 0.15  # Near-duplicate of what was just on screen.
                if best is None or score > best[0]:
                    best = (score, shot)
            if best is None:
                break
            shot = best[1]
            beat_shots.append(shot)
            used.add(shot.asset_id, shot.moment_id, shot.in_ms, shot.out_ms)
            remaining -= shot.ms

        _position_locks(beat_shots, locked_by_beat.get(beat.id, []))
        for anchor, overlay in _overlays(beat_shots, candidates, used):
            # An overlay always follows the primary shot it covers.
            beat_shots.insert(beat_shots.index(anchor) + 1, overlay)
        shots.extend(beat_shots)
        carry = max(0, remaining)

    total = sum(s.ms for s in shots if s.overlay_offset_ms is None)
    if total < proposal.target_ms - 3000:
        conflicts.append(
            f"Only {total // 1000}s could be filled for a {proposal.target_ms // 1000}s target "
            "without repeating footage."
        )
    return ShotPlan(proposal_id=proposal.id, shots=shots, conflicts=conflicts, total_ms=total)


def _cut(m: Moment, transcript: dict[str, TranscriptSegment], room_ms: int, beat_id: str) -> Shot | None:
    lo, hi = m.source_range_ms.in_, m.source_range_ms.out
    if m.has_speech and m.transcript_segment_ids:
        segs = sorted(
            (transcript[t] for t in m.transcript_segment_ids if t in transcript), key=lambda s: s.start_ms
        )
        limit = min(room_ms, MAX_SPEECH_MS)
        chosen: list[TranscriptSegment] = []
        for seg in segs:
            start = max(lo, (chosen[0] if chosen else seg).start_ms - LEAD_IN_MS)
            end = min(hi, seg.end_ms + TAIL_MS)
            if end - start > limit:
                break
            chosen.append(seg)
        if not chosen:
            return None  # Never cut a phrase in half to fit.
        a = max(lo, chosen[0].start_ms - LEAD_IN_MS)
        b = min(hi, chosen[-1].end_ms + TAIL_MS)
        quoted = " ".join(s.text for s in chosen)
        reason = f'Complete line: "{quoted[:80]}"'
    else:
        length = min(hi - lo, MAX_VISUAL_MS, room_ms)
        if length < min(MIN_SHOT_MS, hi - lo) or length < 1000:
            return None
        mid = (lo + hi) // 2
        a = max(lo, mid - length // 2)
        b = a + length
        reason = m.observations[0] if m.observations else "Supporting footage"
    if b - a < 1000:
        return None
    return Shot(beat_id=beat_id, moment_id=m.moment_id, asset_id=m.asset_id, in_ms=a, out_ms=b, reason=reason)


def _position_locks(beat_shots: list[Shot], locks: list[LockedShot]) -> None:
    for lock in locks:
        if not lock.position_locked or lock.position_index is None:
            continue
        idx = next(
            (
                i
                for i, s in enumerate(beat_shots)
                if s.locked and s.in_ms == lock.in_ms and s.asset_id == lock.asset_id
            ),
            None,
        )
        if idx is None:
            continue
        shot = beat_shots.pop(idx)
        beat_shots.insert(min(lock.position_index, len(beat_shots)), shot)


def _overlays(beat_shots: list[Shot], candidates: list[Moment], used: _Used) -> list[tuple[Shot, Shot]]:
    """Cover part of a long talking shot with unused visuals; the dialogue keeps playing."""
    extra = []
    visuals = [m for m in candidates if not m.has_speech and m.moment_id not in used.moments]
    for shot in [s for s in beat_shots if s.overlay_offset_ms is None]:
        if shot.ms < OVERLAY_MIN_DIALOGUE_MS or shot.reason.startswith("Locked"):
            continue
        if not shot.reason.startswith("Complete line"):
            continue
        length = min(OVERLAY_MAX_MS, int(shot.ms * 0.4))
        for m in sorted(visuals, key=rank, reverse=True):
            if m.moment_id in used.moments or m.source_range_ms.ms < length:
                continue
            mid = (m.source_range_ms.in_ + m.source_range_ms.out) // 2
            a = max(m.source_range_ms.in_, mid - length // 2)
            if used.overlaps(m.asset_id, a, a + length):
                continue
            used.add(m.asset_id, m.moment_id, a, a + length)
            note = m.observations[0] if m.observations else "supporting footage"
            extra.append(
                (
                    shot,
                    Shot(
                        beat_id=shot.beat_id,
                        moment_id=m.moment_id,
                        asset_id=m.asset_id,
                        in_ms=a,
                        out_ms=a + length,
                        overlay_offset_ms=shot.ms // 3,
                        reason=f"Cutaway over dialogue: {note}",
                    ),
                )
            )
            break
    return extra
