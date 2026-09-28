"""Ярус размещения в бою: портфель, `stop_watch` и риск-ядро на настоящем конфиге (28.09.2026).

Решение владельца 4 (27.09.2026, `docs/research/allocator-2026-09-27.md`): 20 % банка,
стоп −35 % от вершины капитала стратегии, стоп ветки не применяется. Здесь проверяется то,
чего не видно на фейке портфеля: просадка по ОТКРЫТОЙ позиции от вершины, вершина,
пережившая рестарт, отделение яруса от ветки и пробы задания `stop_watch`, которое до
28.09 вызывалось с пустым списком (пробел 5) и стопа не замечало вовсе.
"""

from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.contracts import Balance, Fill, OrderIntent, Signal, StopSpec, StrategyManifest
from lab.core.journal import Journal
from lab.core.registry import Registry
from lab.db.models import StrategyRow
from lab.ops.portfolio import LivePortfolio
from lab.ops.worker import Worker

T0 = datetime(2026, 9, 28, 12, tzinfo=UTC)
SPOT = "BTC/USDT"
D = Decimal


class PriceExec:
    """Площадка: 1 000 USDT на счёте (банк 1 000 → ярус 200) и цена BTC, которую двигает тест."""

    def __init__(self, price: str = "100000") -> None:
        self.price = D(price)

    def balance(self) -> list[Balance]:
        return [Balance(asset="USDT", total=D(1000), free=D(1000), as_of=T0)]

    def positions(self):
        return []

    def mark_price(self, instrument: str) -> Decimal:
        return self.price


class FakeBot:
    def __init__(self) -> None:
        self.transitions: list = []

    def send_card_sync(self, kind, payload) -> None:
        pass

    def notify_transition(self, transition) -> None:
        self.transitions.append(transition)


@pytest.fixture
def scope(session):
    @contextmanager
    def _scope():
        yield session

    return _scope


def _strategy(session, *, slug: str = "rot", allocation: bool = True) -> str:
    return Registry(session).add(
        StrategyManifest(
            slug=slug,
            branch="cex-spot",
            venue="binance",
            source_kind="test",
            instruments=[SPOT],
            timeframe="1d",
            stop=StopSpec(max_dd_pct=D(35)),
            # slug в отпечаток не входит — без метки одинаковые правила были бы дубликатами
            params={"allocation": allocation, "tag": slug},
        )
    ).id


def _fill(session, strategy_id: str, side: str, qty: str, price: str, n: int) -> None:
    journal = Journal(session)
    signal_id = journal.record_signal(
        Signal(
            strategy_id=strategy_id,
            decided_at=T0,
            instrument=SPOT,
            side=side,
            size=D(qty),
            price_ref=D(price),
            inputs_hash=f"h{n}",
            ttl_s=600,
        )
    ).id
    journal.record_order(
        OrderIntent(
            strategy_id=strategy_id,
            venue="binance",
            instrument=SPOT,
            side=side,  # type: ignore[arg-type]
            qty=D(qty),
            order_type="market",
            mode="live",
            signal_id=signal_id,
            client_order_id=f"coid-{strategy_id}-{n}",
        ),
        order_id=f"o-{strategy_id}-{n}",
    )
    journal.record_fill(
        Fill(
            id=f"f-{strategy_id}-{n}",
            order_id=f"o-{strategy_id}-{n}",
            price=D(price),
            qty=D(qty),
            fee=D(0),
            fee_asset="USDT",
            ts=T0,
        )
    )


def test_drawdown_counts_open_position_from_the_peak(scope, session):
    sid = _strategy(session)
    _fill(session, sid, "buy", "0.002", "100000", 1)  # $200 — вся доля яруса
    ex = PriceExec()
    p = LivePortfolio(scope, executors={"binance": ex}, clock=lambda: T0)

    assert p.allocation_base_usd() == D(200)
    assert p.allocation_exposure_usd() == D(200)
    assert p.strategy_stats(sid).dd_pct == D(0)

    ex.price = D(120_000)  # +40 → вершина капитала 240
    assert p.strategy_stats(sid).dd_pct == D(0)

    ex.price = D(80_000)  # −40 → капитал 160: (240 − 160) / 240
    assert p.strategy_stats(sid).dd_pct == D("33.3333")

    # рестарт: вершина берётся из system_flags, а не с сегодняшней цены
    again = LivePortfolio(scope, executors={"binance": ex}, clock=lambda: T0)
    assert again.strategy_stats(sid).dd_pct == D("33.3333")

    ex.price = D(78_000)  # −44 → 156: ровно 35 %
    assert again.strategy_stats(sid).dd_pct == D(35)


def test_tier_is_kept_out_of_its_branch(scope, session):
    """Сделки яруса не занимают долю `cex-spot` и не встают её дневным стопом."""
    sid = _strategy(session)
    _fill(session, sid, "buy", "0.002", "100000", 1)
    p = LivePortfolio(scope, executors={"binance": PriceExec()}, clock=lambda: T0)
    assert p.branch("cex-spot").exposure_usd == D(0)

    _fill(session, sid, "sell", "0.002", "70000", 2)  # закрыли с убытком −60
    assert p.branch("cex-spot").pnl_day_pct == D(0)

    plain = _strategy(session, slug="plain", allocation=False)
    _fill(session, plain, "buy", "0.001", "100000", 3)
    _fill(session, plain, "sell", "0.001", "90000", 4)  # −10 у соседа — это его ветка
    assert p.branch("cex-spot").pnl_day_pct < 0


def _worker(scope, ex: PriceExec) -> tuple[Worker, FakeBot]:
    bot = FakeBot()
    return Worker(scope, bot=bot, executors={"binance": ex}, clock=lambda: T0), bot


def _to_rung(session, strategy_id: str, rung: str, status: str = "passed") -> None:
    row = session.get(StrategyRow, strategy_id)
    row.rung, row.status = rung, status
    session.flush()


def test_stop_watch_probes_only_trading_strategies_on_money_rungs(scope, session):
    micro = _strategy(session, slug="micro")
    paper = _strategy(session, slug="paper")
    down = _strategy(session, slug="down")
    _to_rung(session, micro, "micro")
    _to_rung(session, paper, "paper")
    _to_rung(session, down, "micro", status="degraded")
    worker, _ = _worker(scope, PriceExec())

    probes = worker.stop_probes()
    assert [p.strategy_id for p in probes] == [micro]
    assert probes[0].mode == "live" and probes[0].reduce_only is False


def test_stop_watch_degrades_tier_strategy_on_drawdown_inside_position(scope, session):
    """Главное: падение внутри открытой позиции теперь видно без нового ордера стратегии."""
    sid = _strategy(session)
    _to_rung(session, sid, "micro")
    _fill(session, sid, "buy", "0.002", "100000", 1)
    ex = PriceExec()
    worker, bot = _worker(scope, ex)

    ex.price = D(70_000)  # −60 → 140 от 200: −30 %
    assert worker.stop_watch.sweep(worker.stop_probes()) == []
    assert session.get(StrategyRow, sid).status == "passed"

    ex.price = D(60_000)  # −80 → 120 от 200: −40 %
    assert worker.stop_watch.sweep(worker.stop_probes()) == [sid]
    assert session.get(StrategyRow, sid).status == "degraded"
    assert bot.transitions, "оператор получает карточку о пробое"
    assert worker.stop_probes() == []  # в degraded больше не пробуем
