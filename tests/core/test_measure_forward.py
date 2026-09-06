"""Форвард/микро-замер по готовым сделкам из журнала (История 19, R11, R12.2):
стратегии-объекта нет, ветка приходит явно или разбирается из id с дефисом."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Branch, Costs
from lab.core.measure import ClosedTrade, branch_of_strategy_id, run

T0 = datetime(2026, 5, 1, tzinfo=UTC)
WINDOW = (T0, T0 + timedelta(days=30))


def _trade(i: int, pnl: str) -> ClosedTrade:
    return ClosedTrade(
        instrument="BTC/USDT", side="long", qty=Decimal(1), entry_price=Decimal(100),
        exit_price=Decimal(100) + Decimal(pnl), opened_at=T0 + timedelta(hours=6 * i),
        closed_at=T0 + timedelta(hours=6 * i + 2), pnl_gross=Decimal(pnl),
        costs=Costs(fee=Decimal("0.1")),
    )


@pytest.mark.parametrize(
    "strategy_id, branch",
    [
        ("cex-spot-bybit-x", Branch.CEX_SPOT),
        ("cex-perp-okx-y", Branch.CEX_PERP),
        ("dex-perp-hl-z", Branch.DEX_PERP),
        ("copy-hl-0xabc", Branch.COPY),
        ("meme-sol-pumpfun-early-v1", Branch.MEME),
    ],
)
def test_branch_of_strategy_id_takes_longest_match(strategy_id, branch):
    assert branch_of_strategy_id(strategy_id) is branch


def test_forward_run_without_strategy_uses_branch_from_id():
    trades = [_trade(i, "5") for i in range(30)]
    m = run("cex-spot-bybit-x", "forward", WINDOW, trades=trades, benchmark=Decimal(0),
            capital=Decimal(1000))
    assert m.status == "ok"
    assert m.metrics.n_trades == 30
    assert m.threshold.status == "passed"
    # Лимит просадки взят по группе ветки cex, а не по «ветке» cex.
    assert {c.name: c.limit for c in m.threshold.criteria}["max_dd_pct"] == Decimal(5)


def test_forward_run_accepts_explicit_branch():
    trades = [_trade(i, "5") for i in range(30)]
    m = run("странный_id", "micro", WINDOW, trades=trades, branch="meme", benchmark=Decimal(0),
            capital=Decimal(1000))
    assert m.threshold.status == "passed"
    assert {c.name: c.limit for c in m.threshold.criteria}["max_dd_pct"] == Decimal(20)


def test_forward_run_without_branch_and_unknown_id_is_explicit_error():
    with pytest.raises(ValueError, match="ветк"):
        run("странный_id", "forward", WINDOW, trades=[_trade(0, "1")])
