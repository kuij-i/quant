"""Typed decision schema shared by every decision engine.

An engine only *judges*. It returns a Decision. Code (policy.py, risk.py)
owns every threshold, size and side effect.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Regime(str, Enum):
    TRENDING = "trending"
    MEAN_REVERTING = "mean_reverting"
    HIGH_VOL = "high_vol"
    CRISIS = "crisis"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"

    @property
    def sign(self) -> int:
        return {"long": 1, "short": -1, "neutral": 0}[self.value]


class RiskState(str, Enum):
    SAFE = "safe"
    NEAR_LIMIT = "near_limit"
    REDUCE = "reduce"

    @property
    def severity(self) -> int:
        return {"safe": 0, "near_limit": 1, "reduce": 2}[self.value]


@dataclass(frozen=True)
class Decision:
    regime: Regime
    direction: Direction
    toxic_flow: bool
    setup_quality: int  # 0..3
    risk_state: RiskState
    # Probability that `direction` is right. Used for gating and Kelly sizing.
    direction_confidence: float
    engine: str = "unknown"
    # Set when the engine failed and this is a safe default, never traded on.
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 <= self.setup_quality <= 3:
            raise ValueError(f"setup_quality out of range: {self.setup_quality}")
        if not 0.0 <= self.direction_confidence <= 1.0:
            raise ValueError(f"direction_confidence out of range: {self.direction_confidence}")

    @classmethod
    def neutral(cls, engine: str, error: str | None = None) -> "Decision":
        """Safe default: no trade."""
        return cls(
            regime=Regime.HIGH_VOL,
            direction=Direction.NEUTRAL,
            toxic_flow=True,
            setup_quality=0,
            risk_state=RiskState.REDUCE,
            direction_confidence=0.0,
            engine=engine,
            error=error,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("regime", "direction", "risk_state"):
            d[k] = getattr(self, k).value
        return d
