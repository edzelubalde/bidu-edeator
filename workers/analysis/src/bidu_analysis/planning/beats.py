"""Beat planning: the story layer (spec section 5, "Plan at two levels").

``BeatPlanner`` is the seam for the reasoning-model planner. The offline
planner here builds an honest chronological outline without a model: it
groups footage by recording order and never names places or events it
cannot support.
"""

from __future__ import annotations

import math
from typing import Protocol

from ..models import Moment
from .models import Beat, Proposal

MAX_BEATS = 8
SECONDS_PER_BEAT = 30


class BeatPlanner(Protocol):
    planner_id: str

    def propose(
        self,
        moments: list[Moment],
        asset_order: list[str],
        target_ms: int,
        excluded: set[str],
        brief: str | None,
    ) -> list[Proposal]: ...


def usable(moments: list[Moment], excluded: set[str]) -> list[Moment]:
    out = []
    for m in moments:
        if m.moment_id in excluded:
            continue
        technical = m.quality.technical if m.quality else 0.5
        if technical < 0.15 and not m.has_speech:
            continue
        out.append(m)
    return out


def rank(m: Moment) -> float:
    technical = m.quality.technical if m.quality else 0.5
    return 0.55 * m.relevance + 0.3 * technical + (0.15 if m.has_speech else 0.0)


class ChronologicalPlanner:
    planner_id = "offline-chronological-1"

    def propose(self, moments, asset_order, target_ms, excluded, brief=None):
        pool = usable(moments, excluded)
        gaps: list[str] = []
        if not pool:
            return []
        available = sum(m.source_range_ms.ms for m in pool)
        target = target_ms
        if available < target_ms:
            target = int(available * 0.8)
            gaps.append(
                f"There is about {available // 1000}s of usable footage, not enough for "
                f"{target_ms // 1000}s. This outline is a shorter {target // 1000}s edit."
            )
        if not any(m.has_speech for m in pool):
            gaps.append("No speech was found, so this is a visual story carried by ambient sound.")

        by_asset: dict[str, list[Moment]] = {}
        for m in pool:
            by_asset.setdefault(m.asset_id, []).append(m)
        assets = [a for a in asset_order if a in by_asset]
        n_beats = max(1, min(MAX_BEATS, len(assets), math.ceil(target / 1000 / SECONDS_PER_BEAT)))
        groups = [
            assets[round(i * len(assets) / n_beats) : round((i + 1) * len(assets) / n_beats)]
            for i in range(n_beats)
        ]

        weights = [math.sqrt(sum(m.source_range_ms.ms for a in g for m in by_asset[a])) for g in groups]
        total_w = sum(weights)
        beats = []
        for i, (group, w) in enumerate(zip(groups, weights, strict=True)):
            ms = [m for a in group for m in by_asset[a]]
            ranked = sorted(ms, key=rank, reverse=True)
            title = (
                "Opening" if i == 0 else ("Closing" if i == n_beats - 1 and n_beats > 2 else f"Part {i + 1}")
            )
            beats.append(
                Beat(
                    id=f"beat_{i + 1}",
                    title=title,
                    purpose=f"Recorded section {i + 1} of {n_beats}, in shooting order ({len(group)} clips)",
                    budget_ms=max(1500, int(target * w / total_w)),
                    candidate_moment_ids=[m.moment_id for m in ranked],
                )
            )
        return [
            Proposal(
                id="proposal_chronological",
                kind="chronological",
                premise="The day as it happened, in the order it was filmed.",
                audience="Viewers following along with the trip",
                target_ms=target,
                beats=beats,
                gaps=gaps,
                planner_id=self.planner_id,
            )
        ]
