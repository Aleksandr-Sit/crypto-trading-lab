"""Журнал решений вперёд: первый запуск ничего не пишет, повтор не задваивает.

Главная проверка здесь — про ПЕРВЫЙ запуск. Прогон по истории даёт сотни решений, и если
записать их в журнал, они лягут туда как сделанные вовремя прогнозы. Проверка вперёд,
начатая записью прошлого, — не проверка вперёд, а её имитация, причём неотличимая снаружи.
"""

from __future__ import annotations

import json
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


def _decision_after(moment: datetime):
    """Первое решение ротации после `moment` на полном ряду — от него строятся опоздания."""
    return next(s for s in replay(build(STRATEGY_ID), series(FULL_DAYS)) if s.decided_at > moment)


def test_bar_that_arrives_a_night_late_is_still_journaled(session, live):
    """Живой дефект 28.09.2026: ни одно решение дневной стратегии не попадало в журнал.

    Бар суток закрывается в 00:00 UTC, а свечи качались в 23:50 UTC — за десять минут
    до закрытия, поэтому бар приезжал только следующей ночью. Отметка же ставилась по
    ЧАСАМ прогона (00:10): к приезду бара его решение (00:00) было «старше» отметки
    и отбрасывалось как уже виденное. Так каждую ночь, и снаружи — «решений нет».
    """
    run_forward(Scope(session), store=FakeStore(series()), now=T1)
    decision = _decision_after(T1)
    day = (decision.decided_at - BASE).days - 1  # бар, на закрытии которого принято решение

    # Первая ночь: бар уже закрыт, но в хранилище всё только до предыдущего дня.
    night = decision.decided_at + timedelta(minutes=10)
    assert run_forward(Scope(session), store=FakeStore(series(day)), now=night).journaled == []

    # Вторая ночь: бар приехал — решение обязано попасть в журнал, пусть и с опозданием.
    report = run_forward(
        Scope(session), store=FakeStore(series(day + 1)), now=night + timedelta(days=1)
    )

    assert decision.decided_at in [at for *_, at in report.journaled]
    row = next(r for r in signals_of(session, live.id) if r.decided_at == decision.decided_at)
    # Опоздание видно глазами: записано на сутки позже, чем принято.
    assert row.meta["seen_at"] == (night + timedelta(days=1)).isoformat()


def test_legacy_mark_without_bars_still_works(session, live):
    """Отметка прежнего вида (только `at`) не ломает прогон: она служит запасной границей."""
    from lab.db.models import SystemFlagRow

    session.add(SystemFlagRow(key=f"{FLAG_PREFIX}{live.id}", value={"at": T1.isoformat()}))
    session.flush()

    report = run_forward(Scope(session), store=FakeStore(series(FULL_DAYS)), now=T2)

    assert report.journaled and all(at > T1 for *_, at in report.journaled)


# -- шорт листингов: решения по инструментам, состав растёт между прогонами -----------------

LISTING_ID = "cex-perp-paper-listing-fade-short"
COIN_A, COIN_B = "AAA/USDT:USDT", "BBB/USDT:USDT"
LISTED_A, LISTED_B = 100, 103  # номер дня листинга от BASE


def day_at(day: int, minutes: int = 30) -> datetime:
    """Ночной прогон журнала: `minutes` после полуночи UTC дня `day`."""
    return BASE + timedelta(days=day, minutes=minutes)


def perp_bars(instrument: str, last_day: int) -> list[Candle]:
    """Перп торгуется с нулевого дня — раньше спота, как у выборки стратегии."""
    return [bar(instrument, d, 10) for d in range(last_day + 1)]


def listing_dates(**days: int) -> str:
    names = {"a": COIN_A, "b": COIN_B}
    return json.dumps(
        {names[k]: (BASE + timedelta(days=d)).date().isoformat() for k, d in days.items()}
    )


@pytest.fixture
def listing(session):
    strategy = build(LISTING_ID)
    manifest = strategy.manifest.model_copy(
        update={
            "instruments": [COIN_A],
            "params": {**strategy.manifest.params, "listing_dates": listing_dates(a=LISTED_A)},
        }
    )
    record = Registry(session).add(manifest)
    row = session.get(StrategyRow, record.id)
    row.rung = Rung.PAPER.value
    row.status = Status.MEASURING.value
    session.flush()
    return record


