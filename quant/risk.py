"""Hard risk layer. Deterministic; no engine output can override it.

Checked before every order:
  1. kill switch (file on disk or latched in memory) -> no orders except flatten
  2. max drawdown from peak -> flatten and latch kill switch
  3. max daily loss (UTC day) -> flatten, no new entries until next day
  4. max position (|notional| / equity) -> clamp
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DAY_MS = 86_400_000


@dataclass
class RiskLimits:
    max_drawdown: float = 0.15
    max_daily_loss: float = 0.04
    max_position: float = 0.50  # |notional| / equity
    kill_switch_path: str = "KILL"  # create this file to halt trading

    def __post_init__(self) -> None:
        if not 0 < self.max_drawdown <= 0.15:
            raise ValueError("max_drawdown must be in (0, 0.15]")
        if not 0 < self.max_daily_loss < self.max_drawdown:
            raise ValueError("max_daily_loss must be in (0, max_drawdown)")
        if not 0 < self.max_position <= 1.0:
            raise ValueError("max_position must be in (0, 1.0]; no leverage")


@dataclass(frozen=True)
class RiskVerdict:
    allow_new_risk: bool
    force_flatten: bool
    max_fraction: float  # cap on |target fraction|
    reason: str


class RiskManager:
    def __init__(self, limits: RiskLimits, starting_equity: float) -> None:
        self.limits = limits
        self.peak_equity = starting_equity
        self._day: int | None = None
        self._day_start_equity = starting_equity
        self.killed: str | None = None  # latched reason; only a human resets

    def kill(self, reason: str) -> None:
        if self.killed is None:
            self.killed = reason

    def reset_kill(self) -> None:
        """Operator action only."""
        self.killed = None

    def update(self, ts: int, equity: float) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        day = ts // DAY_MS
        if day != self._day:
            self._day = day
            self._day_start_equity = equity

    def drawdown(self, equity: float) -> float:
        return max(0.0, 1 - equity / self.peak_equity) if self.peak_equity > 0 else 0.0

    def check(self, ts: int, equity: float) -> RiskVerdict:
        self.update(ts, equity)
        L = self.limits
        if os.path.exists(L.kill_switch_path):
            self.kill(f"kill switch file {L.kill_switch_path!r} present")
        if equity <= 0:
            self.kill("equity <= 0")
        if self.drawdown(equity) >= L.max_drawdown:
            self.kill(f"max drawdown {L.max_drawdown:.0%} breached")
        if self.killed:
            return RiskVerdict(False, True, 0.0, f"KILLED: {self.killed}")
        day_loss = 1 - equity / self._day_start_equity if self._day_start_equity > 0 else 0.0
        if day_loss >= L.max_daily_loss:
            return RiskVerdict(False, True, 0.0, f"daily loss {day_loss:.2%} >= {L.max_daily_loss:.0%}")
        return RiskVerdict(True, False, L.max_position, "ok")
