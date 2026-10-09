import dataclasses
import json

import pytest

from quant.agent import Agent
from quant.backtest import build_agent, run_backtest
from quant.broker import CostModel, PaperBroker
from quant.config import Config, load_config
from quant.engines.jev import JevEngine, parse_response
from quant.engines.rules import RulesEngine
from quant.feed import synthetic
from quant.policy import MAX_KELLY_FRACTION, PolicyConfig, gate, kelly_fraction, needs_escalation, target_fraction
from quant.review import Scored, brier, run_review
from quant.risk import DAY_MS, RiskLimits, RiskManager
from quant.schema import Decision, Direction, Regime, RiskState
from quant.snapshot import MAX_SNAPSHOT_CHARS, Candle, OrderBook, StateEngine


def good(**kw) -> Decision:
    base = dict(
        regime=Regime.TRENDING,
        direction=Direction.LONG,
        toxic_flow=False,
        setup_quality=2,
        risk_state=RiskState.SAFE,
        direction_confidence=0.85,
        engine="test",
    )
    base.update(kw)
    return Decision(**base)


class Fixed:
    name = "fixed"

    def __init__(self, d):
        self.d = d

    def decide(self, snap):
        return self.d


# ---- schema -------------------------------------------------------------------


def test_schema_rejects_out_of_range():
    with pytest.raises(ValueError):
        good(setup_quality=4)
    with pytest.raises(ValueError):
        good(direction_confidence=1.2)


# ---- state engine ------------------------------------------------------------------


def test_snapshot_rejects_out_of_order_data():
    st = StateEngine("X")
    st.on_candle(Candle(2, 1, 1, 1, 1, 1))
    with pytest.raises(ValueError):
        st.on_candle(Candle(1, 1, 1, 1, 1, 1))


def test_snapshot_is_compact_and_causal():
    candles = synthetic(300)
    a, b = StateEngine("X"), StateEngine("X")
    for c in candles[:200]:
        a.on_candle(c)
    for c in candles:
        b.on_candle(c)
    snap = a.snapshot(candles[199].ts, 0, 1e4, 1e4)
    assert len(snap.to_prompt()) <= MAX_SNAPSHOT_CHARS
    # b has seen the future; it must refuse to produce a snapshot in the past
    with pytest.raises(ValueError):
        b.snapshot(candles[199].ts, 0, 1e4, 1e4)


def test_snapshot_ignores_book_from_the_future():
    st = StateEngine("X")
    st.on_candle(Candle(10, 100, 100, 100, 100, 1))
    st.on_book(OrderBook(20, [(99, 5)], [(101, 1)]))
    s = st.snapshot(20, 0, 1e4, 1e4)
    assert s.mid == 100 and s.imbalance == pytest.approx(4 / 6)


# ---- policy ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        dict(setup_quality=1),
        dict(direction_confidence=0.80),
        dict(risk_state=RiskState.NEAR_LIMIT),
        dict(toxic_flow=True),
        dict(regime=Regime.CRISIS),
        dict(direction=Direction.NEUTRAL),
        dict(error="boom"),
    ],
)
def test_gate_blocks(kw):
    assert not gate(good(**kw), PolicyConfig()).passed


def test_gate_passes_good_setup():
    assert gate(good(), PolicyConfig()).passed


def test_kelly_capped_at_quarter():
    with pytest.raises(ValueError):
        PolicyConfig(kelly_fraction=0.5)
    cfg = PolicyConfig()
    p, b = 0.85, cfg.take_profit_bps / cfg.stop_loss_bps
    assert target_fraction(good(), cfg) == pytest.approx(MAX_KELLY_FRACTION * kelly_fraction(p, b))
    assert target_fraction(good(direction=Direction.SHORT), cfg) < 0
    assert kelly_fraction(0.3, 1.0) == 0.0


def test_escalation_on_transition_only():
    cfg = PolicyConfig()
    crisis = good(regime=Regime.CRISIS)
    assert needs_escalation(crisis, good(), cfg)
    assert needs_escalation(crisis, crisis, cfg) is None
    low = good(direction_confidence=0.55)
    assert needs_escalation(low, good(), cfg)
    assert needs_escalation(low, low, cfg) is None


