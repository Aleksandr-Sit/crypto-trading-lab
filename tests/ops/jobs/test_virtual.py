from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from lab.contracts import Book, BookLevel, Candle, Signal
from lab.core.forward import current_observation, virtual_health
from lab.core.journal import Journal
from lab.core.registry import Registry
from lab.db.models import FillRow, SignalRow, StrategyRow, SystemFlagRow, VirtualBarRow
from lab.executors.virtual import VirtualExecutor
from lab.ops.jobs.virtual import PREFIX, run_virtual
from lab.strategies.registry import build

ID = "cex-spot-external-rotation-gold-btc"
NOW = datetime(2026, 11, 6, tzinfo=UTC)


def candle(ts, price=100):
    return Candle(instrument="BTC/USDT", tf="1h", ts=ts, open=Decimal(price),
                  high=Decimal(price), low=Decimal(price), close=Decimal(price), volume=Decimal(1))


class Feed:
    venue = "binance"

    def __init__(self, now):
        self.now = now
        self.bars = [candle(now - timedelta(hours=1))]
        self.fail = False

    def candles(self, *args):
        if self.fail:
            raise ConnectionError("feed offline")
        return self.bars

    def book(self, instrument):
        return Book(instrument=instrument, ts=self.now,
                    bids=[BookLevel(price=Decimal(99), qty=Decimal(10))],
                    asks=[BookLevel(price=Decimal(101), qty=Decimal(10))])


class Store:
    def __init__(self):
        self.bars = []

    def read(self, venue, instrument, tf, start, end):
        return [b for b in self.bars if b.instrument == instrument and start <= b.ts < end]

    def count(self, *args):
        return len(self.bars)


@pytest.fixture
def setup(session, monkeypatch):
    record = Registry(session).add(build(ID).manifest)
    row = session.get_one(StrategyRow, record.id)
    row.rung = "paper"
    row.status = "measuring"
    row.venue = "binance"
    row.instruments = ["BTC/USDT"]
    row.timeframe = "1h"
    session.flush()
    clock = [NOW]
    for module in ("lab.core.journal", "lab.core.forward", "lab.db.models"):
        monkeypatch.setattr(f"{module}.utcnow", lambda: clock[0])
    observation = current_observation(session, record.id, start=True, at=NOW - timedelta(days=1))
    return record, observation, clock


def signal(session, side="buy", size="1", at=NOW, **meta):
    return Journal(session).record_signal(Signal(
        strategy_id=ID, decided_at=at, instrument="BTC/USDT", side=side,
        size=Decimal(size), inputs_hash="frozen", ttl_s=60, meta=meta,
    )).id


def test_executor_fifo_restart_and_no_double_slippage(session, setup):
    feed = Feed(NOW)
    buy = signal(session)
    executor = VirtualExecutor(session, feed, clock=lambda: NOW)
    executor.execute(buy)
    VirtualExecutor(session, feed, clock=lambda: NOW).execute(buy)
    assert len(list(session.scalars(select(FillRow)))) == 1
    assert Journal(session).open_trades(ID)[0].entry_price == 101
    sell = signal(session, side="sell", size="2")
    VirtualExecutor(session, feed, clock=lambda: NOW).execute(sell)
    closed = Journal(session).closed_trades(ID)
    assert len(closed) == 1 and closed[0].qty == 1
    assert closed[0].pnl_gross == -2
    assert closed[0].slippage == 0
    assert closed[0].pnl_net < -2
    assert not Journal(session).open_trades(ID)


def test_venue_reconciliation_ignores_virtual_fills(session, setup):
    from lab.ops.jobs.reconcile import reconcile_all

    VirtualExecutor(session, Feed(NOW), clock=lambda: NOW).execute(signal(session))

    class LiveVenue:
        def fills(self, since):
            return []

    reports = reconcile_all(lambda: nullcontext(session), executors={"binance": LiveVenue()},
                            now=NOW, since=NOW - timedelta(hours=1))
    assert len(reports) == 1 and reports[0].ok


