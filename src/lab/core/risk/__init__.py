"""core.risk — лимиты веток, стопы, раскладка, потолок реального капитала.

Выставляет: `RiskEngine.check(OrderIntent) -> Allow | Deny(reason, rule)`, `allocation(branch)`,
`reload(by)`. Прячет арифметику размера и чтение конфига.
"""

from lab.core.risk.engine import RiskEngine, StrategyLookup, real_capital_cap, registry_lookup
from lab.core.risk.state import (
    DbConfigLog,
    DbHaltSwitch,
    MemoryConfigLog,
    MemoryHaltSwitch,
    config_diff,
)
from lab.core.risk.types import (
    Allocation,
    Allow,
    BranchState,
    ConfigChange,
    ConfigChangeSink,
    Deny,
    HaltSwitch,
    Portfolio,
    ReloadResult,
    StrategyInfo,
    StrategyStats,
    Verdict,
)

__all__ = [
    "Allocation",
    "Allow",
    "BranchState",
    "ConfigChange",
    "ConfigChangeSink",
    "DbConfigLog",
    "DbHaltSwitch",
    "Deny",
    "HaltSwitch",
    "MemoryConfigLog",
    "MemoryHaltSwitch",
    "Portfolio",
    "ReloadResult",
    "RiskEngine",
    "StrategyInfo",
    "StrategyLookup",
    "StrategyStats",
    "Verdict",
    "config_diff",
    "real_capital_cap",
    "registry_lookup",
]
