"""Config: one JSON file, one section per component. Unknown keys are errors."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from quant.agent import AgentConfig
from quant.broker import CostModel
from quant.engines.rules import RulesConfig
from quant.policy import PolicyConfig
from quant.risk import RiskLimits


@dataclass
class Config:
    symbol: str = "BTC/USDT"
    starting_equity: float = 10_000.0
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    risk: RiskLimits = field(default_factory=RiskLimits)
    rules: RulesConfig = field(default_factory=RulesConfig)
    costs: CostModel = field(default_factory=CostModel)
    agent: AgentConfig = field(default_factory=AgentConfig)

    def to_dict(self) -> dict:
        return asdict(self)


_SECTIONS = {"policy": PolicyConfig, "risk": RiskLimits, "rules": RulesConfig, "costs": CostModel, "agent": AgentConfig}


def load_config(path: str | None) -> Config:
    if not path:
        return Config()
    with open(path) as f:
        raw = json.load(f)
    kw = {}
    for k, v in raw.items():
        if k in _SECTIONS:
            kw[k] = _SECTIONS[k](**v)  # TypeError on unknown keys, by design
        elif k in ("symbol", "starting_equity"):
            kw[k] = v
        else:
            raise ValueError(f"unknown config key: {k}")
    return Config(**kw)
