# 01 — Specification (Brainstorm phase output)

Status: **DRAFT, awaiting approval (Checkpoint 1 of 3)**
Date: 2026-10-15

This is an independent research system built from published quantitative methods.
It is **not** Jane Street's system. It contains no proprietary code or strategies from
Jane Street or anyone else, and it makes no claim to.

---

## 1. Capability verification (done before any design)

Each item was checked on 2026-10-15. Labels: **VERIFIED** = confirmed in this container
or from first-party docs; **VENDOR-CLAIM** = stated only by the vendor; **UNAVAILABLE**
= cannot be used in this session as things stand.

| Component | What it really is | Status | Consequence for the design |
|---|---|---|---|
| Repository | Empty (one README, one commit) | VERIFIED | Greenfield. No code to migrate. |
| Python / uv / Node | Python 3.13, uv 0.11, Node 22.22, npm 10.9 | VERIFIED | Python services managed by uv. Node meets REA's minimum (≥22.19). |
| **REA** | `rea-agents` npm package (current 6.5.0), MIT license. Setup: `npx rea-agents setup` (interactive, backs up config). Deep native analysis needs Ghidra, IDA, or Hopper; static JS and .NET need none. | Package VERIFIED; tools **not yet registered** | MCP servers load when a session starts, so REA tools appear only in the **next** session after setup. Version pinned exactly, because minor releases break things on purpose. |
| **QuantGPT** | Hosted MCP at `https://terminal.quantgpt.co/mcp` (returns 401 without auth). Tools named `query_warehouse`, `run_backtest`. **Paid: $49/mo** (200 backtests/day, 500 AI calls/day). OAuth sign-in. | Endpoint VERIFIED; schema and coverage are VENDOR-CLAIM; **not connected** | (a) **Daily bars only.** It cannot support market-making or intraday research. (b) Its backtests are **gross of costs**, with no stated fill or slippage model, so they can only serve as a coarse screen. The local backtester is the system of record. (c) Redistribution rights on the standard plan are not stated, so its data stays out of git. |
| **"AgenKit" Engineering Kit** | No product by that exact name exists. Closest match: **ClaudeKit "Engineer" kit** (`npm i -g claudekit-cli`, `ck init --kit engineer`), which downloads from a **private GitHub repo** and needs a token with `repo` scope (in practice, a purchased license). Its documented commands are `/ck:brainstorm`, `/ck:plan`, `/ck:cook`, `/ck:test`, `/ck:code-review`, `/ck:fix`. Its docs define **no six-phase workflow**. | **UNAVAILABLE** pending your confirmation and license | The session's GitHub token is scoped to `kuij-i/quant` only and must not be reused to pull a third-party private kit. Until you confirm, I run the six phases you named (brainstorm → architecture → plan → test-first build → review → ship) as **repo-native docs plus gates** (`docs/01..06`, a CI test gate, and a review checklist). These map one-to-one onto ClaudeKit commands if you install it later. |
| Free public market data | `data.binance.vision`: spot **1m klines** and **aggTrades** reachable (HTTP 200). Historical L2 order-book snapshots are **not** freely available there. | VERIFIED (klines, aggTrades) | Market-making research runs on **trades plus 1m bars only**. Spread, depth, and imbalance features are **marked unavailable** in historical mode and enabled only when a live L1/L2 WebSocket is recording. Inventory and fill models are flagged as approximations. |
| GitHub access to other OSS repos | Plain HTTPS to github.com/hummingbot returns 403 through the proxy | VERIFIED blocked | Authorized REA targets (for example Hummingbot, Apache-2.0) have to be attached with `add_repo` or fetched as published packages from PyPI or npm. |

## 2. Goals

- G1. A reproducible research loop: hypothesis → versioned code → backtest with realistic costs → walk-forward OOS → recorded verdict (accepted **or rejected**).
- G2. Strict layer boundaries: **models advise, tested deterministic code decides**.
- G3. A risk engine that no model or strategy can bypass. Every order path passes through it, enforced by architecture and by tests.
- G4. Paper trading only. Live trading is architecturally disabled until you approve it explicitly, in writing.
- G5. Full provenance: every observation, feature, forecast, decision, order, and fill carries its source, event time, and ingest time.

