"""Exact time arithmetic.

All timeline positions are integer frames at the project rate, and all source
positions are integer frames at a stated rate. Conversions go through
``fractions.Fraction`` so nothing drifts the way float seconds do.
"""

from __future__ import annotations

from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field


def round_half_up(x: Fraction) -> int:
    """Round to the nearest integer, ties away from zero (stable for frame snapping)."""
    if x >= 0:
        return int(x + Fraction(1, 2))
    return -int(-x + Fraction(1, 2))


class Rate(BaseModel):
    """A frame rate as an exact ratio, e.g. 30000/1001 for 29.97."""

    model_config = ConfigDict(frozen=True)

    numerator: int = Field(gt=0)
    denominator: int = Field(default=1, gt=0)

    @property
    def fps(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    @property
    def frame_seconds(self) -> Fraction:
        return Fraction(self.denominator, self.numerator)

    def frames_to_seconds(self, frames: int) -> Fraction:
        return frames * self.frame_seconds

    def seconds_to_frames(self, seconds: Fraction) -> int:
        """Nearest whole frame for an exact time."""
        return round_half_up(Fraction(seconds) * self.fps)

    @classmethod
    def parse(cls, text: str) -> Rate:
        """Parse ffprobe-style rates such as ``"30000/1001"`` or ``"30"``."""
        if "/" in text:
            num, den = text.split("/", 1)
            frac = Fraction(int(num), int(den))
        else:
            frac = Fraction(text)
        return cls(numerator=frac.numerator, denominator=frac.denominator)


class RationalTime(BaseModel):
    """``value`` frames at ``rate_num/rate_den`` frames per second."""

    model_config = ConfigDict(frozen=True)

    value: int
    rate_num: int = Field(gt=0)
    rate_den: int = Field(default=1, gt=0)

    @property
    def rate(self) -> Rate:
        return Rate(numerator=self.rate_num, denominator=self.rate_den)

    @property
    def seconds(self) -> Fraction:
        return Fraction(self.value * self.rate_den, self.rate_num)

    @property
    def ms(self) -> int:
        return round_half_up(self.seconds * 1000)

    @classmethod
    def from_seconds(cls, seconds: Fraction | int, rate: Rate) -> RationalTime:
        return cls(
            value=rate.seconds_to_frames(Fraction(seconds)),
            rate_num=rate.numerator,
            rate_den=rate.denominator,
        )

    @classmethod
    def from_ms(cls, ms: int, rate: Rate) -> RationalTime:
        return cls.from_seconds(Fraction(ms, 1000), rate)

    def rescaled(self, rate: Rate) -> RationalTime:
        return RationalTime.from_seconds(self.seconds, rate)

    def __add__(self, other: RationalTime) -> RationalTime:
        if (self.rate_num, self.rate_den) == (other.rate_num, other.rate_den):
            return self.model_copy(update={"value": self.value + other.value})
        return RationalTime.from_seconds(self.seconds + other.seconds, self.rate)
