"""ops.worker.place_signal: закрывающая часть сигнала идёт `reduce_only` по лотам стратегии.

Пробел 1 из `docs/research/allocator-2026-09-27.md`: живой путь собирал ордер без
`reduce_only`, и стратегия в `degraded` получала отказ на выходе из собственной позиции —
послабление «закрыть можно всегда» жило в `risk.check`, но до него не доходило.
"""

from contextlib import contextmanager
from decimal import Decimal

import pytest

from lab.contracts import Fill, OrderIntent, Signal, Status, StopSpec, StrategyManifest
from lab.core.journal import Journal
from lab.core.registry import Registry
from lab.core.risk import Allow
from lab.db.models import OrderRow, StrategyRow
from lab.executors.cex import BybitExecutor, client_order_id
from lab.feeds.cex import FakeTransport
from lab.ops.stop_watch import StopWatch
from lab.ops.worker import Worker, _legs

PERP = "BTC/USDT:USDT"


class FakeBot:
    def __init__(self) -> None:
        self.cards: list[tuple[str, dict]] = []

    def send_card_sync(self, kind, payload) -> None:
        self.cards.append((kind, payload))

    def notify_transition(self, transition) -> None:
        pass


class AllowAll:
    """Риск без лимитов: проверяем разбивку сигнала, а не потолки веток."""

    def __init__(self) -> None:
        self.seen: list[OrderIntent] = []

    def check(self, intent):
        self.seen.append(intent)
        return Allow()


@pytest.fixture
def scope(session):
    @contextmanager
    def _scope():
        yield session

    return _scope


@pytest.fixture
def lab(session, scope):
    t = FakeTransport("bybit")
    t.set_ticker(PERP, Decimal("50000"), spread=Decimal("2"))
    strategy = Registry(session).add(
        StrategyManifest(
            slug="close",
            branch="cex-perp",
            venue="bybit",
            source_kind="test",
            instruments=[PERP],
            timeframe="1h",
            stop=StopSpec(daily_pct=Decimal(5)),
        )
    )
    bot = FakeBot()
    worker = Worker(
        scope,
        bot=bot,
        executors={"bybit": BybitExecutor(transport=t, mode="live")},
        clock=lambda: t.now,
    )
    return worker, t, strategy.id, bot


def _signal(session, sid: str, side: str, size: str, at) -> str:
    return (
        Journal(session)
        .record_signal(
            Signal(
                strategy_id=sid,
                decided_at=at,
                instrument=PERP,
                side=side,
                size=Decimal(size),
                price_ref=Decimal("50000"),
                inputs_hash="h",
                ttl_s=60,
            )
        )
        .id
    )


def _hold(session, sid: str, side: str, qty: str, at, *, mode: str = "live") -> None:
    """Открытый лот стратегии в журнале — как после исполненного ордера."""
    j = Journal(session)
    signal_id = _signal(session, sid, side, qty, at)
    j.record_order(
        OrderIntent(
            strategy_id=sid,
            venue="bybit",
            instrument=PERP,
            side=side,
            qty=Decimal(qty),
            order_type="market",
            mode=mode,
            signal_id=signal_id,
            client_order_id=f"seed-{signal_id}",
        ),
        order_id=f"seed-{signal_id}",
    )
    j.record_fill(
        Fill(
            id=f"seed-fill-{signal_id}",
            order_id=f"seed-{signal_id}",
            price=Decimal("49000"),
            qty=Decimal(qty),
            fee=Decimal(0),
            fee_asset="USDT",
            ts=at,
        )
    )


def _degrade(session, sid: str) -> None:
    session.get(StrategyRow, sid).status = Status.DEGRADED.value
    session.flush()


def test_degraded_strategy_closes_its_position_through_live_path(session, lab):
    worker, t, sid, bot = lab
    _hold(session, sid, "buy", "0.01", t.now)
    _degrade(session, sid)
    sig = _signal(session, sid, "sell", "0.01", t.now)

    (order,) = worker.place_signal(sig)

    assert t.orders[order.id]["reduceOnly"] is True
    row = session.get(OrderRow, order.id)
    assert row.reduce_only is True and row.client_order_id == client_order_id(sig, "bybit")
    j = Journal(session)
    assert j.open_trades(sid) == []
    assert [tr.qty for tr in j.closed_trades(sid)] == [Decimal("0.01")]
    assert [kind for kind, _ in bot.cards] == ["fill"]