# ---- risk ---------------------------------------------------------------------


def test_risk_limits_cannot_be_loosened_past_spec():
    with pytest.raises(ValueError):
        RiskLimits(max_drawdown=0.2)
    with pytest.raises(ValueError):
        RiskLimits(max_position=2.0)


def test_drawdown_kill_latches(tmp_path):
    rm = RiskManager(RiskLimits(kill_switch_path=str(tmp_path / "KILL")), 100.0)
    assert rm.check(0, 100).allow_new_risk
    v = rm.check(1, 84.9)
    assert v.force_flatten and not v.allow_new_risk
    assert not rm.check(2 * DAY_MS, 100).allow_new_risk  # recovery does not unlatch
    rm.reset_kill()
    assert rm.check(2 * DAY_MS + 1, 100).allow_new_risk


def test_daily_loss_resets_next_day(tmp_path):
    rm = RiskManager(RiskLimits(kill_switch_path=str(tmp_path / "KILL")), 100.0)
    rm.check(0, 100)
    assert not rm.check(1, 95.9).allow_new_risk
    assert rm.check(DAY_MS, 95.9).allow_new_risk


def test_kill_switch_file(tmp_path):
    path = tmp_path / "KILL"
    rm = RiskManager(RiskLimits(kill_switch_path=str(path)), 100.0)
    assert rm.check(0, 100).allow_new_risk
    path.touch()
    assert not rm.check(1, 100).allow_new_risk


# ---- broker ---------------------------------------------------------------------


def test_round_trip_pays_costs():
    br = PaperBroker(10_000, CostModel(fee_bps=10, slippage_bps=5, default_half_spread_bps=1))
    br.market_order(0, 1.0, 1000, "in")
    br.flatten(1, 1000, "out")
    assert br.qty == 0
    assert br.equity(1000) < 10_000
    assert br.total_fees == pytest.approx(1000 * 1.0016 * 1e-3 + 1000 * 0.9984 * 1e-3)


# ---- agent ---------------------------------------------------------------------


def _agent(engine, tmp_path, **risk_kw):
    cfg = Config(risk=RiskLimits(kill_switch_path=str(tmp_path / "KILL"), **risk_kw))
    cfg.rules = dataclasses.replace(cfg.rules, warmup_bars=0)
    return build_agent(cfg, engine, None), cfg


def test_engine_error_never_trades(tmp_path):
    agent, _ = _agent(Fixed(Decision.neutral("x", error="timeout")), tmp_path)
    for c in synthetic(100):
        agent.on_bar(c)
    assert agent.broker.fills == []


def test_fills_happen_next_bar_at_open(tmp_path):
    agent, _ = _agent(Fixed(good()), tmp_path)
    cs = [Candle(i, 100 + i, 101 + i, 99 + i, 100 + i, 1) for i in range(1, 5)]
    agent.on_bar(cs[0])
    assert agent.broker.fills == []
    agent.on_bar(cs[1])
    assert agent.broker.fills[0].ts == cs[1].ts
    assert agent.broker.fills[0].price > cs[1].open  # paid spread + slippage


def test_position_never_exceeds_max(tmp_path):
    agent, cfg = _agent(Fixed(good(direction_confidence=0.99)), tmp_path, max_position=0.05)
    for c in synthetic(200):
        agent.on_bar(c)
        eq = agent.broker.equity(c.close)
        assert abs(agent.broker.qty * c.close) / eq <= 0.05 * 1.2  # allow intrabar drift


def test_kill_switch_flattens(tmp_path):
    agent, _ = _agent(Fixed(good()), tmp_path)
    cs = synthetic(50)
    for c in cs[:10]:
        agent.on_bar(c)
    assert agent.broker.qty != 0 or agent.broker.fills
    (tmp_path / "KILL").touch()
    for c in cs[10:13]:
        agent.on_bar(c)
    assert agent.broker.qty == 0
    for c in cs[13:]:
        agent.on_bar(c)
    assert agent.broker.qty == 0


