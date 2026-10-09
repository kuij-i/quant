# quant

A crypto trading agent that runs on **paper only**. It uses real public market data and simulates every fill. There is no live order path.

```
candles + order book ──► StateEngine ──► Snapshot (compact JSON, causal)
                                            │
                                            ▼
                               DecisionEngine (rules | Jev)   ← judges only
                                            │  Decision (typed)
                                            ▼
                     policy.gate ─► Kelly sizing (≤ quarter Kelly)
                                            │
                                            ▼
               RiskManager (kill switch, max DD, daily loss, max position)
                                            │   ← model can never override
                                            ▼
                                  PaperBroker (fees, spread, slippage)
                                            │
                                            ▼
                         Journal (JSONL) ──► nightly review (Brier, calibration)
                                                  └─► proposals.json (operator approves)
```

## Layout

| file | role |
|---|---|
| `quant/snapshot.py` | State engine. Computes mid, spread, imbalance, realized vol, trend, z-score, inventory and drawdown in code. Rejects out-of-order data. A snapshot never uses data newer than its timestamp. The snapshot is capped at ~400 tokens. |
| `quant/schema.py` | Typed `Decision`: regime, direction, toxic_flow, setup_quality 0–3, risk_state, direction_confidence. |
| `quant/engines/rules.py` | Deterministic baseline engine. Its confidence is a heuristic, **not calibrated** (see review). |
| `quant/engines/jev.py` | TypeSafe Jev adapter (`POST /v1/systemone`). All 5 questions are sent in one parallel call. **Fails closed**: any error, timeout or malformed answer → neutral → no trade. |
| `quant/policy.py` | Gate: quality ≥ 2, confidence > 0.80, risk_state safe, not toxic, not crisis. Sizing is fractional Kelly with a hard cap at 0.25. Escalation triggers when confidence drops below 0.60 or the regime flips to crisis. |
| `quant/risk.py` | Hard limits, checked before every order: max DD ≤ 15% (latches the kill switch), daily loss, max position ≤ 1× equity (no leverage), and a kill-switch file. |
| `quant/agent.py` | One loop for backtest and paper. It decides at bar close and fills at the next open. Stops and take-profits are checked intrabar; if both are touched, the stop is assumed first. |
| `quant/review.py` | Nightly review: Brier score vs. a base-rate baseline, a calibration table, and proposals. **Never applies anything.** |

## Use

```bash
pip install -e '.[dev,exchange]'
pytest

# plumbing check on synthetic data (says nothing about edge)
python -m quant backtest --synthetic 5000

# real history, public endpoints, no keys
python -m quant fetch --exchange binance --symbol BTC/USDT --timeframe 1h --days 365 --out data/btc_1h.csv
python -m quant backtest --csv data/btc_1h.csv --config config.example.json
python -m quant review runs/<run-dir>

# live public data, simulated fills
python -m quant paper --exchange binance --timeframe 1h
touch KILL   # halts + flattens at the next bar; to resume: rm KILL and restart
```

### Using Jev

```bash
export JEV_API_KEY=...            # from console.typesafe.ai; never commit it
python -m quant check-jev         # one call: prints the parsed decision + latency
python -m quant backtest --csv data/btc_1h.csv --engine jev
```

Optional env vars are `JEV_BASE_URL` (default `https://api.typesafe.ai`) and `JEV_MODEL` (default `jev-latest`). The request and response shape follows docs.typesafe.ai as of Oct 2026. The API is early access, so run `check-jev` first. A Jev backtest makes one paid API call per bar.

## Deliberate departures from the original prompt

- **No self-deploying nightly rewrite.** The review writes `proposals.json` with `status: pending_approval`. You edit the config. A loop that retunes itself on its own noise overfits.
- **Escalation is logged, not auto-acted on.** `escalations.jsonl` records the snapshot and decision for a deep review by you or Claude. Meanwhile the gate blocks new entries in crisis or at low confidence, and crisis flattens.
- **Kelly is applied to notional, not risk.** This is more conservative than textbook Kelly with a stop, by design.
- **No research or token picks.** That part of the prompt needs dated primary sources and is a separate task.

## What could I be wrong about?

- **Edge.** Nothing here shows positive expectancy. On synthetic data the rules engine is badly overconfident: it predicts ~0.89 and hits ~0.51 after costs. Treat every engine as edge-less until a review on real out-of-sample data beats the base-rate Brier score.
- **Costs.** 10 bps fees + 5 bps slippage + half-spread per side is a guess. Hourly signals with a 30 bps round-trip hurdle are hard to beat.
- **Jev calibration.** "Calibrated probability" is the vendor's claim. The review measures it; trust the measurement, not the claim.
- **Bar-level fills.** Real fills, partial fills, outages and gaps behave worse than this simulation.
- **Drawdown limit.** It's checked at bar close, so a gap can push realized drawdown past 15% by up to one bar's move.
