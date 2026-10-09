"""Deterministic baseline engine. No network, no model.

Its confidence is a heuristic mapping of signal strength, NOT a calibrated
probability. The nightly review measures how well it is calibrated; do not
trust the sizing until that review says the buckets line up.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from quant.schema import Decision, Direction, Regime, RiskState
from quant.snapshot import Snapshot


@dataclass
class RulesConfig:
    warmup_bars: int = 50
    crisis_rv_bps: float = 150.0  # per-bar vol that counts as crisis
    high_vol_rv_bps: float = 80.0
    trend_threshold_bps: float = 30.0  # |ema_fast - ema_slow| / mid
    mr_z_threshold: float = 2.0
    toxic_spread_bps: float = 15.0
    toxic_imbalance: float = 0.6
    near_limit_drawdown: float = 0.08
    reduce_drawdown: float = 0.12


class RulesEngine:
    name = "rules"

    def __init__(self, cfg: RulesConfig | None = None) -> None:
        self.cfg = cfg or RulesConfig()

    def decide(self, s: Snapshot) -> Decision:
        try:
            return self._decide(s)
        except Exception as e:  # engines must never raise into the loop
            return Decision.neutral(self.name, error=f"{type(e).__name__}: {e}")

    def _decide(self, s: Snapshot) -> Decision:
        c = self.cfg
        if s.bars < c.warmup_bars:
            return Decision.neutral(self.name, error="warming up")

        if s.rv_bps >= c.crisis_rv_bps:
            regime = Regime.CRISIS
        elif s.rv_bps >= c.high_vol_rv_bps:
            regime = Regime.HIGH_VOL
        elif abs(s.trend_bps) >= c.trend_threshold_bps:
            regime = Regime.TRENDING
        else:
            regime = Regime.MEAN_REVERTING

        direction, strength, quality = Direction.NEUTRAL, 0.0, 0
        if regime == Regime.TRENDING:
            direction = Direction.LONG if s.trend_bps > 0 else Direction.SHORT
            strength = abs(s.trend_bps) / c.trend_threshold_bps
            quality = 1
            if math.copysign(1, s.ret_n_bps) == direction.sign:  # momentum agrees
                quality += 1
            if s.imbalance is not None and s.imbalance * direction.sign > 0.1:  # book agrees
                quality += 1
        elif regime == Regime.MEAN_REVERTING and abs(s.zscore) >= c.mr_z_threshold:
            direction = Direction.SHORT if s.zscore > 0 else Direction.LONG
            strength = abs(s.zscore) / c.mr_z_threshold
            quality = 2 if abs(s.zscore) >= 1.5 * c.mr_z_threshold else 1
            if s.imbalance is not None and s.imbalance * direction.sign > 0.1:
                quality += 1

        toxic = False
        if s.spread_bps is not None and s.spread_bps >= c.toxic_spread_bps:
            toxic = True
        if s.imbalance is not None and direction != Direction.NEUTRAL:
            if s.imbalance * direction.sign <= -c.toxic_imbalance:
                toxic = True

        if s.drawdown >= c.reduce_drawdown:
            risk = RiskState.REDUCE
        elif s.drawdown >= c.near_limit_drawdown:
            risk = RiskState.NEAR_LIMIT
        else:
            risk = RiskState.SAFE

        # Heuristic: 0.5 at no signal, saturating toward ~0.9 at strong signal.
        conf = 0.5 + 0.4 * math.tanh(max(0.0, strength - 1.0) + 0.5 * quality / 3) if direction != Direction.NEUTRAL else 0.5

        return Decision(
            regime=regime,
            direction=direction,
            toxic_flow=toxic,
            setup_quality=min(3, quality),
            risk_state=risk,
            direction_confidence=round(conf, 4),
            engine=self.name,
        )
