"""Deterministic state engine.

Turns market data + portfolio state into one compact numeric Snapshot.
The Snapshot is the only thing a decision engine ever sees.

Causality: every update carries a timestamp; the engine refuses data that is
older than what it has already seen, and a snapshot taken at time t is built
only from updates with ts <= t. Nothing here looks at future data.
"""
from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import asdict, dataclass

# Rough upper bound: ~4 chars per token. 400 tokens -> 1600 chars.
MAX_SNAPSHOT_CHARS = 1600


@dataclass(frozen=True)
class Candle:
    ts: int  # close time, ms since epoch (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OrderBook:
    ts: int
    bids: list[tuple[float, float]]  # (price, size), best first
    asks: list[tuple[float, float]]


@dataclass(frozen=True)
class Snapshot:
    ts: int
    symbol: str
    mid: float
    spread_bps: float | None  # None when no order book is available
    imbalance: float | None  # (bid_vol - ask_vol) / total over top levels, -1..1
    ret_1_bps: float  # last bar log return
    ret_n_bps: float  # log return over momentum window
    rv_bps: float  # realized vol: stdev of per-bar log returns
    trend_bps: float  # (ema_fast - ema_slow) / mid
    zscore: float  # ret_n / (rv * sqrt(n)), mean-reversion signal
    inventory: float  # signed position notional / equity
    drawdown: float  # from equity peak, 0..1
    bars: int  # number of bars seen (warm-up indicator)

    def to_prompt(self) -> str:
        """Compact JSON for the decision engine. Rounded to keep tokens low."""
        d = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}
        s = json.dumps(d, separators=(",", ":"))
        assert len(s) <= MAX_SNAPSHOT_CHARS, "snapshot exceeds token budget"
        return s


class StateEngine:
    def __init__(
        self,
        symbol: str,
        momentum_window: int = 20,
        vol_window: int = 50,
        ema_fast: int = 12,
        ema_slow: int = 48,
        book_levels: int = 10,
    ) -> None:
        self.symbol = symbol
        self.n = momentum_window
        self.book_levels = book_levels
        self._closes: deque[float] = deque(maxlen=max(momentum_window, vol_window) + 1)
        self._rets: deque[float] = deque(maxlen=vol_window)
        self._a_fast = 2 / (ema_fast + 1)
        self._a_slow = 2 / (ema_slow + 1)
        self._ema_fast: float | None = None
        self._ema_slow: float | None = None
        self._book: OrderBook | None = None
        self._last_ts: int | None = None
        self._bars = 0

    def _check_ts(self, ts: int) -> None:
        if self._last_ts is not None and ts < self._last_ts:
            raise ValueError(f"out-of-order data: {ts} < {self._last_ts}")
        self._last_ts = ts

    def on_candle(self, c: Candle) -> None:
        self._check_ts(c.ts)
        if self._closes:
            self._rets.append(math.log(c.close / self._closes[-1]))
        self._closes.append(c.close)
        if self._ema_fast is None:
            self._ema_fast = self._ema_slow = c.close
        else:
            self._ema_fast += self._a_fast * (c.close - self._ema_fast)
            self._ema_slow += self._a_slow * (c.close - self._ema_slow)
        self._bars += 1

    def on_book(self, book: OrderBook) -> None:
        self._check_ts(book.ts)
        self._book = book

    def snapshot(self, ts: int, position_qty: float, equity: float, peak_equity: float) -> Snapshot:
        if not self._closes:
            raise RuntimeError("no data yet")
        if self._last_ts is not None and ts < self._last_ts:
            raise ValueError("snapshot requested before latest data timestamp")

        last = self._closes[-1]
        spread_bps = imbalance = None
        mid = last
        book = self._book
        if book and book.bids and book.asks and book.ts <= ts:
            bb, ba = book.bids[0][0], book.asks[0][0]
            mid = (bb + ba) / 2
            spread_bps = (ba - bb) / mid * 1e4
            bv = sum(s for _, s in book.bids[: self.book_levels])
            av = sum(s for _, s in book.asks[: self.book_levels])
            imbalance = (bv - av) / (bv + av) if bv + av > 0 else 0.0

        rets = list(self._rets)
        ret_1 = rets[-1] if rets else 0.0
        closes = list(self._closes)
        k = min(self.n, len(closes) - 1)
        ret_n = math.log(closes[-1] / closes[-1 - k]) if k > 0 else 0.0
        rv = _stdev(rets) if len(rets) >= 2 else 0.0
        z = ret_n / (rv * math.sqrt(k)) if rv > 0 and k > 0 else 0.0
        trend = (self._ema_fast - self._ema_slow) / mid if self._ema_fast else 0.0

        return Snapshot(
            ts=ts,
            symbol=self.symbol,
            mid=mid,
            spread_bps=spread_bps,
            imbalance=imbalance,
            ret_1_bps=ret_1 * 1e4,
            ret_n_bps=ret_n * 1e4,
            rv_bps=rv * 1e4,
            trend_bps=trend * 1e4,
            zscore=z,
            inventory=position_qty * mid / equity if equity > 0 else 0.0,
            drawdown=max(0.0, 1 - equity / peak_equity) if peak_equity > 0 else 0.0,
            bars=self._bars,
        )


def _stdev(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
