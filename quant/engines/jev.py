"""TypeSafe Jev adapter.

Request/response shape follows https://docs.typesafe.ai/api.md
(POST {base}/v1/systemone, Bearer auth, typed `choice` / `score` / `noul`
questions evaluated in parallel against one `state`). Verify against the
live docs with one test call before relying on it -- the API is early access.

Key comes from the JEV_API_KEY environment variable. It is never logged.
Any error, timeout or malformed answer returns Decision.neutral() -> no trade.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from quant.schema import Decision, Direction, Regime, RiskState
from quant.snapshot import Snapshot

QUESTIONS = {
    "regime": {
        "type": "choice",
        "instructions": "Which market regime does this snapshot of a crypto market describe?",
        "criteria": {
            "trending": "Persistent directional move; fast EMA clearly separated from slow EMA.",
            "mean_reverting": "Range-bound; moves tend to revert; no persistent trend.",
            "high_vol": "Elevated realized volatility without a clear direction.",
            "crisis": "Extreme volatility, gaps or disorderly book; capital preservation first.",
        },
    },
    "direction": {
        "type": "choice",
        "instructions": "Over the next few bars, which position has positive expected value after costs?",
        "criteria": {
            "long": "Price is more likely to rise enough to cover fees and slippage.",
            "short": "Price is more likely to fall enough to cover fees and slippage.",
            "neutral": "No edge after costs, or the signals conflict.",
        },
    },
    "toxic_flow": {
        "type": "noul",
        "instructions": "Is the order flow toxic for a taker entering now (wide spread, one-sided book against the trade, informed flow)?",
    },
    "setup_quality": {
        "type": "score",
        "instructions": "How strong is the trade setup in this snapshot?",
        "criteria": [
            "No setup",
            "Weak: one signal, others neutral or conflicting",
            "Good: several independent signals agree",
            "Strong: trend, momentum and order book all agree with low toxicity",
        ],
    },
    "risk_state": {
        "type": "choice",
        "instructions": "Given drawdown, inventory and volatility, what should the risk posture be?",
        "criteria": {
            "safe": "Normal operation.",
            "near_limit": "Drawdown or inventory approaching limits; be selective.",
            "reduce": "Cut exposure.",
        },
    },
}


class JevEngine:
    name = "jev"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        self._key = api_key or os.environ.get("JEV_API_KEY")
        if not self._key:
            raise RuntimeError("JEV_API_KEY is not set")
        self.url = (base_url or os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai")).rstrip("/") + "/v1/systemone"
        self.model = model or os.environ.get("JEV_MODEL", "jev-latest")
        self.timeout_s = timeout_s
        self.last_latency_ms: float | None = None

    def build_request(self, s: Snapshot) -> dict:
        return {"model": self.model, "state": s.to_prompt(), "questions": QUESTIONS}

    def decide(self, s: Snapshot) -> Decision:
        body = json.dumps(self.build_request(s)).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return Decision.neutral(self.name, error=f"HTTP {e.code}")
        except Exception as e:
            return Decision.neutral(self.name, error=f"{type(e).__name__}")
        finally:
            self.last_latency_ms = (time.perf_counter() - t0) * 1000
        try:
            return parse_response(payload)
        except Exception as e:
            return Decision.neutral(self.name, error=f"bad response: {type(e).__name__}: {e}")


def parse_response(payload: dict) -> Decision:
    a = payload["answers"]
    direction = Direction(a["direction"]["choice"])
    probs = a["direction"].get("probabilities") or {}
    # The calibrated probability of the chosen option is what we size on.
    p = float(probs.get(direction.value, a["direction"]["confidence"]))
    quality = int(round(float(a["setup_quality"]["score"])))
    return Decision(
        regime=Regime(a["regime"]["choice"]),
        direction=direction,
        toxic_flow=float(a["toxic_flow"]["noul"]) >= 0.5,
        setup_quality=max(0, min(3, quality)),
        risk_state=RiskState(a["risk_state"]["choice"]),
        direction_confidence=max(0.0, min(1.0, p)),
        engine="jev",
        extra={
            "model": payload.get("model"),
            "direction_probabilities": probs,
            "toxic_noul": a["toxic_flow"]["noul"],
            "regime_confidence": a["regime"].get("confidence"),
        },
    )