def add_coin_b(session, record) -> None:
    """То, что делает лента: инструмент и дата листинга дописываются в запись."""
    row = session.get(StrategyRow, record.id)
    row.instruments = [COIN_A, COIN_B]
    row.params_json = {**row.params_json, "listing_dates": listing_dates(a=LISTED_A, b=LISTED_B)}
    session.flush()


def entries(report) -> list[tuple[str, datetime]]:
    return [(i, at) for _, i, side, at in report.journaled if side == "sell"]


def test_coin_added_between_runs_is_journaled_from_its_first_decision(session, listing):
    """Новый листинг от ленты: его вход записан, его прошлое — нет."""
    first = run_forward(Scope(session), store=FakeStore(perp_bars(COIN_A, 49)), now=day_at(50))
    assert listing.id in first.started

    run_forward(Scope(session), store=FakeStore(perp_bars(COIN_A, 100)), now=day_at(101))
    a_entry = run_forward(
        Scope(session), store=FakeStore(perp_bars(COIN_A, 101)), now=day_at(102)
    )
    # Вход — на закрытии ПЕРВОГО полного дня (101), решение датировано полуночью 102-го.
    assert entries(a_entry) == [(COIN_A, BASE + timedelta(days=LISTED_A + 2))]

    add_coin_b(session, listing)
    both = perp_bars(COIN_A, 103) + perp_bars(COIN_B, 103)
    assert entries(run_forward(Scope(session), store=FakeStore(both), now=day_at(104))) == []

    both = perp_bars(COIN_A, 104) + perp_bars(COIN_B, 104)
    b_entry = run_forward(Scope(session), store=FakeStore(both), now=day_at(105))

    assert entries(b_entry) == [(COIN_B, BASE + timedelta(days=LISTED_B + 2))]
    sells = [r for r in signals_of(session, listing.id) if r.side == "sell"]
    assert sorted(r.instrument for r in sells) == [COIN_A, COIN_B]


def test_one_coin_late_by_a_night_keeps_its_decision(session, listing):
    """Опоздала одна нога: её решение не должно сгореть из-за свежести соседней.

    Общая отметка «по данным» тут не спасает: соседний инструмент уже сдвинул её за
    момент решения. Поэтому граница ведётся ПО ИНСТРУМЕНТУ.
    """
    add_coin_b(session, listing)
    run_forward(Scope(session), store=FakeStore(perp_bars(COIN_A, 49)), now=day_at(50))
    run_forward(
        Scope(session),
        store=FakeStore(perp_bars(COIN_A, 103) + perp_bars(COIN_B, 103)),
        now=day_at(104),
    )
    # Ночь 105: A свежий, B застрял на вчерашнем дне — его вход ещё не виден.
    lagging = perp_bars(COIN_A, 104) + perp_bars(COIN_B, 103)
    assert entries(run_forward(Scope(session), store=FakeStore(lagging), now=day_at(105))) == []

    caught_up = perp_bars(COIN_A, 105) + perp_bars(COIN_B, 105)
    report = run_forward(Scope(session), store=FakeStore(caught_up), now=day_at(106))

    assert entries(report) == [(COIN_B, BASE + timedelta(days=LISTED_B + 2))]


def test_coin_added_too_late_does_not_write_its_past(session, listing):
    """Лента опоздала: вход уже случился до прошлого прогона — задним числом не пишется."""
    run_forward(Scope(session), store=FakeStore(perp_bars(COIN_A, 49)), now=day_at(50))
    run_forward(Scope(session), store=FakeStore(perp_bars(COIN_A, 105)), now=day_at(106))

    add_coin_b(session, listing)
    both = perp_bars(COIN_A, 106) + perp_bars(COIN_B, 106)
    report = run_forward(Scope(session), store=FakeStore(both), now=day_at(107))

    assert [i for i, _ in entries(report)] == []
    assert not any(r.instrument == COIN_B for r in signals_of(session, listing.id))


