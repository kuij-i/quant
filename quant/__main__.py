"""CLI: python -m quant {backtest,fetch,paper,review,check-jev}"""
from __future__ import annotations

import argparse
import json
import os
import time

from quant.backtest import build_agent, run_backtest
from quant.config import load_config
from quant.engines import JevEngine, RulesEngine
from quant.review import run_review


def make_engine(name: str, cfg):
    if name == "rules":
        return RulesEngine(cfg.rules)
    if name == "jev":
        return JevEngine()
    raise SystemExit(f"unknown engine {name}")


def new_run_dir(base: str, label: str) -> str:
    d = os.path.join(base, time.strftime("%Y%m%d-%H%M%S") + "-" + label)
    os.makedirs(d, exist_ok=True)
    return d


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="quant")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest")
    src = b.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv")
    src.add_argument("--synthetic", type=int, metavar="BARS", help="synthetic data; plumbing test only")
    b.add_argument("--engine", default="rules", choices=["rules", "jev"])
    b.add_argument("--config")
    b.add_argument("--runs", default="runs")

    f = sub.add_parser("fetch", help="download public OHLCV via ccxt to CSV")
    f.add_argument("--exchange", default="binance")
    f.add_argument("--symbol", default="BTC/USDT")
    f.add_argument("--timeframe", default="1h")
    f.add_argument("--days", type=int, default=365)
    f.add_argument("--out", required=True)

    p = sub.add_parser("paper", help="live public data, simulated fills")
    p.add_argument("--exchange", default="binance")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--engine", default="rules", choices=["rules", "jev"])
    p.add_argument("--config")
    p.add_argument("--runs", default="runs")

    r = sub.add_parser("review")
    r.add_argument("run_dir")
    r.add_argument("--horizon", type=int, default=4)

    sub.add_parser("check-jev", help="one Jev call on a sample snapshot")

    a = ap.parse_args(argv)

    if a.cmd == "backtest":
        from quant.feed import load_csv, synthetic

        cfg = load_config(a.config)
        candles = load_csv(a.csv) if a.csv else synthetic(a.synthetic)
        run_dir = new_run_dir(a.runs, f"bt-{a.engine}")
        res = run_backtest(candles, cfg, make_engine(a.engine, cfg), run_dir)
        print(res.summary())
        if a.synthetic:
            print("\nNOTE: synthetic data -- this tests the plumbing, not the edge.")
        print(f"\nrun dir: {run_dir}\nnext: python -m quant review {run_dir}")

    elif a.cmd == "fetch":
        from quant.feed import fetch_ohlcv, save_csv

        since = int((time.time() - a.days * 86400) * 1000)
        candles = fetch_ohlcv(a.exchange, a.symbol, a.timeframe, since)
        save_csv(a.out, candles)
        print(f"wrote {len(candles)} candles to {a.out}")

    elif a.cmd == "paper":
        from quant.feed import live_bars

        cfg = load_config(a.config)
        run_dir = new_run_dir(a.runs, f"paper-{a.engine}")
        with open(os.path.join(run_dir, "config.json"), "w") as fh:
            json.dump(cfg.to_dict(), fh, indent=2)
        agent = build_agent(cfg, make_engine(a.engine, cfg), run_dir)
        print(f"paper trading {cfg.symbol} on {a.exchange} {a.timeframe}; journal in {run_dir}")
        print(f"halt any time: touch {cfg.risk.kill_switch_path}")
        try:
            for candle, book in live_bars(a.exchange, cfg.symbol, a.timeframe):
                d = agent.on_bar(candle, book)
                eq = agent.broker.equity(candle.close)
                print(f"{candle.ts} close={candle.close:.2f} eq={eq:.2f} qty={agent.broker.qty:.6f} "
                      f"{d.direction.value if d else '-'} p={d.direction_confidence if d else 0:.2f} "
                      f"{'KILLED: ' + agent.risk.killed if agent.risk.killed else ''}")
        except KeyboardInterrupt:
            pass
        finally:
            agent.journal.close()

    elif a.cmd == "review":
        rep = run_review(a.run_dir, a.horizon)
        print(open(os.path.join(a.run_dir, "review.md")).read())
        print(f"proposals: {len(rep['proposals'])} (see {a.run_dir}/proposals.json)")

    elif a.cmd == "check-jev":
        from quant.feed import synthetic
        from quant.snapshot import StateEngine

        st = StateEngine("BTC/USDT")
        for c in synthetic(200):
            st.on_candle(c)
        snap = st.snapshot(c.ts, 0.0, 10_000.0, 10_000.0)
        eng = JevEngine()
        d = eng.decide(snap)
        print(json.dumps(d.to_dict(), indent=2))
        print(f"latency: {eng.last_latency_ms:.0f} ms")
        if d.error:
            raise SystemExit(f"Jev call failed: {d.error}")


if __name__ == "__main__":
    main()
