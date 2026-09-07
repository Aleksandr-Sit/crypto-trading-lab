"""Швы R16.2 (рестарт: ордера из базы сверяются с площадкой)
и R20 (перпы: плечо, фандинг, ликвидация)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import OrderIntent, OrderState, Signal, StopSpec, StrategyManifest
from lab.core.journal import Journal
from lab.core.registry import Registry
from lab.db.models import OrderRow
from lab.executors.cex import BybitExecutor, KeyRejected, NotConnected, client_order_id
from lab.feeds.cex import FakeTransport

T0 = datetime(2026, 1, 1, tzinfo=UTC)
PERP = "BTC/USDT:USDT"


def _transport(**kw) -> FakeTransport:
    t = FakeTransport("bybit", **kw)
    t.set_ticker(PERP, Decimal("50000"), spread=Decimal("2"))
    t.set_funding(PERP, Decimal("0.0001"), next_at=T0 + timedelta(hours=8), mark=Decimal("50010"))
    return t


def _intent(signal_id: str, *, price=None, leverage=1, mode="live") -> OrderIntent:
    return OrderIntent(
        strategy_id="cex-perp-test-restart",
        venue="bybit",
        instrument=PERP,
        side="buy",
        qty=Decimal("0.01"),
        price=price,
        order_type="limit" if price else "market",
        leverage=Decimal(leverage),
        mode=mode,
        signal_id=signal_id,
        client_order_id=client_order_id(signal_id, "bybit"),
    )


@pytest.fixture
def journal(session):
    strategy = Registry(session).add(
        StrategyManifest(
            slug="restart",
            branch="cex-perp",
            venue="bybit",
            source_kind="test",
            instruments=[PERP],
            timeframe="1h",
            stop=StopSpec(daily_pct=Decimal(5)),
        )
    )
    return Journal(session), strategy.id


def test_restart_restores_active_orders_from_db_and_reconciles_with_venue(session, journal):
    jr, sid = journal
    t = _transport()
    before = BybitExecutor(transport=t, mode="live")
    for i, price in enumerate(("40000", "41000", "42000")):
        signal_id = f"sig-r{i}"
        jr.record_signal(
            Signal(
                strategy_id=sid,
                decided_at=T0,
                instrument=PERP,
                side="buy",
                size=Decimal("0.01"),
                inputs_hash="h",
                ttl_s=60,
            ),
            signal_id=signal_id,
        )
        intent = _intent(signal_id, price=Decimal(price))
        order = before.place(intent, mode="live")
        jr.record_order(intent, order_id=order.id, state=order.state)
    session.flush()
    ids = sorted(t.orders)
    # пока процесс лежал: первый исполнен, второй отменён на площадке, третий пропал
    t._fill(t.orders[ids[0]], Decimal("40000"))
    t.orders[ids[1]]["status"] = "canceled"
    lost = t.orders.pop(ids[2])

    after = BybitExecutor(transport=t, mode="live")  # новый процесс: памяти нет
    restored = after.restore(session)
    states = {o.id: o.state for o in restored}
    assert states == {
        ids[0]: OrderState.FILLED,
        ids[1]: OrderState.CANCELLED,
        ids[2]: OrderState.CANCELLED,
    }
    assert next(o for o in restored if o.id == ids[2]).reason == "нет на площадке"
    assert {o.signal_id for o in restored} == {"sig-r0", "sig-r1", "sig-r2"}
    rows = {r.id: r for r in session.query(OrderRow).all()}
    assert rows[ids[0]].state == "filled" and rows[ids[0]].filled_qty == Decimal("0.01")
    assert rows[ids[1]].state == "cancelled" and rows[ids[2]].state == "cancelled"
    assert after.open_orders() == [] and lost["clientOrderId"] == client_order_id("sig-r2", "bybit")
    # повторный рестарт — активных нет, ничего не трогаем
    assert BybitExecutor(transport=t, mode="live").restore(session) == []


def test_perp_leverage_funding_and_liquidation_from_venue():
    t = _transport(max_leverage=10)
    ex = BybitExecutor(transport=t, mode="live")
    rejected = ex.place(_intent("sig-l1", leverage=25), mode="live")
    assert rejected.state == OrderState.REJECTED and "плечо" in rejected.reason
    assert "create_order" not in t.calls, (
        "ордер с плечом выше максимума не должен уходить на площадку"
    )

    order = ex.place(_intent("sig-l2", leverage=5), mode="live")
    assert order.state == OrderState.FILLED and t.leverage[PERP] == 5
    info = ex.perp_info(PERP)
    assert info.leverage == Decimal(5) and info.max_leverage == Decimal(10)
    assert info.funding_rate == Decimal("0.0001") and info.next_funding_at == T0 + timedelta(
        hours=8
    )
    assert info.mark_price == Decimal("50010")
    assert info.liquidation_price == Decimal("40000.8")  # 50001 * (1 - 1/5), из ответа площадки
    pos = next(p for p in ex.positions() if p.instrument == PERP)
    assert pos.leverage == Decimal(5) and pos.side == "buy"

    t.add_funding_payment(PERP, Decimal("-0.05"), T0 + timedelta(hours=8))
    payments = ex.funding_payments(since=T0)
    assert [(p.instrument, p.amount) for p in payments] == [(PERP, Decimal("-0.05"))]
    assert payments[0].ts == T0 + timedelta(hours=8)


def test_keys_missing_is_read_only_and_withdraw_key_is_rejected():
    t = _transport(has_keys=False)
    ex = BybitExecutor(transport=t, mode="live")
    assert ex.rights().trade is False and ex.rights().withdraw is False
    paper = BybitExecutor(transport=t, mode="paper")
    assert paper.place(_intent("sig-p", mode="paper"), mode="paper").state == OrderState.FILLED
    with pytest.raises(NotConnected):
        ex.place(_intent("sig-x"), mode="live")
    assert ex.health().status == "ok" and ex.positions() == [] and ex.balance() == []

    bad = BybitExecutor(
        transport=_transport(rights={"trade": True, "withdraw": True}), mode="live"
    )
    assert bad.rights().withdraw is True
    with pytest.raises(KeyRejected):
        bad.place(_intent("sig-y"), mode="live")


def test_paper_and_live_state_do_not_mix_and_balance_is_never_invented():
    """Ревью: риск-ядро не должно видеть бумажные цифры под видом живых и наоборот."""
    import ccxt

    from lab.executors.cex import CexError, PaperFill

    t = _transport()
    paper = BybitExecutor(transport=t, mode="paper")
    live = BybitExecutor(transport=t, mode="live")
    with pytest.raises(CexError):
        paper.place(_intent("sig-m0"), mode="live")

    porder = paper.place(_intent("sig-m1", mode="paper"), mode="paper")
    lorder = live.place(_intent("sig-m2", leverage=2), mode="live")
    assert porder.state == OrderState.FILLED and lorder.state == OrderState.FILLED

    assert [p.leverage for p in live.positions() if p.instrument == PERP] == [Decimal(2)]
    assert [p.leverage for p in paper.positions() if p.instrument == PERP] == [Decimal(1)]
    assert {f.order_id for f in live.fills(since=T0)} == {lorder.id}
    pfills = [f for f in paper.fills(since=T0)]
    assert [f.order_id for f in pfills] == [porder.id]
    assert isinstance(pfills[0], PaperFill) and pfills[0].ref_price == Decimal("50000")  # мид
    assert "fetch_my_trades" not in t.calls[: t.calls.index("create_order")], (
        "paper не ходит за филлами"
    )

    # paper-баланс — расчётный, из памяти; live — только с площадки
    pbal = paper.balance()[0]
    assert pbal.total == Decimal("10000") - Decimal("500.01") - Decimal("0.50001")
    assert pbal.stale is False
    lbal = live.balance()[0]
    assert lbal.total == t.balance["USDT"] and lbal.stale is False  # только ответ площадки
    t.offline = True
    stale = live.balance()[0]
    assert stale.total == lbal.total and stale.stale is True  # последний успешный ответ
    fresh = BybitExecutor(transport=t, mode="live")
    with pytest.raises(ccxt.NetworkError):
        fresh.balance()  # кэша нет — не выдумываем
    assert paper.balance()[0].total == pbal.total  # paper не зависит от связи


def test_unknown_venue_leverage_limit_rejects_leverage_above_one():
    t = _transport()
    t.market = lambda symbol: (_ for _ in ()).throw(KeyError(symbol))  # рынки не загружены
    ex = BybitExecutor(transport=t, mode="live")
    assert ex.max_leverage(PERP) == Decimal(1)
    assert ex.place(_intent("sig-u1", leverage=2), mode="live").state == OrderState.REJECTED
    assert "create_order" not in t.calls
