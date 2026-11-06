"""Fresh public data → frozen input bars → signals → persistent paper fills.

The archive warms up rules and later checks inputs; it never supplies execution prices.
Failures and open lots survive restarts in the database. No trading transport is used.
"""

from copy import deepcopy
from datetime import datetime

from sqlalchemy import select

from lab.contracts import Candle, parse_tf
from lab.core.forward import current_observation
from lab.core.journal import Journal
from lab.core.registry import Registry
from lab.db.base import utcnow
from lab.db.models import SignalRow, SystemFlagRow, VirtualBarRow
from lab.executors.virtual import VirtualExecutor
from lab.ops.jobs.data_refresh import LIVE_STATUSES
from lab.ops.jobs.forward import WARMUP_BARS, ForwardReport, _horizon, replay
from lab.ops.measure import _build_strategy

JOB_ID = "virtual_forward"
PREFIX = "virtual_forward:"


def run_virtual(session_scope, *, store=None, feed_factory=None, clock=utcnow):
    from lab.data import CandleStore
    from lab.feeds.cex.feed import make_feed
    from lab.ops.measure import data_root

    store = store if store is not None else CandleStore(data_root())
    feed_factory = feed_factory or make_feed
    report = ForwardReport()
    with session_scope() as session:
        for record in Registry(session).list():
            if record.rung != "paper" or record.status not in LIVE_STATUSES:
                continue
            observation = current_observation(session, record.id, start=True)
            key = f"{PREFIX}{record.id}"
            flag = session.get(SystemFlagRow, key)
            if flag is None:
                flag = SystemFlagRow(key=key, value={}, updated_by="worker")
                session.add(flag)
            state = dict(flag.value or {})
            if state.get("observation_id") != observation.id:
                state = {"observation_id": observation.id}
            try:
                previous = deepcopy(state)
                journaled_before = len(report.journaled)
                started_before = len(report.started)
                with session.begin_nested():
                    _collect(
                        session, record, observation, state, store, feed_factory, clock, report,
                    )
                state["error"] = None
            except Exception as err:  # noqa: BLE001 — one failed feed must not stop other rules
                state = previous
                del report.journaled[journaled_before:]
                del report.started[started_before:]
                state["error"] = str(err)
                report.skipped.append((record.id, str(err)))
            state["checked_at"] = clock().isoformat()
            # Open lots are deliberately kept after a version change or a data outage.
            lots = [t for t in Journal(session).open_trades(record.id) if t.mode == "paper"]
            state["open_positions"] = [t.model_dump(mode="json") for t in lots]
            flag.value = state
            flag.updated_by = "worker"
    return report


