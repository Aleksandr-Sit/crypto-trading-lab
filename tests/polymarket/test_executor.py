"""Исполнитель Polymarket: paper по живому стакану, режим экземпляра, доступность торговли.

Проверка доступности (История 84) даёт ветке `prediction` режим «только замер»: флаг пишется
в `system_flags` и попадает в `/status` бота через `lab.executors.access.status_lines`.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Executor, OrderIntent, OrderState
from lab.executors import registry
from lab.executors.access import branch_mode, branch_modes, status_lines
from lab.executors.polymarket import PolymarketError, PolymarketExecutor, check_trading_access
from lab.feeds.chains.fake import FakeHttpTransport
from lab.feeds.chains.transport import ChainError
from lab.feeds.polymarket import PolymarketFeed
from tests.polymarket.test_feed import BOOK, TOKEN


def _transport(*, offline: bool = False, geo_blocked: bool = False) -> FakeHttpTransport:
    transport = FakeHttpTransport(offline=offline)
    transport.route("GET", "clob.polymarket.com/book", BOOK)
    if geo_blocked:

        def blocked(url, params, json):
            raise ChainError(f"{url}: 403")

        transport.route("GET", "clob.polymarket.com/ok", blocked)
    else:
        transport.route("GET", "clob.polymarket.com/ok", "OK")
    return transport


def _intent(side: str = "buy", price: Decimal | None = Decimal("0.55")) -> OrderIntent:
    return OrderIntent(
        strategy_id="prediction-pm-copy-0xaaa",
        venue="polymarket",
        instrument=TOKEN,
        side=side,
        qty=Decimal("100"),
        price=price,
        order_type="limit" if price is not None else "market",
        mode="paper",
        signal_id="sig-pm-1",
        client_order_id="pm-1",
    )


def make_executor(mode="paper", **kw) -> PolymarketExecutor:
    transport = kw.pop("transport", None) or _transport()
    return PolymarketExecutor(PolymarketFeed(transport), mode=mode, **kw)


def test_registered_in_executors_registry():
    assert "polymarket" in registry.all()
    assert isinstance(registry.get("polymarket")(), Executor)


def test_paper_order_fills_by_book_and_costs_are_counted():
    ex = make_executor()
    order = ex.place(_intent(), mode="paper")
    assert order.state == OrderState.FILLED
    fills = ex.fills(since=datetime.now(UTC) - timedelta(minutes=1))
    # 100 контрактов уходят в ask 0.54 (там 800) — цена исполнения равна лучшему аску
    assert fills[0].price == Decimal("0.54")
    assert fills[0].qty == Decimal("100")
    positions = ex.positions()
    assert positions[0].instrument == TOKEN
    assert positions[0].qty == Decimal("100")


def test_paper_balance_drops_by_notional():
    ex = make_executor(paper_balance=Decimal("1000"))
    ex.place(_intent(), mode="paper")
    usdc = next(b for b in ex.balance() if b.asset == "USDC")
    assert usdc.total < Decimal("1000")


def test_mode_of_instance_separates_state():
    ex = make_executor(mode="live")
    with pytest.raises(PolymarketError):
        ex.place(_intent(), mode="paper")


def test_live_without_key_is_not_connected():
    ex = make_executor(mode="live")
    assert ex.rights().trade is False
    assert ex.rights().withdraw is False
    intent = _intent().model_copy(update={"mode": "live"})
    with pytest.raises(PolymarketError):
        ex.place(intent, mode="live")


def test_trading_access_unavailable_without_key():
    access = make_executor().trading_access()
    assert access.available is False
    assert "POLYMARKET_PRIVATE_KEY" in access.reason


def test_trading_access_reports_geo_block():
    ex = make_executor(transport=_transport(geo_blocked=True), private_key="0xdead")
    access = ex.trading_access()
    assert access.available is False
    assert "403" in access.reason or "недоступ" in access.reason


def test_read_only_branch_lands_in_status(session):
    ex = make_executor()
    mode = check_trading_access(ex, session=session)
    assert mode.branch == "prediction"
    assert mode.read_only is True
    assert branch_mode(session, "prediction").read_only is True
    assert any(m.branch == "prediction" for m in branch_modes(session))
    line = "\n".join(status_lines(session))
    assert "prediction" in line and "только замер" in line