@pytest.mark.parametrize("case", ["late", "future", "stale_book", "depth", "limit", "cash"])
def test_executor_rejects_bad_execution(session, setup, case):
    feed = Feed(NOW)
    at = NOW
    size = "1"
    meta = {}
    if case == "late":
        at -= timedelta(seconds=61)
    if case == "future":
        at += timedelta(seconds=1)
    if case == "stale_book":
        feed.now -= timedelta(seconds=11)
    if case == "depth":
        size = "11"
    if case == "limit":
        meta["order_type"] = "limit"
    if case == "cash":
        session.get_one(type(setup[1]), setup[1].id).spec = {
            **setup[1].spec, "measurement_capital": "10",
        }
    sid = signal(session, size=size, at=at, **meta)
    with pytest.raises(ValueError):
        VirtualExecutor(session, feed, clock=lambda: NOW).execute(sid)
    assert not list(session.scalars(select(FillRow)))


class EveryBar:
    strategy_id = ID

    def reset(self):
        pass

    def on_bar(self, bar):
        return [Signal(strategy_id=ID, decided_at=bar.ts + timedelta(hours=1),
                       instrument=bar.instrument, side="buy", size=Decimal(1),
                       inputs_hash=str(bar.close), ttl_s=60)]


def run(session, feed, store, clock):
    return run_virtual(lambda: nullcontext(session), store=store,
                       feed_factory=lambda venue: feed, clock=lambda: clock[0])


def test_collector_warmup_restart_archive_and_open_positions(session, setup, monkeypatch):
    _, observation, clock = setup
    monkeypatch.setattr("lab.ops.jobs.virtual._build_strategy", lambda record: EveryBar())
    feed, store = Feed(NOW), Store()
    assert run(session, feed, store, clock).started == [ID]
    assert not list(session.scalars(select(SignalRow)))
    clock[0] += timedelta(hours=1)
    feed.now = clock[0]
    feed.bars.append(candle(NOW))
    assert len(run(session, feed, store, clock).journaled) == 1
    assert not run(session, feed, store, clock).journaled
    flag = session.get_one(SystemFlagRow, PREFIX + ID)
    assert len(flag.value["open_positions"]) == 1
    snapshots = list(session.scalars(select(VirtualBarRow)))
    assert len(snapshots) == 2 and all(s.archive_status == "pending" for s in snapshots)
    store.bars = list(feed.bars)
    run(session, feed, store, clock)
    assert all(s.archive_status == "matched" for s in snapshots)
    store.bars[-1] = candle(NOW, 90)
    run(session, feed, store, clock)
    changed = next(s for s in snapshots if s.ts == NOW)
    assert changed.archive_status == "mismatch"
    assert flag.value["blocked"] == "archive_mismatch"
    assert virtual_health(session, ID)
    assert Candle.model_validate(changed.payload).close == 100


def test_feed_failure_keeps_horizon_and_positions(session, setup, monkeypatch):
    monkeypatch.setattr("lab.ops.jobs.virtual._build_strategy", lambda record: EveryBar())
    feed, store = Feed(NOW), Store()
    clock = setup[2]
    run(session, feed, store, clock)
    flag = session.get_one(SystemFlagRow, PREFIX + ID)
    horizon = flag.value["horizon"]
    feed.fail = True
    clock[0] += timedelta(hours=1)
    assert run(session, feed, store, clock).skipped
    assert flag.value["horizon"] == horizon
    assert "feed offline" in flag.value["error"]
    assert not list(session.scalars(select(FillRow)))


def test_strategy_exception_does_not_advance_watermark(session, setup, monkeypatch):
    class Broken(EveryBar):
        def on_bar(self, bar):
            raise RuntimeError("broken rule")

    monkeypatch.setattr("lab.ops.jobs.virtual._build_strategy", lambda record: Broken())
    assert run(session, Feed(NOW), Store(), setup[2]).skipped
    flag = session.get_one(SystemFlagRow, PREFIX + ID)
    assert "horizon" not in flag.value