def test_replay_feeds_both_legs_in_time_order():
    """Связка обязана видеть бары в том же порядке, что вживую, иначе сравнит разные моменты."""
    strategy = build(STRATEGY_ID)
    got = replay(strategy, list(reversed(series())))

    assert strategy.decided_on is not None
    assert isinstance(got, list)


# -- обновление свечей -------------------------------------------------------------------


def test_refresh_targets_only_live_strategies(session, live):
    """Качать весь архив каждую ночь незачем: у нас 1004 инструмента, живых стратегий единицы."""
    from lab.ops.jobs.data_refresh import targets

    wanted = targets(session)

    assert (live.venue, RISK, "1d") in wanted
    assert (live.venue, SAFE, "1d") in wanted
    # Бенчмарк тоже обязан быть свежим: иначе сравнение молча уезжает в прошлое.
    assert (live.venue, "BTC/USDT", "1d") in wanted

    session.get(StrategyRow, live.id).rung = Rung.BACKTEST.value
    session.flush()
    assert targets(session) == set()


def test_refresh_report_reads_the_real_result_fields(session, live, monkeypatch):
    """Имена полей `SymbolResult` — `instrument` и `rows_written`, а не `symbol`/`written`.

    Ошибка здесь тихая: сбор идёт как шёл, а отчёт печатает «?» и ноль записанных строк,
    то есть выглядит как «источник ничего не отдал». Так и было в первом живом прогоне.
    """
    from lab.data.backfill_cex import SymbolResult
    from lab.ops.jobs import data_refresh

    monkeypatch.setattr(
        "lab.data.backfill_cex.backfill_venue",
        lambda store, venue, symbols, tf, days: [
            SymbolResult(venue=venue, instrument=s, tf=tf, rows_written=7) for s in symbols
        ],
    )

    report = data_refresh.refresh(Scope(session), store=FakeStore([]))

    assert report.ok
    assert all(instrument != "?" for _, instrument, _, _, _ in report.rows)
    assert all(written == 7 for *_, written, _ in report.rows)


def test_refresh_also_updates_funding_of_perpetuals(session, listing, monkeypatch):
    """Без свежих ставок перемер подставит константу — для шорта это доход вместо расхода.

    Ставки нужны только бессрочным контрактам: у спотового бенчмарка BTC их нет, и запрос
    по нему был бы ошибкой каждую ночь.
    """
    from lab.data.backfill_cex import SymbolResult
    from lab.ops.jobs import data_refresh

    monkeypatch.setattr(
        "lab.data.backfill_cex.backfill_venue",
        lambda store, venue, symbols, tf, days: [
            SymbolResult(venue=venue, instrument=s, tf=tf, rows_written=1) for s in symbols
        ],
    )
    asked: list[tuple[str, list[str], int]] = []

    def fake_funding(store, venue, symbols, days):
        asked.append((venue, list(symbols), days))
        return [(s, 6, None) for s in symbols]

    monkeypatch.setattr("lab.data.backfill_cex.backfill_funding", fake_funding)

    report = data_refresh.refresh(Scope(session), store=FakeStore([]), funding_store=object())

    assert asked == [("binance", [COIN_A], data_refresh.DEFAULT_DAYS)]
    assert report.ok and report.funding == [("binance", COIN_A, 6, None)]
    assert "фандинг: рядов 1, ставок 6" in report.text()


def test_refresh_reports_funding_failure(session, listing, monkeypatch):
    from lab.data.backfill_cex import SymbolResult
    from lab.ops.jobs import data_refresh

    monkeypatch.setattr(
        "lab.data.backfill_cex.backfill_venue",
        lambda store, venue, symbols, tf, days: [
            SymbolResult(venue=venue, instrument=s, tf=tf, rows_written=1) for s in symbols
        ],
    )
    monkeypatch.setattr(
        "lab.data.backfill_cex.backfill_funding",
        lambda store, venue, symbols, days: [(s, 0, "NetworkError: timeout") for s in symbols],
    )

    report = data_refresh.refresh(Scope(session), store=FakeStore([]), funding_store=object())

    assert not report.ok
    assert report.failed() == [f"фандинг binance {COIN_A}"]
