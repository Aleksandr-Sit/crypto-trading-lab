"""Стратегия `pm-copy-*` и метрики ветки `prediction` (Истории 82, 83).

Ожидаемые значения посчитаны вручную:
Brier = среднее (p − исход)² по трём ставкам (0.8→да, 0.3→нет, 0.6→нет) =
(0.04 + 0.09 + 0.36) / 3 = 0.16333…; доходность к резолюции = (100 − 170) / 170 = −41.176…%.
"""

from datetime import UTC, datetime
from decimal import Decimal

from lab.contracts import Event
from lab.core.measure import metrics
from lab.core.measure.types import NotApplicable
from lab.feeds.polymarket import POSITION_EVENT
from lab.strategies.prediction import (
    ResolvedBet,
    brier_score,
    make_pm_copy_strategy,
    measure_extra,
    resolution_return,
    resolution_trades,
)

WALLET = "0xAAA"
TOKEN = "713210456792522125946263855327069127503327285719425322896313793124555839925"
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


def position_event(size: str, *, price: str = "0.53", avg: str = "0.42") -> Event:
    return Event(
        kind=POSITION_EVENT,
        ts=NOW,
        payload={
            "wallet": WALLET,
            "condition_id": "0xcond1",
            "token_id": TOKEN,
            "outcome": "Yes",
            "size": size,
            "avg_price": avg,
            "price": price,
            "value_usd": str(Decimal(size) * Decimal(price)),
            "title": "Will BTC close above $100k in 2026?",
            "slug": "btc-100k-2026",
            "redeemable": False,
            "ts": NOW.isoformat(),
            "venue": "polymarket",
        },
    )


def make_strategy(**kw):
    params = {"capital_usd": "1000", "leader_capital_usd": "100000", "max_trade_usd": "500"}
    params.update(kw)
    return make_pm_copy_strategy(WALLET, **params)


def test_strategy_id_and_manifest():
    s = make_strategy()
    assert s.strategy_id.startswith("prediction-pm-copy-")
    assert s.manifest.branch == "prediction"
    assert s.manifest.venue == "polymarket"
    assert s.manifest.can_backtest is False


def test_leader_position_is_copied_by_scale():
    s = make_strategy()
    (signal,) = s.on_event(position_event("1000"))
    # масштаб = 1000 / 100000 = 1%, значит 10 контрактов лидера из 1000
    assert signal.side == "buy"
    assert signal.size == Decimal("10")
    assert signal.price_ref == Decimal("0.53")
    assert signal.meta["reason"] == "leader_entry"
    assert signal.meta["leader"] == WALLET
    assert signal.meta["token_id"] == TOKEN
    assert signal.meta["outcome"] == "Yes"


def test_same_position_twice_gives_no_second_signal():
    s = make_strategy()
    s.on_event(position_event("1000"))
    assert s.on_event(position_event("1000")) == []


def test_leader_increase_copies_only_the_difference():
    # доливка мелкая (5 контрактов по 0.53 ≈ 2.65 USD), поэтому порог сделки опущен до 1 USD
    s = make_strategy(min_trade_usd="1")
    s.on_event(position_event("1000"))
    (signal,) = s.on_event(position_event("1500"))
    assert signal.side == "buy"
    assert signal.size == Decimal("5")
    assert signal.meta["reason"] == "leader_increase"


def test_leader_exit_closes_our_position():
    s = make_strategy()
    s.on_event(position_event("1000"))
    (signal,) = s.on_event(position_event("0"))
    assert signal.side == "sell"
    assert signal.size == Decimal("10")
    assert signal.meta["reason"] == "leader_exit"


def test_max_trade_cap_limits_size():
    s = make_strategy(capital_usd="100000", leader_capital_usd="100000", max_trade_usd="53")
    (signal,) = s.on_event(position_event("1000"))
    # потолок 53 USD при цене 0.53 — это ровно 100 контрактов вместо 1000
    assert signal.size == Decimal("100")


BETS = [
    ResolvedBet(
        strategy_id="prediction-pm-copy-0xaaa",
        market="0xcond1",
        token_id="t1",
        outcome="Yes",
        probability=Decimal("0.8"),
        qty=Decimal("100"),
        resolved_yes=True,
        opened_at=NOW,
        resolved_at=NOW,
    ),
    ResolvedBet(
        strategy_id="prediction-pm-copy-0xaaa",
        market="0xcond2",
        token_id="t2",
        outcome="Yes",
        probability=Decimal("0.3"),
        qty=Decimal("100"),
        resolved_yes=False,
        opened_at=NOW,
        resolved_at=NOW,
    ),
    ResolvedBet(
        strategy_id="prediction-pm-copy-0xaaa",
        market="0xcond3",
        token_id="t3",
        outcome="Yes",
        probability=Decimal("0.6"),
        qty=Decimal("100"),
        resolved_yes=False,
        opened_at=NOW,
        resolved_at=NOW,
    ),
]


def test_brier_and_resolution_return_have_known_values():
    assert round(brier_score(BETS), 6) == Decimal("0.163333")
    assert round(resolution_return(BETS), 3) == Decimal("-41.176")


def test_branch_metrics_reach_measure_and_pnl_is_common():
    trades = resolution_trades(BETS)
    m = metrics(
        trades,
        capital=Decimal("1000"),
        window=(NOW, NOW),
        extra=measure_extra(BETS),
    )
    assert not isinstance(m.brier, NotApplicable)
    assert round(m.brier, 5) == Decimal("0.16333")
    assert round(m.resolution_return, 3) == Decimal("-41.176")
    # общий P&L считается по тем же ставкам: 20 − 30 − 60 = −70
    assert m.net_pnl == Decimal("-70")
    assert m.n_trades == 3
    assert m.max_dd_pct > 0
