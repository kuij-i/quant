"""The trading loop. One code path for backtest and live paper trading.

Timing (causal):
  bar t closes -> snapshot(t) -> engine decides -> policy/risk -> pending order
  bar t+1 opens -> pending order fills at open (+costs)
  during bar t+1 -> stop-loss / take-profit checked against high/low
                    (if both are touched in one bar, assume the stop hit first)
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from quant.broker import PaperBroker
from quant.engines.base import DecisionEngine
from quant.journal import Journal
from quant.policy import PolicyConfig, gate, needs_escalation, target_fraction
from quant.risk import RiskManager
from quant.schema import Decision, RiskState
from quant.snapshot import Candle, OrderBook, StateEngine


@dataclass
class AgentConfig:
    max_hold_bars: int = 24
    near_limit_at: float = 0.5  # fraction of max_drawdown that counts as near_limit


class Agent:
    def __init__(
        self,
        state: StateEngine,
        engine: DecisionEngine,
        broker: PaperBroker,
        risk: RiskManager,
        policy: PolicyConfig,
        cfg: AgentConfig | None = None,
        journal: Journal | None = None,
    ) -> None:
        self.state, self.engine, self.broker, self.risk = state, engine, broker, risk
        self.policy = policy
        self.cfg = cfg or AgentConfig()
        self.journal = journal or Journal(None)
        self._pending: tuple[float, str] | None = None  # (signed target fraction, reason)
        self._bars_held = 0
        self._half_spread_bps: float | None = None
        self.escalations = 0
        self._last_close = 0.0
        self._prev: Decision | None = None

    # ---- public -------------------------------------------------------------

    def on_bar(self, c: Candle, book: OrderBook | None = None) -> Decision | None:
        self._execute_pending(c)
        self._check_brackets(c)

        self.state.on_candle(c)
        self._last_close = c.close
        if book is not None:
            self.state.on_book(book)
        equity = self.broker.equity(c.close)
        verdict = self.risk.check(c.ts, equity)
        self.journal.write("equity", {"ts": c.ts, "equity": equity, "qty": self.broker.qty, "close": c.close})

        if verdict.force_flatten:
            if self.broker.qty != 0:
                self._pending = (0.0, f"risk: {verdict.reason}")
            self.journal.write("risk", {"ts": c.ts, "reason": verdict.reason})
            return None

        snap_ts = max(c.ts, book.ts) if book is not None else c.ts
        snap = self.state.snapshot(snap_ts, self.broker.qty, equity, self.risk.peak_equity)
        self._half_spread_bps = snap.spread_bps / 2 if snap.spread_bps is not None else None
        d = self.engine.decide(snap)
        d = self._apply_code_risk_state(d, equity)

        esc = needs_escalation(d, self._prev, self.policy)
        self._prev = d
        if esc:
            self.escalations += 1
            self.journal.write("escalations", {"ts": c.ts, "reason": esc, "snapshot": snap.to_prompt(), "decision": d.to_dict()})

        g = gate(d, self.policy)
        target, why = self._target(d, g.passed, verdict.max_fraction)
        if target is not None:
            self._pending = (target, why)

        self.journal.write(
            "decisions",
            {
                "ts": c.ts,
                "close": c.close,
                "snapshot": snap.to_prompt(),
                "decision": d.to_dict(),
                "gate": g.reason,
                "target": target,
                "escalation": esc,
            },
        )
        if self.broker.qty != 0:
            self._bars_held += 1
        return d

    # ---- internals ----------------------------------------------------------

    def _apply_code_risk_state(self, d: Decision, equity: float) -> Decision:
        """The code's own view of risk can only make the engine's view stricter."""
        dd = self.risk.drawdown(equity)
        code = RiskState.NEAR_LIMIT if dd >= self.cfg.near_limit_at * self.risk.limits.max_drawdown else RiskState.SAFE
        if code.severity > d.risk_state.severity:
            return dataclasses.replace(d, risk_state=code)
        return d

    def _target(self, d: Decision, passed: bool, cap: float) -> tuple[float | None, str]:
        qty = self.broker.qty
        if d.regime.value == "crisis" and qty != 0:
            return 0.0, "crisis: flatten"
        if passed:
            f = target_fraction(d, self.policy)
            f = max(-cap, min(cap, f))
            same_side = qty != 0 and (qty > 0) == (f > 0)
            if same_side:
                return None, "hold: already positioned"
            return f, f"entry {d.direction.value} p={d.direction_confidence:.2f}"
        if qty != 0 and d.risk_state == RiskState.REDUCE:
            return 0.5 * self._current_fraction(), "reduce"
        if qty != 0 and self._bars_held >= self.cfg.max_hold_bars:
            return 0.0, "time exit"
        return None, "no change"

    def _current_fraction(self) -> float:
        px = self._last_close
        eq = self.broker.equity(px)
        return self.broker.qty * px / eq if eq > 0 else 0.0

    def _execute_pending(self, c: Candle) -> None:
        if self._pending is None:
            return
        target, reason = self._pending
        self._pending = None
        # Re-check risk at the moment of execution; only risk-reducing orders pass a kill.
        equity = self.broker.equity(c.open)
        verdict = self.risk.check(c.ts - 1, equity)
        if not verdict.allow_new_risk and abs(target) > 0:
            target, reason = 0.0, f"risk at execution: {verdict.reason}"
        if equity <= 0:
            return
        target_qty = target * equity / c.open
        delta = target_qty - self.broker.qty
        if abs(delta) * c.open < 1e-9 * equity:
            return
        prev = self.broker.qty
        f = self.broker.market_order(c.ts, delta, c.open, reason, self._half_spread_bps)
        if f:
            if prev == 0 or (prev > 0) != (self.broker.qty > 0):
                self._bars_held = 0
            self.journal.write("fills", dataclasses.asdict(f))

    def _check_brackets(self, c: Candle) -> None:
        qty, entry = self.broker.qty, self.broker.entry_price
        if qty == 0 or entry is None:
            return
        sl, tp = self.policy.stop_loss_bps / 1e4, self.policy.take_profit_bps / 1e4
        if qty > 0:
            stop, take = entry * (1 - sl), entry * (1 + tp)
            hit_stop, hit_take = c.low <= stop, c.high >= take
            stop_px = min(stop, c.open)  # gap through the stop fills at the open
            take_px = max(take, c.open) if c.open >= take else take
        else:
            stop, take = entry * (1 + sl), entry * (1 - tp)
            hit_stop, hit_take = c.high >= stop, c.low <= take
            stop_px = max(stop, c.open)
            take_px = min(take, c.open) if c.open <= take else take
        if hit_stop:
            f = self.broker.flatten(c.ts, stop_px, "stop loss", self._half_spread_bps)
        elif hit_take:
            f = self.broker.flatten(c.ts, take_px, "take profit", self._half_spread_bps)
        else:
            return
        self._pending = None
        if f:
            self.journal.write("fills", dataclasses.asdict(f))
