from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Beat(BaseModel):
    id: str
    title: str
    purpose: str
    budget_ms: int = Field(gt=0)
    candidate_moment_ids: list[str]
    """Ranked by the beat planner; the solver makes the final picks."""


class Proposal(BaseModel):
    id: str
    kind: Literal["chronological", "experience", "informative"]
    premise: str
    audience: str
    target_ms: int
    beats: list[Beat]
    gaps: list[str] = []
    planner_id: str


class LockedShot(BaseModel):
    """A clip the creator locked in an earlier edit version."""

    clip_id: str
    beat_id: str
    moment_id: str | None
    asset_id: str
    in_ms: int
    out_ms: int
    position_locked: bool = False
    position_index: int | None = None


class Shot(BaseModel):
    beat_id: str
    moment_id: str
    asset_id: str
    in_ms: int
    out_ms: int
    reason: str
    """Why the solver chose it; shown on the storyboard card."""
    locked: bool = False
    overlay_offset_ms: int | None = None
    """Set for supporting video laid over the previous primary shot."""

    @property
    def ms(self) -> int:
        return self.out_ms - self.in_ms


class ShotPlan(BaseModel):
    proposal_id: str
    shots: list[Shot]
    conflicts: list[str] = []
    total_ms: int
