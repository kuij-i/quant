"""Market data sources: CSV replay, synthetic data, and public data via ccxt.

ccxt is optional (`pip install ccxt`) and only public endpoints are used --
no API keys, no order placement.
"""
from __future__ import annotations

import csv
import math
import random
import time
from collections.abc import Iterator

from quant.snapshot import Candle, OrderBook

FIELDS = ["ts", "open", "high", "low", "close", "volume"]


def load_csv(path: str) -> list[Candle]:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    candles = [Candle(int(r["ts"]), *(float(r[k]) for k in FIELDS[1:])) for r in rows]
    candles.sort(key=lambda c: c.ts)
    return candles


def save_csv(path: str, candles: list[Candle]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for c in candles:
            w.writerow([c.ts, c.open, c.high, c.low, c.close, c.volume])


def synthetic(n: int = 5000, seed: int = 7, start: float = 60_000.0, bar_ms: int = 3_600_000) -> list[Candle]:
    """Regime-switching random walk. FOR TESTING PLUMBING ONLY -- results on
    synthetic data say nothing about real-market edge."""
    rng = random.Random(seed)
    px, ts, out = start, 0, []
    drift, vol = 0.0, 0.006
    for _ in range(n):
        if rng.random() < 0.01:  # regime switch
            drift = rng.choice([-0.0015, 0.0, 0.0, 0.0015])
            vol = rng.choice([0.004, 0.006, 0.01, 0.02])
        o = px
        path = [o]
        for _ in range(4):
            path.append(path[-1] * math.exp(drift / 4 + vol / 2 * rng.gauss(0, 1)))
        px = path[-1]
        ts += bar_ms
        out.append(Candle(ts, o, max(path), min(path), px, rng.uniform(100, 1000)))
    return out


def _exchange(name: str):
    try:
        import ccxt  # type: ignore
    except ImportError as e:
        raise SystemExit("ccxt is required for exchange data: pip install ccxt") from e
    return getattr(ccxt, name)({"enableRateLimit": True})


def fetch_ohlcv(exchange: str, symbol: str, timeframe: str, since_ms: int, limit: int = 1000) -> list[Candle]:
    ex = _exchange(exchange)
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    out: list[Candle] = []
    since = since_ms
    now = ex.milliseconds()
    while since < now:
        rows = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        if not rows:
            break
        for t, o, h, l, c, v in rows:
            close_ts = t + tf_ms
            if close_ts <= now:  # only fully closed candles
                out.append(Candle(close_ts, o, h, l, c, v))
        since = rows[-1][0] + tf_ms
    dedup = {c.ts: c for c in out}
    return [dedup[k] for k in sorted(dedup)]


def live_bars(exchange: str, symbol: str, timeframe: str, warmup: int = 200) -> Iterator[tuple[Candle, OrderBook | None]]:
    """Yield closed candles (with a fresh order book) as they complete."""
    ex = _exchange(exchange)
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    last_ts = 0
    first = True
    while True:
        rows = ex.fetch_ohlcv(symbol, timeframe, limit=warmup if first else 3)
        now = ex.milliseconds()
        closed = [r for r in rows if r[0] + tf_ms <= now and r[0] + tf_ms > last_ts]
        for i, (t, o, h, l, c, v) in enumerate(closed):
            book = None
            if i == len(closed) - 1:
                ob = ex.fetch_order_book(symbol, limit=20)
                book = OrderBook(max(now, t + tf_ms), [tuple(x[:2]) for x in ob["bids"]], [tuple(x[:2]) for x in ob["asks"]])
            last_ts = t + tf_ms
            yield Candle(last_ts, o, h, l, c, v), book
        first = False
        next_close = (now // tf_ms + 1) * tf_ms
        time.sleep(max(1.0, (next_close - ex.milliseconds()) / 1000 + 2))
