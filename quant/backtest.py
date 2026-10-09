"""Replay candles through the exact same Agent used for paper trading."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

from quant.agent import Agent
from quant.broker import PaperBroker
from quant.config import Config
from quant.engines.base import DecisionEngine
from quant.journal import Journal
from quant.risk import RiskManager
from quant.snapshot import Candle, StateEngine


@dataclass
class BacktestResult:
    bars: int
    start_equity: float
    end_equity: float
    total_return: float
    buy_hold_return: float
    max_drawdown: float
    sharpe_per_bar: float
    trades: int
    fees: float
    escalations: int
    killed: str | None

    def summary(self) -> str:
        return "\n".join(
            [
                f"bars            {self.bars}",
                f"return          {self.total_return:+.2%}   (buy & hold {self.buy_hold_return:+.2%})",
                f"max drawdown    {self.max_drawdown:.2%}",
                f"sharpe/bar      {self.sharpe_per_bar:.4f}",
                f"fills           {self.trades}",
                f"fees paid       {self.fees:,.2f}",
                f"escalations     {self.escalations}",
                f"kill switch     {self.killed or 'not triggered'}",
            ]
        )


def build_agent(cfg: Config, engine: DecisionEngine, run_dir: str | None) -> Agent:
    return Agent(
        state=StateEngine(cfg.symbol),
        engine=engine,
        broker=PaperBroker(cfg.starting_equity, cfg.costs),
        risk=RiskManager(cfg.risk, cfg.starting_equity),
        policy=cfg.policy,
        cfg=cfg.agent,
        journal=Journal(run_dir),
    )


def run_backtest(candles: list[Candle], cfg: Config, engine: DecisionEngine, run_dir: str | None = None) -> BacktestResult:
    agent = build_agent(cfg, engine, run_dir)
    if run_dir:
        with open(os.path.join(run_dir, "config.json"), "w") as f:
            json.dump(cfg.to_dict(), f, indent=2)
    equity, peak, max_dd = [], cfg.starting_equity, 0.0
    try:
        for c in candles:
            agent.on_bar(c)
            e = agent.broker.equity(c.close)
            equity.append(e)
            peak = max(peak, e)
            max_dd = max(max_dd, 1 - e / peak)
    finally:
        agent.journal.close()

    rets = [b / a - 1 for a, b in zip(equity, equity[1:]) if a > 0]
    mu = sum(rets) / len(rets) if rets else 0.0
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    end = equity[-1] if equity else cfg.starting_equity
    return BacktestResult(
        bars=len(candles),
        start_equity=cfg.starting_equity,
        end_equity=end,
        total_return=end / cfg.starting_equity - 1,
        buy_hold_return=candles[-1].close / candles[0].open - 1 if candles else 0.0,
        max_drawdown=max_dd,
        sharpe_per_bar=mu / sd if sd > 0 else 0.0,
        trades=len(agent.broker.fills),
        fees=agent.broker.total_fees,
        escalations=agent.escalations,
        killed=agent.risk.killed,
    )