## 3. Non-goals (v1)

- Live trading with real money.
- Any claim of reproducing or recovering any firm's proprietary strategy.
- Inferring order-book liquidity from daily or minute candles.
- Letting an LLM write directly to production strategy code, risk config, or deployment.

## 4. Layers and boundaries

| Layer | Owner | May do | May NOT do |
|---|---|---|---|
| L1 Research brain | Opus (this agent), offline | Write hypotheses, propose variants as PRs, review reports | Touch risk config, place orders, merge its own PRs, edit the experiment registry history |
| L2 Software investigation | REA (MCP), offline | Analyze **authorized** OSS or permitted artifacts. Output goes to the evidence ledger with labels: observed / recovered / inferred / unverified | Touch any non-authorized target, access controls, or private systems |
| L3 Data | QuantGPT (daily, research), Binance public dumps (intraday, research), exchange WebSocket (paper, live data) | Supply observations with provenance | Serve as a source of decisions |
| L4 Deterministic core (Python) | Tested code | Features, signals, sizing, validation gates, **risk**, execution (paper), reconciliation | Import or call any LLM client. Enforced by an import-linter contract in CI. |

## 5. Functional requirements (each one gets tests plus acceptance criteria)

**Data (D)**
- D1. Every record carries `source`, `source_version`, `event_ts` (UTC, ns), `ingest_ts`, and `available_ts`. `available_ts` is the earliest time the value could have been known. For a fundamental, that is the filing date, not the period end.
- D2. A point-in-time query API, `as_of(t)`, never returns a row with `available_ts > t`. *Test: property-based check over randomized cutoffs.*
- D3. Gap, duplicate, and outlier detection, with a quarantine table. Missing values are never forward-filled silently.
- D4. Raw vendor data stays out of git. A data manifest (hashes, ranges, license note) is committed instead.

**State engine (S)**
- S1. `Snapshot(t)` is a versioned, typed numeric vector. Each feature has a written formula, a lookback, the minimum data granularity it needs, and an `available` flag.
- S2. A feature whose required granularity is missing reads `UNAVAILABLE`. It is never imputed. For example, `book_imbalance` from daily data reads `UNAVAILABLE`.
- S3. Snapshots are stored with forward outcomes at the declared horizons, joined only after the horizon has elapsed.
- S4. Determinism: the same inputs and version produce byte-identical snapshot hashes.

**Strategies (ST)**: implemented independently from the published literature
- ST1. Inventory-aware market making (Avellaneda & Stoikov, 2008; Guéant-Lehalle-Fernandez-Tapia extensions). Simulated on trade-through fills, with stated assumptions.
- ST2. Statistical mean reversion (OU / z-score of residual spreads).
- ST3. Cross-sectional momentum (Jegadeesh & Titman 12-1, with vol scaling).
- ST4. Relative value / pairs (Engle-Granger or Johansen, with re-estimation in rolling windows only).
- ST5. Volatility-targeted sizing overlay.
- Each strategy declares its inputs, output (target position or quotes), horizon, and invalidating regime.

**Decision engine (DE)**
- DE1. Every model forecast declares its target, horizon, and calibration method, and is logged before its outcome is known.
- DE2. Rule-based combiner, versioned in config, with hashes in every decision log.
- DE3. Hard rejects: stale data, missing required inputs, uncalibrated model (no current calibration certificate), expected edge ≤ estimated cost × safety factor, or any risk check failure. Every rejection reason is logged.

