"""Decision engine interface. Engines judge; they never size or trade."""
from __future__ import annotations

from typing import Protocol

from quant.schema import Decision
from quant.snapshot import Snapshot


class DecisionEngine(Protocol):
    name: str

    def decide(self, snap: Snapshot) -> Decision:
        """Must not raise. On any failure return Decision.neutral(...)."""
        ...
