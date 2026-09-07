"""ops.stop_watch: `Deny(strategy_stop_*)` на открывающем ордере → `ladder.breach()`."""

from decimal import Decimal

import pytest

from lab.contracts import OrderIntent
from lab.core.risk import Allow, Deny
from lab.ops.stop_watch import RiskCoreError, StopWatch


class FakeRisk:
    def __init__(self, verdict):
        self.verdict = verdict
        self.seen: list[OrderIntent] = []

    def check(self, intent):
        self.seen.append(intent)
        return self.verdict


class FakeLadder:
    def __init__(self):
        self.breached: list[tuple[str, str]] = []

    def breach(self, strategy_id, reason, snapshot=None):
        self.breached.append((strategy_id, reason))
        return f"transition:{strategy_id}"


def intent(**over) -> OrderIntent:
    base = dict(
        strategy_id="cex-spot-preset-x", venue="bybit", instrument="BTC/USDT", side="buy",
        qty=Decimal("0.01"), price=None, order_type="market", mode="paper",
        signal_id="s1", client_order_id="c1",
    )
    return OrderIntent(**{**base, **over})


def test_stop_deny_on_opening_order_breaches_strategy_once() -> None:
    risk = FakeRisk(Deny(reason="дневной стоп пробит", rule="strategy_stop_daily"))
    ladder = FakeLadder()
    watch = StopWatch(risk, ladder)
    v1 = watch.guard(intent())
    v2 = watch.guard(intent(client_order_id="c2"))
    assert isinstance(v1, Deny) and isinstance(v2, Deny)
    assert ladder.breached == [("cex-spot-preset-x", "дневной стоп пробит")]
    assert watch.breached() == ["cex-spot-preset-x"]


def test_other_denies_and_allows_do_not_breach() -> None:
    ladder = FakeLadder()
    StopWatch(FakeRisk(Deny(reason="нет цены", rule="no_price")), ladder).guard(intent())
    StopWatch(FakeRisk(Allow()), ladder).guard(intent())
    assert ladder.breached == []


def test_stop_deny_on_reduce_only_is_risk_core_error_not_refusal() -> None:
    """Известный дефект T03: стоп на закрывающем ордере — ошибка риск-ядра, не отказ."""
    risk = FakeRisk(Deny(reason="просадка", rule="strategy_stop_dd"))
    ladder = FakeLadder()
    with pytest.raises(RiskCoreError):
        StopWatch(risk, ladder).guard(intent(reduce_only=True, side="sell"))
    assert ladder.breached == []


def test_sweep_probes_active_strategies_and_reports_breaches() -> None:
    risk = FakeRisk(Deny(reason="просадка 12%", rule="strategy_stop_dd"))
    ladder = FakeLadder()
    watch = StopWatch(risk, ladder)
    result = watch.sweep([intent(strategy_id="a"), intent(strategy_id="b")])
    assert result == ["a", "b"]
    assert [i.strategy_id for i in risk.seen] == ["a", "b"]
    assert watch.sweep([intent(strategy_id="a")]) == []  # уже degraded — повторно не бьём