**Validation (V)**
- V1. Chronological splits: train / validation / **locked test**. The test period is unlocked once per candidate, and the unlock is logged.
- V2. Walk-forward with purging and embargo (López de Prado).
- V3. Costs: fees, spread, slippage model, funding (perps). Every report includes a cost sensitivity sweep at 0.5×, 1×, 2×, and 3×.
- V4. Metrics: net Sharpe, max DD, returns, turnover, tail (CVaR 95/99), t-stat, calibration (Brier, reliability).
- V5. Multiple testing: an **append-only** experiment registry counts every trial. The report shows the Deflated Sharpe Ratio (Bailey & López de Prado) and PBO via CSCV.
- V6. Sharpe > 1.5, DD < 15%, and t > 2.0 are **screening** thresholds only. Passing them means "eligible for paper trading", not "has edge".

**Risk (R)**: deterministic, pre-trade, in the order path, with no override API
- R1. Max position and max notional per instrument, gross and net exposure, leverage, and concentration.
- R2. Daily loss limit and drawdown limit. Breaching either trips the kill switch, which latches and needs a human reset.
- R3. Stale-data gate, price collar (bands relative to the reference price), order-rate limit, max order size.
- R4. Kill switch: file and signal triggered. It cancels all orders and blocks new ones.
- R5. Risk config is read-only at runtime. A change needs a new signed config version, and the service restarts.
- R6. Reconciliation of orders, fills, positions, and balances against the venue (paper venue for v1). A mismatch halts trading.
- R7. **Tests prove that no code path reaches `venue.submit()` without passing `RiskGate.check()`.**

**Calibration (C)**
- C1. A forecast-outcome log, Brier score, and reliability curves, broken down by target and regime, compared against a climatology baseline.
- C2. Recalibration (Platt or isotonic) fitted on past data only, on a rolling window.
- C3. Drift monitors (PSI on features, rolling Brier). When a threshold is breached, exposure scales down, then the model is disabled.

**Self-improvement (SI)**
- SI1. A scheduled research cycle produces a report and **proposals as PRs**. Proposals pass the same gates (tests, OOS, risk review) and need human approval to merge.
- SI2. Negative results are recorded in the registry with the same detail as positive ones.

**Ops / dashboard (O)**
- O1. Dashboard showing data provenance and freshness, signals, model probabilities and calibration, trades, costs, P&L, and risk status (including the kill switch).
- O2. Structured JSON logs, health endpoints, systemd or Docker restart policies, and alerts (fills, errors, data gaps, limit breaches, kill switch).
- O3. Secrets come only from environment variables or a secret store. A pre-commit and CI secret scan fails the build on any key pattern.
- O4. A daily research report: net P&L, drawdown, turnover, costs, calibration, rejected experiments, and proposed changes.

## 6. Proposed v1 scope (to keep it shippable)

1. **Phase A (offline research):** a local event-driven backtester, the data layer (Binance public 1m and aggTrades for BTC/ETH spot; QuantGPT daily equities if you connect it), the state engine, ST2, ST3, ST5, validation, and the registry.
2. **Phase B:** ST1 market making on aggTrades, with conservative fills (fills only on trade-through, with a queue-position haircut) and inventory risk.
3. **Phase C:** paper trading against a live public WebSocket (Binance or Coinbase public market data, no keys needed), the risk engine in the path, the dashboard, and reconciliation against the paper venue.
4. **REA track (parallel):** investigate an authorized OSS engine (proposed: **Hummingbot**, Apache-2.0, which ships A-S-style market-making strategies) and record what it does in `docs/rea-evidence-ledger.md`.

## 7. Decisions needed from you (blocking)

1. **Approve or modify this spec.**
2. **AgenKit:** do you mean ClaudeKit's Engineer kit, and do you hold a license? If yes, please install it yourself (`npm i -g claudekit-cli && ck init --kit engineer`, using **your own** GitHub token) or provide a token through the environment's secret settings. Otherwise I continue with the repo-native six-phase workflow.
3. **QuantGPT:** do you have a paid account? If yes, add it as a connector in claude.ai (OAuth, URL `https://terminal.quantgpt.co/mcp`). No key belongs in this repo.
4. **REA target:** confirm Hummingbot (or name another target you are authorized to analyze). I'll attach it read-only with `add_repo`.
5. **Venue for paper data:** Binance public, Coinbase public, or another? (Check that you are permitted to access the venue from your jurisdiction.)
