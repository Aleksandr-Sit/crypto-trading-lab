"""Контрактный набор Executor в режиме paper. Один набор на все исполнители.

Параметризован по `executors.registry.all()`: новый исполнитель, зарегистрировавшись,
попадает сюда без правки теста.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Executor, KeyRights, OrderIntent, OrderState
from lab.executors import registry


def _intent(side: str = "buy", price: Decimal | None = Decimal("100")) -> OrderIntent:
    return OrderIntent(
        strategy_id="cex-spot-preset-contract-v1",
        venue="fake",
        instrument="BTC/USDT",
        side=side,
        qty=Decimal("0.5"),
        price=price,
        order_type="limit" if price is not None else "market",
        mode="paper",
        signal_id="sig-contract",
        client_order_id=f"lab-{side}-{price}",
    )


@pytest.fixture(params=sorted(registry.all()), ids=lambda name: name)
def executor(request) -> Executor:
    ex = registry.all()[request.param]()
    assert isinstance(ex, Executor), f"{request.param} не реализует протокол Executor"
    return ex


def test_registry_is_not_empty():
    assert registry.all(), "в реестре нет ни одного исполнителя"


def test_rights_are_reported(executor: Executor):
    rights = executor.rights()
    assert isinstance(rights, KeyRights)
    assert rights.withdraw is False, "исполнитель с правом вывода не должен попадать в реестр"


def test_paper_place_fills_and_reports_position(executor: Executor):
    order = executor.place(_intent(), mode="paper")
    assert order.mode == "paper"
    assert order.client_order_id == "lab-buy-100"
    assert order.state in (OrderState.FILLED, OrderState.OPEN, OrderState.PARTIAL)

    fills = executor.fills(since=datetime.now(UTC) - timedelta(minutes=1))
    assert any(f.order_id == order.id for f in fills)
    assert all(f.qty > 0 and f.price > 0 for f in fills)

    positions = executor.positions()
    assert any(p.instrument == "BTC/USDT" and p.qty == Decimal("0.5") for p in positions)


def test_cancel_open_order_returns_cancelled_state(executor: Executor):
    # лимитка далеко от рынка — должна остаться открытой, затем отменяется
    order = executor.place(_intent(price=Decimal("1")), mode="paper")
    if order.state == OrderState.FILLED:
        pytest.skip("исполнитель заполняет любую лимитку сразу — отмена неприменима")
    cancelled = executor.cancel(order.id)
    assert cancelled.id == order.id
    assert cancelled.state == OrderState.CANCELLED


def test_balance_is_decimal_and_timestamped(executor: Executor):
    balances = executor.balance()
    assert balances, "баланс пуст"
    for b in balances:
        assert isinstance(b.total, Decimal)
        assert b.as_of.tzinfo is not None


def test_health_has_status(executor: Executor):
    assert executor.health().status in ("ok", "degraded", "down")
