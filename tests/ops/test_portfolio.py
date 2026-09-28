"""Портфель для риск-ядра (R30i.5) и запись фандинга в журнал (R29i)."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import (
    Balance,
    Fill,
    OrderIntent,
    Signal,
    StopSpec,
    StrategyManifest,
)
from lab.core.journal import Journal
from lab.core.registry import Registry
from lab.db.models import AllocationRow, TradeRow
from lab.executors.cex import FundingPayment
from lab.ops.funding import record_funding
from lab.ops.portfolio import LivePortfolio

T0 = datetime(2026, 9, 7, 12, tzinfo=UTC)


class FakeExec:
    def __init__(self, usd: str = "1000", *, fail: bool = False) -> None:
        self.usd, self.fail = Decimal(usd), fail

    def balance(self) -> list[Balance]:
        if self.fail:
            raise ConnectionError("площадка недоступна")
        return [Balance(asset="USDT", total=self.usd, free=self.usd, as_of=T0)]

    def positions(self):
        if self.fail:
            raise ConnectionError("площадка недоступна")
        return []


@pytest.fixture
def scope(session):
    @contextmanager
    def _scope():
        yield session

    return _scope


def test_bank_sums_venue_balances(scope):
    p = LivePortfolio(scope, executors={"bybit": FakeExec("1000"), "okx": FakeExec("500")})
    assert p.bank_usd() == Decimal(1500)
    assert p.venue_available("bybit") is True


def test_unavailable_venue_keeps_last_known_balance(scope):
    LivePortfolio(scope, executors={"bybit": FakeExec("1000")}).bank_usd()

    down = LivePortfolio(scope, executors={"bybit": FakeExec(fail=True)})
    known = down.venue_balance("bybit")
    assert known.usd == Decimal(1000) and known.stale is True
    assert down.bank_usd() == Decimal(1000)  # не ноль
    assert down.venue_available("bybit") is False
    assert down.branch("cex-spot").stale is True


def test_refresh_writes_allocations_table(scope, session):
    p = LivePortfolio(scope, executors={"bybit": FakeExec("1000")})
    rows = p.refresh(now=T0)
    assert rows

    saved = {r.branch: r for r in session.query(AllocationRow).all()}
    # ветки делят 80 %: ещё 20 % банка — ярус размещения, он не ветка (limits.yaml)
    assert sum(r.share for r in saved.values()) == Decimal(80)
    assert saved["cex-spot"].base_amount > 0
    assert saved["cex-spot"].updated_at is not None


def _open_trade(session) -> tuple[Journal, str]:
    strategy = Registry(session).add(
        StrategyManifest(
            slug="fund",
            branch="cex-perp",
            venue="bybit",
            source_kind="test",
            instruments=["BTC/USDT:USDT"],
            timeframe="1h",
            stop=StopSpec(daily_pct=Decimal(5)),
        )
    )
    journal = Journal(session)
    signal = Signal(
        strategy_id=strategy.id,
        decided_at=T0,
        instrument="BTC/USDT:USDT",
        side="buy",
        size=Decimal(1),
        price_ref=Decimal(100),
        inputs_hash="abc",
        ttl_s=600,
    )
    signal_id = journal.record_signal(signal).id
    journal.record_order(
        OrderIntent(
            strategy_id=strategy.id,
            venue="bybit",
            instrument="BTC/USDT:USDT",
            side="buy",
            qty=Decimal(1),
            order_type="market",
            mode="live",
            signal_id=signal_id,
            client_order_id="coid-1",
        ),
        order_id="o-1",
    )
    journal.record_fill(
        Fill(
            id="f-1",
            order_id="o-1",
            price=Decimal(100),
            qty=Decimal(1),
            fee=Decimal("0.1"),
            fee_asset="USDT",
            ts=T0,
        )
    )
    return journal, strategy.id


class FundingExec:
    def __init__(self, payments):
        self._payments = payments

    def funding_payments(self, since):
        return [p for p in self._payments if p.ts >= since]


def test_funding_payment_lands_in_open_trade(session):
    _open_trade(session)
    executor = FundingExec(
        [
            FundingPayment(
                id="fp-1",
                instrument="BTC/USDT:USDT",
                amount=Decimal("-0.5"),  # площадка списала — для нас это издержка
                ts=T0 + timedelta(hours=8),
            )
        ]
    )
    report = record_funding(session, executor=executor, venue="bybit", since=T0)

    assert report.applied == 1 and report.skipped == 0
    trade = session.query(TradeRow).one()
    assert trade.funding == Decimal("0.5")

    again = record_funding(session, executor=executor, venue="bybit", since=T0)
    assert again.applied == 0  # повтор не задваивает платёж
    assert session.query(TradeRow).one().funding == Decimal("0.5")


def test_funding_without_open_position_is_skipped(session):
    executor = FundingExec(
        [
            FundingPayment(
                id="fp-2", instrument="ETH/USDT:USDT", amount=Decimal("-1"), ts=T0
            )
        ]
    )
    report = record_funding(session, executor=executor, venue="bybit", since=T0)
    assert report.applied == 0 and report.skipped == 1
