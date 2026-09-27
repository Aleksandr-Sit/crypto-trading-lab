"""Шов executors.cex: paper/live одним классом поверх FakeTransport. Сети нет."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.contracts import OrderIntent, OrderState
from lab.executors.cex import BybitExecutor, client_order_id
from lab.feeds.cex import FakeTransport

T0 = datetime(2026, 1, 1, tzinfo=UTC)
PERP = "BTC/USDT:USDT"
SPOT = "BTC/USDT"


def _transport(**kw) -> FakeTransport:
    t = FakeTransport("bybit", **kw)
    t.seed_ohlcv(PERP, "1h", T0, 3, start_price=Decimal("50000"))
    t.seed_ohlcv(SPOT, "1h", T0, 3, start_price=Decimal("50000"))
    t.set_ticker(PERP, Decimal("50000"), spread=Decimal("2"))
    t.set_ticker(SPOT, Decimal("50000"), spread=Decimal("2"))
    return t


def _intent(
    mode="live",
    instrument=PERP,
    side="buy",
    price=None,
    leverage=1,
    signal_id="sig-1",
    reduce_only=False,
) -> OrderIntent:
    return OrderIntent(
        strategy_id="cex-perp-preset-t04",
        venue="bybit",
        instrument=instrument,
        side=side,
        qty=Decimal("0.01"),
        price=price,
        order_type="limit" if price else "market",
        leverage=Decimal(leverage),
        reduce_only=reduce_only,
        mode=mode,
        signal_id=signal_id,
        client_order_id=client_order_id(signal_id, "bybit"),
    )


def test_client_order_id_is_deterministic_and_venue_safe():
    a, b = client_order_id("sig-1", "bybit"), client_order_id("sig-1", "bybit")
    assert a == b and a != client_order_id("sig-2", "bybit")
    assert len(a) <= 32 and a.isalnum()  # OKX: ≤32 символов, только буквы и цифры
    hl = client_order_id("sig-1", "hyperliquid")
    assert hl.startswith("0x") and len(hl) == 34  # HL cloid — 128-битный hex


def test_resend_after_timeout_does_not_duplicate_order():
    t = _transport()
    ex = BybitExecutor(transport=t, mode="live")
    import ccxt

    t.timeout_after_accept = True  # площадка приняла ордер, ответ потерян
    t.fail_next = None
    first = ex.place(_intent(price=Decimal("40000")), mode="live")
    assert first.state == OrderState.OPEN, "ордер после таймаута найден на площадке по client id"
    assert len(t.orders) == 1
    order = ex.place(_intent(price=Decimal("40000")), mode="live")  # повтор после таймаута
    assert order.id == first.id and len(t.orders) == 1, "повторная отправка создала дубль"
    assert order.client_order_id == client_order_id("sig-1", "bybit")

    # таймаут и сразу обрыв: поиск не удался → ошибка наружу; после реконнекта повтор без дубля
    ex2 = BybitExecutor(transport=t, mode="live")
    t.timeout_after_accept = True
    t.fail_next = ccxt.NetworkError("bybit: connection lost")
    with pytest.raises(ccxt.NetworkError):
        ex2.place(_intent(price=Decimal("41000"), signal_id="sig-2"), mode="live")
    again = ex2.place(_intent(price=Decimal("41000"), signal_id="sig-2"), mode="live")
    assert again.state == OrderState.OPEN and len(t.orders) == 2


def test_fill_during_outage_is_recovered_without_duplicate():
    """История 55: ордер исполнен, пока связи не было; после реконнекта fills(since) восполняет."""
    import ccxt

    t = _transport()
    ex = BybitExecutor(transport=t, mode="live")
    order = ex.place(_intent(price=Decimal("40000")), mode="live")
    assert order.state == OrderState.OPEN

    t.offline = True
    t.fill_open_orders()  # площадка исполнила по 40000, пока мы были без связи
    with pytest.raises(ccxt.NetworkError):
        ex.fills(since=T0)

    t.offline = False
    result = ex.reconcile(since=T0)
    fills = [f for f in result.fills if f.order_id == order.id]
    assert len(fills) == 1
    assert fills[0].price == Decimal("40000") and fills[0].qty == Decimal("0.01")
    assert fills[0].fee == Decimal("0.4")  # 40000 * 0.01 * 10 bps — из ответа площадки
    assert next(o for o in result.orders if o.id == order.id).state == OrderState.FILLED
    assert [(p.instrument, p.side, p.qty) for p in result.positions] == [
        (PERP, "buy", Decimal("0.01"))
    ]
    # повтор того же сигнала после реконнекта — тот же ордер, дубля нет
    again = ex.place(_intent(price=Decimal("40000")), mode="live")
    assert again.id == order.id and len(t.orders) == 1
    # повторный fills(since) не выдаёт тот же филл дважды в одном ответе
    assert len([f for f in ex.fills(since=T0) if f.order_id == order.id]) == 1


def test_spot_market_and_limit_fee_from_venue_and_paper_from_cost_model():
    """История 50: спот — лимит/маркет, комиссия из ответа площадки; paper — из core.costs."""
    t = _transport()
    ex = BybitExecutor(transport=t, mode="live")
    market = ex.place(_intent(instrument=SPOT), mode="live")
    assert market.state == OrderState.FILLED
    fill = next(f for f in ex.fills(since=T0) if f.order_id == market.id)
    assert fill.price == Decimal("50001")  # ask при спреде 2
    assert fill.fee == Decimal("0.50001") and fill.fee_asset == "USDT"  # 50001 * 0.01 * 10 bps

    limit = ex.place(
        _intent(instrument=SPOT, side="sell", price=Decimal("60000"), signal_id="sig-3"),
        mode="live",
    )
    assert limit.state == OrderState.OPEN and limit.price == Decimal("60000")
    assert ex.cancel(limit.id).state == OrderState.CANCELLED

    pex = BybitExecutor(transport=t, mode="paper")  # тот же транспорт, отдельный экземпляр
    paper = pex.place(_intent(mode="paper", instrument=SPOT, signal_id="sig-4"), mode="paper")
    assert paper.state == OrderState.FILLED and paper.mode == "paper"
    pfill = next(f for f in pex.fills(since=T0) if f.order_id == paper.id)
    assert pfill.price == Decimal("50001")  # те же живые котировки площадки
    assert pfill.fee == Decimal("0.50001")  # costs.yaml: bybit taker 10 bps по VWAP стакана


def test_paper_perp_fee_uses_perp_tariff():
    """costs v2: бумажный перп Bybit платит тейкер перпа 5.5 б.п., а не спотовые 10."""
    pex = BybitExecutor(transport=_transport(), mode="paper")
    paper = pex.place(_intent(mode="paper", signal_id="sig-p"), mode="paper")  # PERP
    pfill = next(f for f in pex.fills(since=T0) if f.order_id == paper.id)
    assert pfill.price == Decimal("50001")
    assert pfill.fee == Decimal("0.2750055")  # 50001 * 0.01 * 5.5 bps


def test_reduce_only_flag_goes_to_venue_only_on_perp():
    """Спотового `reduceOnly` у бирж нет, а ccxt шлёт параметр как есть: на споте закрытие —
    обычная продажа, объём которой сверил `place_signal` по лотам стратегии."""
    t = _transport()
    ex = BybitExecutor(transport=t, mode="live")
    perp = ex.place(_intent(side="sell", reduce_only=True), mode="live")
    spot = ex.place(
        _intent(instrument=SPOT, side="sell", reduce_only=True, signal_id="sig-2"), mode="live"
    )
    assert t.orders[perp.id]["reduceOnly"] is True
    assert t.orders[spot.id]["reduceOnly"] is False
    assert spot.state == OrderState.FILLED