@pytest.mark.parametrize("failure", ["gap", "zero_volume", "archive_error", "future_only"])
def test_data_errors_do_not_create_trades(session, setup, monkeypatch, failure):
    monkeypatch.setattr("lab.ops.jobs.virtual._build_strategy", lambda record: EveryBar())
    feed, store = Feed(NOW), Store()
    clock = setup[2]
    run(session, feed, store, clock)
    if failure == "gap":
        clock[0] += timedelta(hours=2)
        feed.bars.append(candle(NOW + timedelta(hours=1)))
    elif failure == "zero_volume":
        feed.bars = [feed.bars[0].model_copy(update={"volume": Decimal(0)})]
    elif failure == "future_only":
        feed.bars = [candle(NOW + timedelta(hours=1))]
    else:
        def broken(*args):
            raise OSError("archive broken")
        store.read = broken
    assert run(session, feed, store, clock).skipped
    assert not list(session.scalars(select(FillRow)))
    assert virtual_health(session, ID)


def test_book_outage_preserves_decision_and_does_not_retry_past_signal(
    session, setup, monkeypatch,
):
    monkeypatch.setattr("lab.ops.jobs.virtual._build_strategy", lambda record: EveryBar())
    feed, store = Feed(NOW), Store()
    clock = setup[2]
    run(session, feed, store, clock)
    clock[0] += timedelta(hours=1)
    feed.bars.append(candle(NOW))

    def broken(instrument):
        raise ConnectionError("book offline")

    feed.book = broken
    report = run(session, feed, store, clock)
    assert report.skipped and len(report.journaled) == 1
    rows = list(session.scalars(select(SignalRow)))
    assert len(rows) == 1 and rows[0].outcome == "skipped"
    assert rows[0].meta["virtual_error"] == "book offline"
    assert not run(session, feed, store, clock).journaled
    assert not list(session.scalars(select(FillRow)))


def test_version_change_keeps_open_positions(session, setup, monkeypatch):
    monkeypatch.setattr("lab.ops.jobs.virtual._build_strategy", lambda record: EveryBar())
    feed, store = Feed(NOW), Store()
    clock = setup[2]
    run(session, feed, store, clock)
    clock[0] += timedelta(hours=1)
    feed.now = clock[0]
    feed.bars.append(candle(NOW))
    run(session, feed, store, clock)
    row = session.get_one(StrategyRow, ID)
    row.params_json = {**row.params_json, "capital": 9000}
    run(session, feed, store, clock)
    state = session.get_one(SystemFlagRow, PREFIX + ID).value
    assert len(state["open_positions"]) == 1
    assert state["observation_id"] != setup[1].id
    assert "open_positions" in virtual_health(session, ID)


@pytest.mark.parametrize("problem", [
    "archive_pending", "archive_missing", "archive_mismatches", "open_positions",
    "execution_errors", "error", "collector_stale",
])
def test_current_collector_failure_blocks_previously_passing_metrics(session, setup, problem):
    from types import SimpleNamespace

    from lab.contracts import Rung
    from lab.core.ladder import Ladder
    from lab.core.measure.types import SampleStatus, ThresholdResult

    observation = setup[1]
    m = SimpleNamespace(
        forward_verified=True, observation_id=observation.id, rules_revision=observation.revision,
        observation_started_at=observation.started_at, trade_source="journal_paper", window_to=NOW,
    )
    result = ThresholdResult(status="passed", criteria=[], sample=SampleStatus(
        status="ok", n=40, required=30, detail="enough",
    ))
    ladder = Ladder(session, threshold=lambda *args: result)
    row = session.get_one(StrategyRow, ID)
    assert ladder._admission(row, m, Rung.PAPER).status == "passed"
    state = {"checked_at": NOW.isoformat()}
    if problem == "collector_stale":
        state["checked_at"] = (NOW - timedelta(minutes=4)).isoformat()
    else:
        state[problem] = 1
    session.add(SystemFlagRow(key=PREFIX + ID, value=state, updated_by="worker"))
    session.flush()
    assert ladder._admission(row, m, Rung.PAPER).status == "insufficient"
