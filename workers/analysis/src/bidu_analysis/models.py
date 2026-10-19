"""Analysis records (spec section 6). Times are canonical source milliseconds."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ShotRole = Literal["establishing", "action", "detail", "reaction", "dialogue", "transition", "other"]


class RangeMs(BaseModel):
    in_: int = Field(alias="in", ge=0)
    out: int = Field(gt=0)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _ordered(self) -> RangeMs:
        if self.out <= self.in_:
            raise ValueError("range must have positive duration")
        return self

    @property
    def ms(self) -> int:
        return self.out - self.in_

    def overlap(self, other: RangeMs) -> int:
        return max(0, min(self.out, other.out) - max(self.in_, other.in_))


class Word(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    probability: float | None = None


class TranscriptSegment(BaseModel):
    id: str
    asset_id: str
    analysis_run_id: str
    start_ms: int
    end_ms: int
    text: str
    language: str | None = None
    words: list[Word] = []
    uncertain: bool = False
    """True when the recognizer was unsure; the creator can correct it."""


class Evidence(BaseModel):
    type: Literal["video", "audio", "transcript"]
    start_ms: int
    end_ms: int


class Inference(BaseModel):
    text: str
    certainty: Literal["likely", "uncertain"]


class Quality(BaseModel):
    sharpness: float = Field(ge=0, le=1)
    exposure: float = Field(ge=0, le=1)
    """1 is well exposed; low values are too dark or blown out."""
    speech: float | None = Field(default=None, ge=0, le=1)

    @property
    def technical(self) -> float:
        parts = [self.sharpness, self.exposure]
        if self.speech is not None:
            parts.append(self.speech)
        return sum(parts) / len(parts)


class Moment(BaseModel):
    """One evidence-linked span of footage. Observations are things seen or heard;
    inferences are interpretations and carry their certainty."""

    moment_id: str
    asset_id: str
    analysis_run_id: str
    source_range_ms: RangeMs
    observations: list[str]
    evidence: list[Evidence]
    inferences: list[Inference] = []
    transcript_segment_ids: list[str] = []
    shot_role: ShotRole = "other"
    has_speech: bool = False
    relevance: float = Field(default=0.5, ge=0, le=1)
    quality: Quality | None = None
    labels: list[str] = []
    location: str | None = None
    """Only set when footage or creator notes support it; never guessed."""


class AnalysisRun(BaseModel):
    id: str
    asset_id: str
    input_hash: str
    model_id: str
    prompt_version: str
    schema_version: str
    status: Literal["completed", "failed"]
    windows: list[tuple[int, int]] = []
    usage: dict[str, float] = {}
    raw_output: list[dict] = []
    validation_errors: list[str] = []


class AssetAnalysis(BaseModel):
    run: AnalysisRun
    transcript: list[TranscriptSegment]
    moments: list[Moment]
