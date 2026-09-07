"""Базовый класс стратегии (Решения §9–§10): манифест, id `<ветка>-<источник>-<слаг>`,
сигнал с `decided_at` на закрытии бара и детерминированным `inputs_hash`;
стратегии не имеют доступа к лестнице и риск-ядру (проверка импортов)."""

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from lab.contracts import Branch, Candle, StopSpec, StrategyManifest
from lab.strategies import registry
from lab.strategies.base import Strategy

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "src" / "lab" / "strategies"
FORBIDDEN = ("lab.core.ladder", "lab.core.risk", "lab.core.registry", "lab.executors")


def _manifest() -> StrategyManifest:
    return StrategyManifest(
        slug="echo-v1",
        branch=Branch.CEX_SPOT,
        venue="bybit",
        source_kind="test",
        instruments=["SYN/USD"],
        timeframe="1h",
        params={"ttl_s": 900, "k": 2},
        stop=StopSpec(daily_pct=Decimal(5)),
    )


class Echo(Strategy):
    """Покупает на каждом баре — чтобы проверить оформление сигнала."""

    def on_bar(self, bar: Candle):
        return [self.signal(bar, "buy", Decimal(1), inputs={"close": bar.close})]


def _bar(ts: datetime, close: str = "100") -> Candle:
    c = Decimal(close)
    return Candle(
        instrument="SYN/USD", tf="1h", ts=ts, open=c, high=c, low=c, close=c, volume=Decimal(100)
    )


def test_strategy_id_follows_branch_source_slug():
    assert Echo(_manifest()).strategy_id == "cex-spot-test-echo-v1"


def test_signal_decided_at_bar_close_with_ttl_and_deterministic_hash():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    a, b = Echo(_manifest()), Echo(_manifest())
    sa = a.on_bar(_bar(t0))[0]
    sb = b.on_bar(_bar(t0))[0]
    assert sa.decided_at == t0 + timedelta(hours=1)  # решение — на закрытии бара, не раньше
    assert sa.ttl_s == 900
    assert sa.strategy_id == "cex-spot-test-echo-v1"
    assert len(sa.inputs_hash) == 64 and sa.inputs_hash == sb.inputs_hash
    assert a.on_bar(_bar(t0, "101"))[0].inputs_hash != sa.inputs_hash  # другие входы — другой хеш


def test_registry_builds_by_id_and_rejects_unknown():
    registry.register(Echo, _manifest(), replace=True)
    built = registry.build("cex-spot-test-echo-v1")
    assert isinstance(built, Echo)
    assert built.manifest.params["k"] == 2
    tuned = registry.build("cex-spot-test-echo-v1", params={"k": 5})
    assert tuned.manifest.params["k"] == 5 and tuned.manifest.params["ttl_s"] == 900
    assert "cex-spot-test-echo-v1" in registry.ids()
    try:
        registry.build("cex-spot-test-nope")
    except registry.UnknownStrategy:
        pass
    else:
        raise AssertionError("неизвестный id должен давать UnknownStrategy")


def test_strategies_never_import_ladder_risk_or_executors():
    offenders = []
    for path in STRATEGIES_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in FORBIDDEN):
                    offenders.append(f"{path.relative_to(STRATEGIES_DIR)}: {name}")
    assert offenders == []
