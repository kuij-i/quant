"""Paper broker: simulated market orders with fees, half-spread and slippage.

There is deliberately no live broker in this package.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    fee_bps: float = 10.0  # taker fee per side
    slippage_bps: float = 5.0
    default_half_spread_bps: float = 1.0  # used when no order book


@dataclass(frozen=True)
class Fill:
    ts: int
    qty: float  # signed
    price: float
    fee: float
    reason: str


class PaperBroker:
    def __init__(self, starting_cash: float, costs: CostModel | None = None) -> None:
        self.cash = starting_cash
        self.qty = 0.0
        self.costs = costs or CostModel()
        self.fills: list[Fill] = []
        self.total_fees = 0.0
        self.entry_price: float | None = None

    def equity(self, mark: float) -> float:
        return self.cash + self.qty * mark

    def market_order(self, ts: int, qty: float, ref_price: float, reason: str, half_spread_bps: float | None = None) -> Fill | None:
        if qty == 0:
            return None
        hs = self.costs.default_half_spread_bps if half_spread_bps is None else half_spread_bps
        adverse = (hs + self.costs.slippage_bps) / 1e4
        price = ref_price * (1 + adverse) if qty > 0 else ref_price * (1 - adverse)
        fee = abs(qty) * price * self.costs.fee_bps / 1e4
        new_qty = self.qty + qty
        if self.qty == 0 or (self.qty > 0) != (new_qty > 0) or new_qty == 0:
            self.entry_price = price if new_qty != 0 else None
        elif abs(new_qty) > abs(self.qty):  # adding: weighted average entry
            self.entry_price = (self.entry_price * self.qty + price * qty) / new_qty
        self.cash -= qty * price + fee
        self.qty = new_qty
        self.total_fees += fee
        f = Fill(ts, qty, price, fee, reason)
        self.fills.append(f)
        return f

    def flatten(self, ts: int, ref_price: float, reason: str, half_spread_bps: float | None = None) -> Fill | None:
        return self.market_order(ts, -self.qty, ref_price, reason, half_spread_bps)
