"""Nightly review: calibration (Brier score), hit rates, costs, proposals.

This never changes the running system. It writes `review.md` and
`proposals.json` (status "pending_approval"). An operator decides whether to
apply a proposal to the config -- the system does not redeploy itself.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from quant.journal import read_jsonl

BUCKETS = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]


@dataclass
class Scored:
    ts: int
    p: float
    outcome: int
    traded: bool


def score_decisions(decisions: list[dict], horizon: int, cost_bps: float) -> list[Scored]:
    """Outcome = 1 if price moved in the predicted direction by more than
    round-trip costs within `horizon` bars. Uses future closes, which is fine
    here: this is after-the-fact evaluation, never fed back into a decision."""
    closes = [d["close"] for d in decisions]
    out = []
    for i, rec in enumerate(decisions):
        d = rec["decision"]
        if d.get("error") or d["direction"] == "neutral" or i + horizon >= len(decisions):
            continue
        sign = 1 if d["direction"] == "long" else -1
        move_bps = sign * (closes[i + horizon] / closes[i] - 1) * 1e4
        out.append(Scored(rec["ts"], float(d["direction_confidence"]), int(move_bps > cost_bps), rec.get("gate") == "pass"))
    return out


def brier(xs: list[Scored]) -> float | None:
    return sum((x.p - x.outcome) ** 2 for x in xs) / len(xs) if xs else None


def calibration(xs: list[Scored]) -> list[dict]:
    rows = []
    for lo, hi in zip(BUCKETS, BUCKETS[1:]):
        b = [x for x in xs if lo <= x.p < hi]
        if b:
            rows.append(
                {
                    "bucket": f"{lo:.1f}-{min(hi, 1.0):.1f}",
                    "n": len(b),
                    "mean_p": sum(x.p for x in b) / len(b),
                    "hit_rate": sum(x.outcome for x in b) / len(b),
                }
            )
    return rows


def propose(xs: list[Scored], cfg: dict, min_n: int = 30) -> list[dict]:
    props = []
    thr = cfg["policy"]["min_confidence"]
    above = [x for x in xs if x.p > thr]
    if len(above) >= min_n:
        mean_p = sum(x.p for x in above) / len(above)
        hit = sum(x.outcome for x in above) / len(above)
        if hit < mean_p - 0.10:
            props.append(
                {
                    "change": "policy.kelly_fraction",
                    "from": cfg["policy"]["kelly_fraction"],
                    "to": round(cfg["policy"]["kelly_fraction"] / 2, 4),
                    "why": f"over-confident above gate: mean p {mean_p:.2f} vs hit rate {hit:.2f} (n={len(above)})",
                }
            )
        if hit < 0.5:
            props.append(
                {
                    "change": "halt_trading",
                    "why": f"gated trades lose more often than win after costs (hit {hit:.2f}, n={len(above)}); edge not demonstrated",
                }
            )
    elif xs:
        props.append({"change": "none", "why": f"only {len(above)} decisions above the gate; need >= {min_n} to judge calibration"})
    return props


def run_review(run_dir: str, horizon: int = 4) -> dict:
    decisions = read_jsonl(os.path.join(run_dir, "decisions.jsonl"))
    fills = read_jsonl(os.path.join(run_dir, "fills.jsonl"))
    escalations = read_jsonl(os.path.join(run_dir, "escalations.jsonl"))
    with open(os.path.join(run_dir, "config.json")) as f:
        cfg = json.load(f)
    cost_bps = 2 * (cfg["costs"]["fee_bps"] + cfg["costs"]["slippage_bps"])

    xs = score_decisions(decisions, horizon, cost_bps)
    traded = [x for x in xs if x.traded]
    base = sum(x.outcome for x in xs) / len(xs) if xs else None
    report = {
        "decisions": len(decisions),
        "directional_scored": len(xs),
        "horizon_bars": horizon,
        "cost_hurdle_bps": cost_bps,
        "brier_all": brier(xs),
        "brier_gated": brier(traded),
        "brier_baseline_base_rate": (base * (1 - base)) if base is not None else None,
        "calibration": calibration(xs),
        "fills": len(fills),
        "fees": sum(f["fee"] for f in fills),
        "engine_errors": sum(1 for d in decisions if d["decision"].get("error")),
        "escalations": len(escalations),
        "proposals": [dict(p, status="pending_approval") for p in propose(xs, cfg)],
    }
    with open(os.path.join(run_dir, "proposals.json"), "w") as f:
        json.dump(report["proposals"], f, indent=2)
    with open(os.path.join(run_dir, "review.md"), "w") as f:
        f.write(render(report))
    return report


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.4f}"


def render(r: dict) -> str:
    lines = [
        "# Nightly review",
        "",
        f"- decisions: {r['decisions']} (directional & scorable: {r['directional_scored']})",
        f"- outcome: move > {r['cost_hurdle_bps']:.0f} bps in predicted direction within {r['horizon_bars']} bars",
        f"- Brier (all directional): {_fmt(r['brier_all'])}",
        f"- Brier (gated / traded): {_fmt(r['brier_gated'])}",
        f"- Brier of always predicting the base rate: {_fmt(r['brier_baseline_base_rate'])} (lower is better; beat this or there is no skill)",
        f"- fills: {r['fills']}, fees: {r['fees']:.2f}, engine errors: {r['engine_errors']}, escalations: {r['escalations']}",
        "",
        "## Calibration",
        "",
        "| bucket | n | mean p | hit rate |",
        "|---|---|---|---|",
    ]
    lines += [f"| {c['bucket']} | {c['n']} | {c['mean_p']:.3f} | {c['hit_rate']:.3f} |" for c in r["calibration"]]
    lines += ["", "## Proposals (pending operator approval; nothing was applied)", ""]
    lines += [f"- **{p['change']}**: {p['why']}" for p in r["proposals"]] or ["- none"]
    return "\n".join(lines) + "\n"
