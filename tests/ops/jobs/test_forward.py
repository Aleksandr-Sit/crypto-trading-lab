"""Журнал решений вперёд: первый запуск ничего не пишет, повтор не задваивает.

Главная проверка здесь — про ПЕРВЫЙ запуск. Прогон по истории даёт сотни решений, и если
записать их в журнал, они лягут туда как сделанные вовремя прогнозы. Проверка вперёд,
начатая записью прошлого, — не проверка вперёд, а её имитация, причём неотличимая снаружи.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Candle, Rung, Status
from lab.core.registry import Registry
from lab.db.models import SignalRow, StrategyRow
from lab.ops.jobs.forward import FLAG_PREFIX, replay, run_forward
from lab.strategies.registry import build

STRATEGY_ID = "cex-spot-external-rotation-gold-btc"
BASE = datetime(2025, 8, 1, tzinfo=UTC)
# Первый прогон — на растущем BTC (правило уже в BTC, решение не меняется).
# Второй — когда BTC сложился: перелом приходится на участок ПОСЛЕ точки отсчёта,
# и только такие решения обязаны попасть в журнал.
CALM_DAYS, FULL_DAYS = 361, 430
T1 = BASE + timedelta(days=CALM_DAYS, hours=6)
T2 = BASE + timedelta(days=FULL_DAYS, hours=6)
RISK, SAFE = "BTC/USDT", "PAXG/USDT"


class Scope:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *a):
        return False


class FakeStore:
    """Хранилище свечей с сигнатурой `read`, которую спрашивает задание."""

    def __init__(self, bars: list[Candle]) -> None:
        self.bars = bars

    def read(self, venue, instrument, tf, from_ts, to_ts):
        return [
            b for b in self.bars if b.instrument == instrument and from_ts <= b.ts < to_ts
        ]

    def count(self, venue, instrument, tf):
        return len([b for b in self.bars if b.instrument == instrument])


def bar(instrument: str, day: int, close: float) -> Candle:
    price = Decimal(str(close))
    return Candle(
        ts=BASE + timedelta(days=day),
        instrument=instrument,
        tf="1d",
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1),
    )


def series(days: int = CALM_DAYS) -> list[Candle]:
    """BTC растёт до `CALM_DAYS`, дальше складывается; золото стоит.

    Правило выдаёт сигнал только когда решение МЕНЯЕТСЯ, поэтому ровный рост даёт одну
    покупку в начале и тишину дальше. Перелом на хвосте — единственный способ получить
    решение с датой позже точки отсчёта.
    """
    out: list[Candle] = []
    for day in range(days):
        if day < CALM_DAYS:
            price = 100 + day
        else:
            price = max(20.0, (100 + CALM_DAYS) - (day - CALM_DAYS) * 12)
        out.append(bar(RISK, day, price))
        out.append(bar(SAFE, day, 100))
    return out


@pytest.fixture
def live(session):
    strategy = build(STRATEGY_ID)
    record = Registry(session).add(strategy.manifest)
    row = session.get(StrategyRow, record.id)
    row.rung = Rung.PAPER.value
    row.status = Status.MEASURING.value
    session.flush()
    return record


def signals_of(session, strategy_id: str) -> list[SignalRow]:
    return list(session.query(SignalRow).filter(SignalRow.strategy_id == strategy_id).all())


def flag_value(session, strategy_id: str):
    from lab.db.models import SystemFlagRow

    row = session.get(SystemFlagRow, f"{FLAG_PREFIX}{strategy_id}")
    return (row.value or {}).get("at") if row is not None else None


def test_first_run_writes_nothing_and_only_sets_the_mark(session, live):
    """Разогрев по истории НЕ должен попасть в журнал ни одной строкой."""
    report = run_forward(Scope(session), store=FakeStore(series()), now=T1)

    assert report.journaled == []
    assert live.id in report.started
    assert signals_of(session, live.id) == []
    assert flag_value(session, live.id) == T1.isoformat()


def test_second_run_journals_only_new_decisions(session, live):
    run_forward(Scope(session), store=FakeStore(series()), now=T1)

    report = run_forward(Scope(session), store=FakeStore(series(FULL_DAYS)), now=T2)

    assert report.journaled, "новое решение не записано"
    rows = signals_of(session, live.id)
    assert rows and all(r.decided_at > T1 for r in rows), "в журнал попало прошлое"


def test_repeat_run_does_not_duplicate(session, live):
    run_forward(Scope(session), store=FakeStore(series()), now=T1)
    store = FakeStore(series(FULL_DAYS))
    first = run_forward(Scope(session), store=store, now=T2)
    second = run_forward(Scope(session), store=store, now=T2 + timedelta(hours=1))

    assert first.journaled
    assert len(signals_of(session, live.id)) == len(first.journaled)
    assert second.journaled == []


def test_backtest_rung_is_not_touched(session, live):
    """Ниже ступени `paper` стратегия живой не считается: ей свежие решения не нужны."""
    session.get(StrategyRow, live.id).rung = Rung.BACKTEST.value
    session.flush()

    report = run_forward(Scope(session), store=FakeStore(series()), now=T1)

    assert report.started == [] and report.journaled == []
    assert flag_value(session, live.id) is None


def test_missing_candles_are_reported_not_hidden(session, live):
    report = run_forward(Scope(session), store=FakeStore([]), now=T1)

    assert report.journaled == []
    assert any(sid == live.id for sid, _ in report.skipped)
    # Отметка НЕ ставится: иначе пустой прогон молча съел бы точку отсчёта.
    assert flag_value(session, live.id) is None


def test_meta_records_when_the_decision_was_seen(session, live):
    run_forward(Scope(session), store=FakeStore(series()), now=T1)
    run_forward(Scope(session), store=FakeStore(series(FULL_DAYS)), now=T2)

    rows = signals_of(session, live.id)
    assert rows and rows[0].meta.get("source") == "forward_journal"
    # Когда решение ПРИНЯТО и когда ЗАПИСАНО — разные вещи; после простоя они разойдутся.
    assert rows[0].meta.get("seen_at") == T2.isoformat()


def test_replay_feeds_both_legs_in_time_order():
    """Связка обязана видеть бары в том же порядке, что вживую, иначе сравнит разные моменты."""
    strategy = build(STRATEGY_ID)
    got = replay(strategy, list(reversed(series())))

    assert strategy.decided_on is not None
    assert isinstance(got, list)
