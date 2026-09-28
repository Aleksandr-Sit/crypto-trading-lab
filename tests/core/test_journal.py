"""Журнал (R31i, R31i.1, A03, §10): сигнал до исхода, филлы → сделки по FIFO, P&L, CSV, сверка."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Costs, Fill, OrderIntent, Signal, StopSpec, StrategyManifest
from lab.core.journal import Journal, ReconcileMismatch
from lab.core.registry import Registry

T0 = datetime(2026, 3, 1, 12, tzinfo=UTC)


@pytest.fixture
def journal(session):
    reg = Registry(session)
    strategy = reg.add(
        StrategyManifest(
            slug="fifo",
            branch="cex-spot",
            venue="bybit",
            source_kind="test",
            instruments=["BTC/USDT"],
            timeframe="1h",
            stop=StopSpec(daily_pct=Decimal(5)),
        )
    )
    return Journal(session), strategy.id


def _signal(sid: str, side: str, size: str, at: datetime) -> Signal:
    return Signal(
        strategy_id=sid,
        decided_at=at,
        instrument="BTC/USDT",
        side=side,
        size=Decimal(size),
        price_ref=Decimal(100),
        inputs_hash="abc",
        ttl_s=600,
    )


def _intent(sid: str, signal_id: str, side: str, qty: str, coid: str) -> OrderIntent:
    return OrderIntent(
        strategy_id=sid,
        venue="bybit",
        instrument="BTC/USDT",
        side=side,
        qty=Decimal(qty),
        order_type="market",
        mode="paper",
        signal_id=signal_id,
        client_order_id=coid,
    )


def _fill(fid: str, order_id: str, price: str, qty: str, fee: str, ts: datetime) -> Fill:
    return Fill(
        id=fid,
        order_id=order_id,
        price=Decimal(price),
        qty=Decimal(qty),
        fee=Decimal(fee),
        fee_asset="USDT",
        ts=ts,
    )


def test_signal_recorded_before_outcome_and_fills_link_fifo(journal):
    j, sid = journal
    s1 = j.record_signal(_signal(sid, "buy", "2", T0))
    assert s1.outcome == "pending" and s1.decided_at == T0
    o1 = j.record_order(_intent(sid, s1.id, "buy", "2", "c1"), order_id="o1")
    # Два лота: 1 по 100 и 1 по 110.
    j.record_fill(
        _fill("f1", o1.id, "100", "1", "0.1", T0 + timedelta(minutes=1)),
        costs=Costs(fee=Decimal("0.1"), slippage=Decimal("0.05")),
    )
    j.record_fill(
        _fill("f2", o1.id, "110", "1", "0.11", T0 + timedelta(minutes=2)),
        costs=Costs(fee=Decimal("0.11")),
    )
    assert j.signal(s1.id).outcome == "executed"
    assert j.open_trades(sid) and len(j.open_trades(sid)) == 2

    # Продажа 1.5 по 120: FIFO закрывает лот 100 целиком (+20) и половину лота 110 (+5).
    s2 = j.record_signal(_signal(sid, "sell", "1.5", T0 + timedelta(hours=1)))
    o2 = j.record_order(_intent(sid, s2.id, "sell", "1.5", "c2"), order_id="o2")
    closed = j.record_fill(
        _fill("f3", o2.id, "120", "1.5", "0.18", T0 + timedelta(hours=1, minutes=1)),
        costs=Costs(fee=Decimal("0.18")),
    )
    assert [(t.qty, t.pnl_gross) for t in closed] == [
        (Decimal("1"), Decimal("20")),
        (Decimal("0.5"), Decimal("5")),
    ]
    # Издержки первой сделки: открытие 0.1+0.05 + доля закрытия 0.18 × (1/1.5) = 0.12 → 0.27.
    assert closed[0].fee == Decimal("0.1") + Decimal("0.18") / Decimal("1.5")
    assert closed[0].slippage == Decimal("0.05")
    assert closed[0].pnl_net == Decimal("20") - closed[0].fee - Decimal("0.05")
    open_left = j.open_trades(sid)
    assert len(open_left) == 1 and open_left[0].qty == Decimal("0.5")

    pnl = j.pnl(sid, marks={"BTC/USDT": Decimal("130")})
    assert pnl.realized == closed[0].pnl_net + closed[1].pnl_net
    # Остаток 0.5 по 110 при марке 130: +10 минус его открывающие издержки 0.11 × 0.5.
    assert pnl.unrealized == Decimal("10") - Decimal("0.11") / 2
    assert pnl.total == pnl.realized + pnl.unrealized

    csv = j.export_csv(strategy_id=sid)
    lines = csv.strip().splitlines()
    assert lines[0].startswith("trade_id,strategy_id,instrument,side,qty,entry_price,exit_price")
    assert len(lines) == 4  # 2 закрытые + 1 открытая
    assert ",20," in lines[1] or ",20.000000000000," in lines[1]


def test_open_qty_counts_by_side_and_mode_and_live_fill_skips_paper_lot(journal):
    """Позиция стратегии для `place_signal`: лоты считаются по стороне и режиму, и живой
    филл не закрывает бумажный лот — иначе живая продажа «закрыла» бы то, чего на бирже нет."""
    j, sid = journal
    s1 = j.record_signal(_signal(sid, "buy", "2", T0))
    o1 = j.record_order(_intent(sid, s1.id, "buy", "2", "c1"), order_id="o1")  # бумага
    j.record_fill(_fill("f1", o1.id, "100", "2", "0", T0))
    assert j.open_qty(sid, "BTC/USDT", "long", mode="paper") == Decimal("2")
    assert j.open_qty(sid, "BTC/USDT", "long", mode="live") == Decimal(0)
    assert j.open_qty(sid, "BTC/USDT", "short", mode="paper") == Decimal(0)

    s2 = j.record_signal(_signal(sid, "sell", "0.5", T0 + timedelta(hours=1)))
    live = _intent(sid, s2.id, "sell", "0.5", "c2").model_copy(update={"mode": "live"})
    o2 = j.record_order(live, order_id="o2")
    closed = j.record_fill(_fill("f2", o2.id, "110", "0.5", "0", T0 + timedelta(hours=1)))
    assert closed == []
    assert j.open_qty(sid, "BTC/USDT", "long", mode="paper") == Decimal("2")
    assert j.open_qty(sid, "BTC/USDT", "short", mode="live") == Decimal("0.5")


def test_close_trade_explicitly_and_by_strategy_filters(journal):
    j, sid = journal
    s = j.record_signal(_signal(sid, "buy", "1", T0))
    o = j.record_order(_intent(sid, s.id, "buy", "1", "c9"), order_id="o9")
    j.record_fill(_fill("f9", o.id, "100", "1", "0.1", T0), costs=Costs(fee=Decimal("0.1")))
    (trade,) = j.open_trades(sid)
    closed = j.close_trade(
        trade.id,
        _fill("f10", o.id, "90", "1", "0.09", T0 + timedelta(hours=2)),
        costs=Costs(fee=Decimal("0.09"), funding=Decimal("0.02")),
    )
    assert closed.pnl_gross == Decimal("-10")
    assert closed.funding == Decimal("0.02")
    assert closed.pnl_net == Decimal("-10") - Decimal("0.19") - Decimal("0.02")
    assert j.open_trades(sid) == []
    assert j.closed_trades(sid)[0].id == trade.id
    assert j.closed_trades("nope") == []


def test_reconcile_reports_mismatch_event(journal):
    j, sid = journal
    s = j.record_signal(_signal(sid, "buy", "1", T0))
    o = j.record_order(_intent(sid, s.id, "buy", "1", "c1"), order_id="o1")
    ours = _fill("f1", o.id, "100", "1", "0.1", T0)
    j.record_fill(ours, costs=Costs(fee=Decimal("0.1")))

    same = j.reconcile("bybit", [ours], since=T0 - timedelta(days=1))
    assert same.ok and same.mismatches == [] and same.event is None

    # Площадка знает филл, которого нет у нас, и цену другого — расхождение > порога.
    venue_fills = [
        ours.model_copy(update={"price": Decimal("101")}),
        _fill("f2", o.id, "100", "1", "0.1", T0 + timedelta(minutes=5)),
    ]
    report = j.reconcile(
        "bybit", venue_fills, since=T0 - timedelta(days=1), tolerance=Decimal("0.005")
    )  # 0.5 %; цена расходится на 1 %
    assert not report.ok
    kinds = sorted(m.kind for m in report.mismatches)
    assert kinds == ["missing_in_journal", "price"]
    assert isinstance(report.event, ReconcileMismatch)
    assert report.event.venue == "bybit" and report.event.count == 2
    # Журнал не правится тихо: наш филл остался по 100.
    assert j.closed_trades(sid) == [] and j.open_trades(sid)[0].entry_price == Decimal("100")


def _spot_fill(fid: str, order_id: str, price: str, qty: str, fee: str, asset: str) -> Fill:
    return _fill(fid, order_id, price, qty, fee, T0).model_copy(update={"fee_asset": asset})


def test_spot_fee_in_received_coin_nets_the_lot_and_books_fee_in_quote(journal):
    """Пробел 4: на споте покупка платит комиссию в получаемой монете. Купили 0.01 BTC
    по 60 000 с комиссией 0.00001 BTC — на счёте 0.00999, комиссия стоит $0.60, а не
    «0.00001 доллара». Итог сделки обязан совпасть с движением денег на счёте."""
    j, sid = journal
    s1 = j.record_signal(_signal(sid, "buy", "0.01", T0))
    o1 = j.record_order(_intent(sid, s1.id, "buy", "0.01", "c1"), order_id="o1")
    j.record_fill(_spot_fill("f1", o1.id, "60000", "0.01", "0.00001", "BTC"))
    lot = j.open_trades(sid)[0]
    assert lot.qty == Decimal("0.00999")  # столько и продать можно
    assert lot.fee == Decimal("0.6")  # 0.00001 BTC × 60 000
    assert j.open_qty(sid, "BTC/USDT", "long", mode="paper") == Decimal("0.00999")

    s2 = j.record_signal(_signal(sid, "sell", "0.00999", T0 + timedelta(days=1)))
    o2 = j.record_order(_intent(sid, s2.id, "sell", "0.00999", "c2"), order_id="o2")
    closed = j.record_fill(_spot_fill("f2", o2.id, "61000", "0.00999", "0.60939", "USDT"))
    assert [t.qty for t in closed] == [Decimal("0.00999")] and not j.open_trades(sid)
    spent = Decimal("600")  # 0.01 × 60 000 котировкой
    received = Decimal("0.00999") * 61000 - Decimal("0.60939")
    assert closed[0].pnl_net == received - spent == Decimal("8.78061")


def test_spot_partial_close_splits_netted_lot_and_its_fee(journal):
    j, sid = journal
    s1 = j.record_signal(_signal(sid, "buy", "0.01", T0))
    o1 = j.record_order(_intent(sid, s1.id, "buy", "0.01", "c1"), order_id="o1")
    j.record_fill(_spot_fill("f1", o1.id, "60000", "0.01", "0.00001", "BTC"))
    s2 = j.record_signal(_signal(sid, "sell", "0.004995", T0 + timedelta(hours=1)))
    o2 = j.record_order(_intent(sid, s2.id, "sell", "0.004995", "c2"), order_id="o2")
    closed = j.record_fill(_spot_fill("f2", o2.id, "60000", "0.004995", "0", "USDT"))
    assert closed[0].qty == Decimal("0.004995") and closed[0].fee == Decimal("0.3")
    left = j.open_trades(sid)
    assert left[0].qty == Decimal("0.004995") and left[0].fee == Decimal("0.3")


def test_fee_in_third_coin_without_costs_is_refused_before_anything_is_written(journal):
    """Комиссия в BNB: цены монеты у журнала нет — ошибка ДО записи, число не угадывается.
    Издержки с оценкой по тарифу передаёт воркер (`ops.worker._fill_costs`)."""
    from lab.core.costs import UnknownFeeAsset
    from lab.db.models import FillRow

    j, sid = journal
    s1 = j.record_signal(_signal(sid, "buy", "0.01", T0))
    o1 = j.record_order(_intent(sid, s1.id, "buy", "0.01", "c1"), order_id="o1")
    fill = _spot_fill("f1", o1.id, "60000", "0.01", "0.0008", "BNB")
    with pytest.raises(UnknownFeeAsset):
        j.record_fill(fill)
    assert j.s.get(FillRow, "f1") is None and not j.open_trades(sid)
    # с переданными издержками — лот целый: BNB базовую монету не уменьшает
    j.record_fill(fill, costs=Costs(fee=Decimal("0.6")))
    assert j.open_trades(sid)[0].qty == Decimal("0.01")


def test_raw_fill_keeps_venue_fee_so_reconcile_matches(journal):
    """Строка филла — как у биржи (0.00001 BTC), иначе суточная сверка видела бы
    расхождение комиссии на каждом спотовом филле."""
    j, sid = journal
    s1 = j.record_signal(_signal(sid, "buy", "0.01", T0))
    o1 = j.record_order(_intent(sid, s1.id, "buy", "0.01", "c1"), order_id="o1")
    fill = _spot_fill("f1", o1.id, "60000", "0.01", "0.00001", "BTC")
    j.record_fill(fill)
    report = j.reconcile("bybit", [fill], since=T0 - timedelta(hours=1))
    assert report.ok, report.mismatches
