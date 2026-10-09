"""Entry gate and position sizing. Pure functions; every threshold lives here."""
from __future__ import annotations

from dataclasses import dataclass

from quant.schema import Decision, Direction, Regime, RiskState

MAX_KELLY_FRACTION = 0.25  # hard cap: never more than quarter Kelly


@dataclass
class PolicyConfig:
    min_setup_quality: int = 2
    min_confidence: float = 0.80
    escalate_below_confidence: float = 0.60
    kelly_fraction: float = 0.25
    # Payoff ratio b = take_profit / stop_loss, used by Kelly.
    take_profit_bps: float = 150.0
    stop_loss_bps: float = 100.0

    def __post_init__(self) -> None:
        if not 0 < self.kelly_fraction <= MAX_KELLY_FRACTION:
            raise ValueError(f"kelly_fraction must be in (0, {MAX_KELLY_FRACTION}]")


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str


def gate(d: Decision, cfg: PolicyConfig) -> GateResult:
    if d.error:
        return GateResult(False, f"engine error: {d.error}")
    if d.direction == Direction.NEUTRAL:
        return GateResult(False, "neutral")
    if d.regime == Regime.CRISIS:
        return GateResult(False, "crisis regime")
    if d.toxic_flow:
        return GateResult(False, "toxic flow")
    if d.setup_quality < cfg.min_setup_quality:
        return GateResult(False, f"setup_quality {d.setup_quality} < {cfg.min_setup_quality}")
    if d.direction_confidence <= cfg.min_confidence:
        return GateResult(False, f"confidence {d.direction_confidence:.2f} <= {cfg.min_confidence}")
    if d.risk_state != RiskState.SAFE:
        return GateResult(False, f"risk_state {d.risk_state.value}")
    return GateResult(True, "pass")


def needs_escalation(d: Decision, prev: Decision | None, cfg: PolicyConfig) -> str | None:
    """Reason to hand this to a deep review, on the transition only:
    regime flips into crisis, or directional confidence drops below the floor."""
    was_crisis = prev is not None and prev.regime == Regime.CRISIS
    if d.regime == Regime.CRISIS and not was_crisis:
        return "regime flipped to crisis"
    low = d.direction != Direction.NEUTRAL and d.direction_confidence < cfg.escalate_below_confidence
    was_low = prev is not None and prev.direction != Direction.NEUTRAL and prev.direction_confidence < cfg.escalate_below_confidence
    if low and not was_low:
        return f"confidence dropped to {d.direction_confidence:.2f} < {cfg.escalate_below_confidence}"
    return None


def kelly_fraction(p: float, b: float) -> float:
    """Full Kelly for a binary bet: win prob p, win/loss payoff ratio b."""
    if b <= 0:
        return 0.0
    return max(0.0, p - (1 - p) / b)


def target_fraction(d: Decision, cfg: PolicyConfig) -> float:
    """Signed fraction of equity to hold. Risk layer caps it further."""
    b = cfg.take_profit_bps / cfg.stop_loss_bps
    f = kelly_fraction(d.direction_confidence, b) * min(cfg.kelly_fraction, MAX_KELLY_FRACTION)
    return d.direction.sign * f