def test_degraded_strategy_without_position_still_cannot_open(session, lab):
    worker, t, sid, bot = lab
    _degrade(session, sid)

    assert worker.place_signal(_signal(session, sid, "sell", "0.01", t.now)) == []

    assert t.orders == {}
    ((kind, payload),) = bot.cards
    assert kind == "alert" and "открытие позиций запрещено" in payload["detail"]


def test_paper_lot_does_not_make_live_order_closing(session, lab):
    """Позиция берётся в режиме ордера: бумажного лота на бирже нет, закрывать там нечего."""
    worker, t, sid, bot = lab
    _hold(session, sid, "buy", "0.01", t.now, mode="paper")
    _degrade(session, sid)

    assert worker.place_signal(_signal(session, sid, "sell", "0.01", t.now)) == []
    assert t.orders == {}


def test_flip_in_degraded_closes_and_refuses_only_the_opening_rest(session, lab):
    worker, t, sid, bot = lab
    _hold(session, sid, "buy", "0.01", t.now)
    _degrade(session, sid)

    (order,) = worker.place_signal(_signal(session, sid, "sell", "0.03", t.now))

    assert t.orders[order.id]["amount"] == 0.01 and t.orders[order.id]["reduceOnly"] is True
    assert [kind for kind, _ in bot.cards] == ["fill", "alert"]
    assert "отклонён только остаток переворота" in bot.cards[-1][1]["detail"]
    assert Journal(session).open_trades(sid) == []


def test_flip_is_close_then_open_with_distinct_client_ids(session, lab):
    worker, t, sid, bot = lab
    risk = AllowAll()
    worker.stop_watch = StopWatch(risk, worker._ladder_for)
    _hold(session, sid, "buy", "0.01", t.now)
    sig = _signal(session, sid, "sell", "0.03", t.now)

    placed = worker.place_signal(sig)

    assert [(i.qty, i.reduce_only, i.client_order_id) for i in risk.seen] == [
        (Decimal("0.01"), True, client_order_id(sig, "bybit")),
        (Decimal("0.02"), False, client_order_id(f"{sig}:open", "bybit")),
    ]
    assert [t.orders[o.id]["reduceOnly"] for o in placed] == [True, False]
    (left,) = Journal(session).open_trades(sid)
    assert (left.side, left.qty) == ("short", Decimal("0.02"))


def test_opening_signal_is_one_order_with_the_signal_client_id(session, lab):
    """Без позиции — прежнее поведение: один открывающий ордер, client id от сигнала."""
    worker, t, sid, bot = lab
    risk = AllowAll()
    worker.stop_watch = StopWatch(risk, worker._ladder_for)
    sig = _signal(session, sid, "buy", "0.01", t.now)

    (order,) = worker.place_signal(sig)

    (intent,) = risk.seen
    assert intent.reduce_only is False and intent.client_order_id == client_order_id(sig, "bybit")
    assert t.orders[order.id]["reduceOnly"] is False


@pytest.mark.parametrize(
    ("size", "held", "legs"),
    [
        ("1", "0", [("1", False, "s")]),
        ("1", "3", [("1", True, "s")]),  # частичное закрытие: остальное держится
        ("1", "1", [("1", True, "s")]),
        ("3", "1", [("1", True, "s"), ("2", False, "s:open")]),
        ("0", "1", []),
    ],
)
def test_legs_split_signal_by_held_position(size, held, legs):
    got = _legs(Decimal(size), Decimal(held), "s")
    assert got == [(Decimal(q), ro, key) for q, ro, key in legs]


# -- спот: комиссия в получаемой монете (пробел 4, allocator-2026-09-27.md) -------------

SPOT = "BTC/USDT"


@pytest.fixture
def spot_lab(session, scope):
    """Спотовая стратегия на фейке, который берёт комиссию как биржа: покупка платит
    базовой монетой. Риск без лимитов — проверяем объёмы и журнал, а не потолки веток."""
    t = FakeTransport("bybit")
    t.set_ticker(SPOT, Decimal("50000"), spread=Decimal("2"))
    strategy = Registry(session).add(
        StrategyManifest(
            slug="spot",
            branch="cex-spot",
            venue="bybit",
            source_kind="test",
            instruments=[SPOT],
            timeframe="1d",
            stop=StopSpec(daily_pct=Decimal(5)),
        )
    )
    bot = FakeBot()
    worker = Worker(
        scope,
        bot=bot,
        executors={"bybit": BybitExecutor(transport=t, mode="live")},
        clock=lambda: t.now,
    )
    worker.stop_watch = StopWatch(AllowAll(), worker._ladder_for)
    return worker, t, strategy.id, bot