def _collect(session, record, observation, state, store, feed_factory, clock, report):
    at = clock()
    tf = record.timeframe or "1h"
    step = parse_tf(tf)
    if step is None:
        raise ValueError("unsupported timeframe")
    strategy = _build_strategy(record)
    if strategy is None:
        raise ValueError("no strategy code")
    feed = feed_factory(record.venue)
    if feed.venue != record.venue:
        raise ValueError("feed venue mismatch")
    saved = list(session.scalars(select(VirtualBarRow).where(
        VirtualBarRow.observation_id == observation.id,
    )).all())
    start = min([at - step * WARMUP_BARS, *(r.ts for r in saved)])
    # A broken archive must be an explicit failure, not an empty warmup.
    archive = [b for instrument in record.instruments for b in store.read(
        record.venue, instrument, tf, start, at,
    ) if b.ts + step <= at]
    archived = {(b.instrument, b.ts): b for b in archive}
    archive_horizon = _horizon(archive)
    for row in saved:
        bar = archived.get((row.instrument, row.ts))
        if bar is not None:
            if row.archive_status != "mismatch":
                matches = bar == Candle.model_validate(row.payload)
                row.archive_status = "matched" if matches else "mismatch"
        elif (row.archive_status != "mismatch"
              and row.ts + step < archive_horizon.get(row.instrument, row.ts)):
            row.archive_status = "missing"
    state["archive_pending"] = sum(r.archive_status == "pending" for r in saved)
    state["archive_mismatches"] = sum(r.archive_status == "mismatch" for r in saved)
    state["archive_missing"] = sum(r.archive_status == "missing" for r in saved)
    if state["archive_mismatches"] or state["archive_missing"]:
        # Preserve the mismatch audit even though execution is stopped.
        state["blocked"] = "archive_mismatch"
        report.skipped.append((record.id, "archive_mismatch"))
        return
    state.pop("blocked", None)
    merged = dict(archived)
    # Replays always use the input actually consumed, even if archives later change.
    merged.update({(r.instrument, r.ts): Candle.model_validate(r.payload) for r in saved})
    known = {i: utc for i, utc in _horizon(list(merged.values())).items()}
    for instrument in record.instruments:
        if not instrument or instrument == "*":
            raise ValueError("explicit instruments required")
        start = known.get(instrument, at - step * 2)
        received = list(feed.candles(instrument, tf, start - step, at))
        closed = sorted((b for b in received if b.ts + step <= at), key=lambda b: b.ts)
        if (not closed or closed[-1].ts + step < at - step
                or any(b.instrument != instrument or b.tf != tf for b in closed)):
            raise ValueError(f"missing or stale candles: {instrument}")
        for bar in closed:
            if (bar.close <= 0 or bar.volume <= 0 or bar.low > min(bar.open, bar.close)
                    or bar.high < max(bar.open, bar.close) or bar.low > bar.high):
                raise ValueError(f"invalid candle: {instrument}")
            existing = merged.get((instrument, bar.ts))
            if existing is not None and existing != bar:
                raise ValueError(f"live/archive candle mismatch: {instrument} {bar.ts}")
            merged.setdefault((instrument, bar.ts), bar)
        times = sorted(t for i, t in merged if i == instrument)
        if any(b - a != step for a, b in zip(times, times[1:], strict=False)):
            raise ValueError(f"candle gap: {instrument}")
    signals = replay(strategy, list(merged.values()), strict=True)
    marks = {i: max((b for b in merged.values() if b.instrument == i), key=lambda b: b.ts).close
             for i in record.instruments}
    if "started_at" not in state:
        state["started_at"] = at.isoformat()
        report.started.append(record.id)
        # Freeze initial horizon too: the next run must not journal warmup decisions.
        state["horizon"] = {i: t.isoformat() for i, t in _horizon(list(merged.values())).items()}
    else:
        horizon = {i: datetime.fromisoformat(t) for i, t in state["horizon"].items()}
        journal = Journal(session)
        for signal in signals:
            if signal.decided_at <= horizon.get(signal.instrument, at):
                continue
            if signal.strategy_id != record.id:
                raise ValueError("signal strategy mismatch")
            signal.meta.update(source="virtual_forward", seen_at=clock().isoformat())
            recorded = journal.record_signal(signal)
            try:
                with session.begin_nested():
                    VirtualExecutor(session, feed, clock=clock).execute(recorded.id)
            except Exception as err:  # noqa: BLE001 — preserve decision after book/network failure
                # Keep the decision, but never pretend a missing fill was executed.
                signal_row = session.get_one(SignalRow, recorded.id)
                signal_row.meta = {**signal_row.meta, "virtual_error": str(err)}
                journal.set_outcome(recorded.id, "skipped", clock())
                state["execution_errors"] = state.get("execution_errors", 0) + 1
                report.skipped.append((record.id, str(err)))
            report.journaled.append((record.id, signal.instrument, signal.side, signal.decided_at))
        state["horizon"] = {i: t.isoformat() for i, t in _horizon(list(merged.values())).items()}
    identities = {(r.instrument, r.ts) for r in saved}
    # Include archive-delivered new bars too: every input used for a new decision is frozen.
    fresh = list(merged.values())
    for bar in fresh:
        if (bar.instrument, bar.ts) not in identities:
            session.add(VirtualBarRow(
                observation_id=observation.id, instrument=bar.instrument, ts=bar.ts,
                payload=bar.model_dump(mode="json"), recorded_at=clock(),
                archive_status="matched" if (bar.instrument, bar.ts) in archived else "pending",
            ))
            identities.add((bar.instrument, bar.ts))
            if (bar.instrument, bar.ts) not in archived:
                state["archive_pending"] += 1
    # Mark the final portfolio, including fills created in this tick.
    lots = [t for t in Journal(session).open_trades(record.id) if t.mode == "paper"]
    state["unpriced_positions"] = [t.id for t in lots if t.instrument not in marks]
    state["unrealized_pnl"] = (None if state["unpriced_positions"] else str(sum(
        ((marks[t.instrument] - t.entry_price) * t.qty * (1 if t.side == "long" else -1)
         - t.costs.total for t in lots), start=0,
    )))
    state["marks_at"] = at.isoformat()


def virtual_job(session_scope, *, store=None, feed_factory=None, alert=None):
    from lab.ops.scheduler import Job

    def run():
        report = run_virtual(session_scope, store=store, feed_factory=feed_factory)
        if report.skipped and alert:
            alert("alert", {"service": JOB_ID, "detail": str(report.skipped[:5]),
                            "at": utcnow().isoformat()})
        return report

    return Job(id=JOB_ID, func=run,
               description="Свежие данные → виртуальные сделки и сверка архива")