def test_decisions_do_not_depend_on_future(tmp_path):
    cs = synthetic(600, seed=3)
    alt = cs[:400] + synthetic(200, seed=99, start=cs[399].close)
    alt = alt[:400] + [dataclasses.replace(c, ts=cs[400 + i].ts) for i, c in enumerate(alt[400:])]
    cfg = Config(risk=RiskLimits(kill_switch_path=str(tmp_path / "KILL")))
    a = build_agent(cfg, RulesEngine(cfg.rules), None)
    b = build_agent(cfg, RulesEngine(cfg.rules), None)
    da = [a.on_bar(c) for c in cs]
    db = [b.on_bar(c) for c in alt]
    assert alt[400:] != cs[400:]
    assert da[:400] == db[:400]
    assert [f for f in a.broker.fills if f.ts <= cs[399].ts] == [f for f in b.broker.fills if f.ts <= cs[399].ts]


# ---- jev adapter ------------------------------------------------------------------


SAMPLE = {
    "model": "jev-1.13.0",
    "answers": {
        "regime": {"type": "choice", "choice": "trending", "probabilities": {"trending": 0.7}, "confidence": 0.7},
        "direction": {
            "type": "choice",
            "choice": "long",
            "probabilities": {"long": 0.83, "short": 0.07, "neutral": 0.10},
            "confidence": 0.8,
        },
        "toxic_flow": {"type": "noul", "noul": 0.1},
        "setup_quality": {"type": "score", "score": 2.4, "probabilities": [0.0, 0.1, 0.4, 0.5], "confidence": 0.6},
        "risk_state": {"type": "choice", "choice": "safe", "probabilities": {"safe": 0.9}, "confidence": 0.9},
    },
    "usage": {"input_tokens": 300, "output_tokens": 0},
}


def test_parse_jev_response():
    d = parse_response(SAMPLE)
    assert d.direction == Direction.LONG
    assert d.direction_confidence == pytest.approx(0.83)
    assert d.setup_quality == 2 and not d.toxic_flow
    assert gate(d, PolicyConfig()).passed


def test_jev_bad_response_and_network_fail_closed(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        JevEngine()
    eng = JevEngine(api_key="k", base_url="http://127.0.0.1:9", timeout_s=0.2)
    st = StateEngine("X")
    st.on_candle(Candle(1, 1, 1, 1, 1, 1))
    d = eng.decide(st.snapshot(1, 0, 1, 1))
    assert d.error and d.direction == Direction.NEUTRAL
    bad = json.loads(json.dumps(SAMPLE))
    bad["answers"]["direction"]["choice"] = "moon"
    with pytest.raises(ValueError):
        parse_response(bad)


def test_jev_key_not_in_request_body():
    eng = JevEngine(api_key="secret-key")
    st = StateEngine("X")
    st.on_candle(Candle(1, 1, 1, 1, 1, 1))
    assert "secret-key" not in json.dumps(eng.build_request(st.snapshot(1, 0, 1, 1)))


# ---- review & end to end -------------------------------------------------------------


def test_brier():
    assert brier([Scored(0, 1.0, 1, True), Scored(0, 0.0, 0, True)]) == 0
    assert brier([Scored(0, 0.5, 1, True)]) == 0.25


def test_backtest_and_review_end_to_end(tmp_path):
    cfg = Config(risk=RiskLimits(kill_switch_path=str(tmp_path / "KILL")))
    res = run_backtest(synthetic(1500), cfg, RulesEngine(cfg.rules), str(tmp_path / "run"))
    assert res.max_drawdown < cfg.risk.max_drawdown + 0.03  # limit + one bar of gap risk
    rep = run_review(str(tmp_path / "run"))
    assert (tmp_path / "run" / "review.md").exists()
    assert all(p["status"] == "pending_approval" for p in rep["proposals"])


def test_config_roundtrip(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"policy": {"min_confidence": 0.85}, "risk": {"max_drawdown": 0.1}}))
    cfg = load_config(str(p))
    assert cfg.policy.min_confidence == 0.85 and cfg.risk.max_drawdown == 0.1
    p.write_text(json.dumps({"policy": {"nope": 1}}))
    with pytest.raises(TypeError):
        load_config(str(p))