def _spot_signal(session, sid: str, side: str, size: str, at) -> str:
    signal = Signal(
        strategy_id=sid,
        decided_at=at,
        instrument=SPOT,
        side=side,
        size=Decimal(size),
        price_ref=Decimal("50000"),
        inputs_hash="h",
        ttl_s=60,
    )
    return Journal(session).record_signal(signal).id


def test_spot_round_trip_sells_what_the_account_holds_and_pnl_is_cash(session, spot_lab):
    """Правило ротации: продать «сколько купила» (0.01). На счёте после покупки 0.00999 —
    до правки уходила продажа 0.01 и биржа отвечала «недостаточно средств»."""
    worker, t, sid, bot = spot_lab
    cash0 = t.balance["USDT"]

    worker.place_signal(_spot_signal(session, sid, "buy", "0.01", t.now))
    (lot,) = Journal(session).open_trades(sid)
    assert lot.qty == Decimal("0.00999") == t.balance["BTC"]
    assert lot.fee == Decimal("0.50001")  # 0.00001 BTC × 50 001, а не «0.00001 доллара»
    assert Decimal(bot.cards[-1][1]["costs"]) == Decimal("0.50001")

    (sold,) = worker.place_signal(_spot_signal(session, sid, "sell", "0.01", t.now))

    assert t.orders[sold.id]["amount"] == 0.00999 and t.balance["BTC"] == 0
    j = Journal(session)
    (trade,) = j.closed_trades(sid)
    assert not j.open_trades(sid)
    assert trade.pnl_net == t.balance["USDT"] - cash0  # журнал = движение денег на счёте
    assert [kind for kind, _ in bot.cards] == ["fill", "fill"]  # разница — комиссия: без тревоги


def test_spot_sell_without_position_sends_nothing_and_alerts(session, spot_lab):
    """Шорта на споте нет: продавать нечего — ни ордера, ни попытки, тревога оператору."""
    worker, t, sid, bot = spot_lab

    assert worker.place_signal(_spot_signal(session, sid, "sell", "0.01", t.now)) == []

    assert t.orders == {}
    ((kind, payload),) = bot.cards
    assert kind == "alert" and "разошлись" in payload["detail"]


def test_spot_sell_far_above_lot_sells_the_lot_and_alerts(session, spot_lab):
    """Решение владельца 28.09.2026: расхождение больше 1% — продать известную позицию
    и поднять тревогу, а не стоять в позиции до разбора."""
    worker, t, sid, bot = spot_lab
    worker.place_signal(_spot_signal(session, sid, "buy", "0.01", t.now))

    (sold,) = worker.place_signal(_spot_signal(session, sid, "sell", "0.02", t.now))

    assert t.orders[sold.id]["amount"] == 0.00999 and t.orders[sold.id]["side"] == "sell"
    assert not Journal(session).open_trades(sid)
    assert [kind for kind, _ in bot.cards] == ["fill", "alert", "fill"]
    assert "разошлись" in bot.cards[1][1]["detail"]


class _BnbFees(FakeTransport):
    """Binance со включённой оплатой комиссий BNB: комиссия в третьей монете."""

    def _fill(self, order, price) -> None:
        super()._fill(order, price)
        self.my_trades[-1]["fee"] = {"cost": 0.0008, "currency": "BNB"}


def test_fee_in_third_coin_is_booked_by_tariff_and_alerts(session, spot_lab):
    worker, _, sid, bot = spot_lab
    t = _BnbFees("bybit")
    t.set_ticker(SPOT, Decimal("50000"), spread=Decimal("2"))
    worker.executors["bybit"] = BybitExecutor(transport=t, mode="live")

    worker.place_signal(_spot_signal(session, sid, "buy", "0.01", t.now))

    (lot,) = Journal(session).open_trades(sid)
    assert lot.qty == Decimal("0.01")  # BNB базовую монету не уменьшает
    # тейкер спота Bybit 10 б.п. с номинала ≈ $0.50, с запасом на спред — не 0.0008
    assert Decimal("0.5") < lot.fee < Decimal("0.51")
    alert = next(p for kind, p in bot.cards if kind == "alert")
    assert "0.0008 BNB" in alert["detail"] and "оценка по тарифу" in alert["detail"]


def test_legs_can_drop_the_opening_rest():
    assert _legs(Decimal("3"), Decimal("1"), "s", open_rest=False) == [(Decimal("1"), True, "s")]
    assert _legs(Decimal("1"), Decimal("0"), "s", open_rest=False) == []
