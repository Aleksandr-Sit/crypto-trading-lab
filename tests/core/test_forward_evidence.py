"""Forward evidence must survive replay, version changes and delayed journal writes."""

from datetime import UTC, datetime, timedelta

import pytest

from lab.core.forward import current_observation, source_hash
from lab.core.journal import Journal
from lab.core.ladder import Ladder, LadderError
from lab.core.registry import Registry
from lab.db.models import FillRow, OrderRow, SignalRow, StrategyRow
from lab.ops.measure import _journal_trades
from tests.core.test_journal import _fill, _intent, _signal
from tests.core.test_ladder import FakeThreshold, make_metrics, manifest

T = datetime(2026, 11, 1, tzinfo=UTC)
WINDOW = (T - timedelta(hours=1), T + timedelta(days=1))


@pytest.fixture
def evidence(session, monkeypatch):
    spec = manifest()
    spec.update(venue="bybit", instruments=["BTC/USDT"])
    sid = Registry(session).add(spec).id
    session.get(StrategyRow, sid).rung = "paper"
    session.get(StrategyRow, sid).status = "measuring"
    observation = current_observation(session, sid, start=True, at=T - timedelta(minutes=1))
    j = Journal(session)
    for index, side in enumerate(("buy", "sell")):
        at = T + timedelta(hours=index)
        monkeypatch.setattr("lab.core.journal.utcnow", lambda at=at: at + timedelta(seconds=2))
        sig = j.record_signal(_signal(sid, side, "1", at), signal_id=f"s{index}")
        j.record_order(_intent(sid, sig.id, side, "1", f"c{index}"), order_id=f"o{index}")
        session.get(OrderRow, f"o{index}").created_at = at + timedelta(seconds=3)
        monkeypatch.setattr("lab.core.journal.utcnow", lambda at=at: at + timedelta(seconds=5))
        j.record_fill(_fill(f"f{index}", f"o{index}", str(100 + index), "1", "0",
                            at + timedelta(seconds=4)))
    session.flush()
    return sid, observation


def selected(session, evidence):
    sid, observation = evidence
    return _journal_trades(session, sid, WINDOW, observation=observation)


def test_timely_trade_is_selected_and_journal_timestamps_are_independent(session, evidence):
    assert len(selected(session, evidence)) == 1
    signal = session.get(SignalRow, "s0")
    assert signal.recorded_at == T + timedelta(seconds=2)
    assert signal.observation_id == evidence[1].id
    assert session.get(FillRow, "f0").recorded_at == T + timedelta(seconds=5)


@pytest.mark.parametrize("leg", [0, 1])
@pytest.mark.parametrize("defect", [
    "legacy_signal", "legacy_fill", "late_signal", "late_fill", "future_signal",
    "future_fill", "order_before_signal", "fill_before_order", "wrong_observation",
    "wrong_mode", "expired_signal",
])
def test_either_bad_leg_excludes_trade_without_deleting_it(session, evidence, leg, defect):
    signal = session.get(SignalRow, f"s{leg}")
    order = session.get(OrderRow, f"o{leg}")
    fill = session.get(FillRow, f"f{leg}")
    if defect == "legacy_signal":
        signal.recorded_at = None
    elif defect == "legacy_fill":
        fill.recorded_at = None
    elif defect == "late_signal":
        signal.decided_at -= timedelta(seconds=61)
    elif defect == "late_fill":
        fill.recorded_at += timedelta(seconds=301)
    elif defect == "future_signal":
        signal.decided_at = signal.recorded_at + timedelta(seconds=1)
    elif defect == "future_fill":
        fill.ts = fill.recorded_at + timedelta(seconds=1)
    elif defect == "order_before_signal":
        order.created_at = signal.recorded_at - timedelta(seconds=1)
    elif defect == "fill_before_order":
        fill.ts = order.created_at - timedelta(seconds=1)
    elif defect == "wrong_observation":
        signal.observation_id = None
    elif defect == "wrong_mode":
        order.mode = "live"
    elif defect == "expired_signal":
        signal.ttl = 1
    session.flush()
    assert selected(session, evidence) == []
    assert len(Journal(session).closed_trades(evidence[0])) == 1


def test_start_and_measurement_window_both_exclude_earlier_entries(session, evidence):
    observation = evidence[1]
    observation.started_at = T + timedelta(seconds=1)
    assert selected(session, evidence) == []
    observation.started_at = T - timedelta(minutes=1)
    assert _journal_trades(
        session, evidence[0], (T + timedelta(minutes=1), WINDOW[1]), observation=observation
    ) == []


def test_changed_params_start_new_observation_and_reverting_does_not_reuse_old_run(
    session, evidence
):
    sid, old = evidence
    row = session.get(StrategyRow, sid)
    original = dict(row.params_json)
    row.params_json = {**original, "period": 10}
    assert current_observation(session, sid) is None
    changed = current_observation(session, sid, start=True)
    assert changed.id != old.id and changed.revision != old.revision
    row.params_json = original
    reverted = current_observation(session, sid, start=True)
    assert reverted.revision == old.revision and reverted.id not in (old.id, changed.id)
    assert _journal_trades(session, sid, WINDOW, observation=reverted) == []


@pytest.mark.parametrize("method", ["evaluate", "promote"])
def test_stale_measurement_cannot_promote_even_when_custom_threshold_passes(
    session, evidence, method
):
    sid, observation = evidence
    metrics = make_metrics().model_copy(update={
        "forward_verified": True, "trade_source": "journal_paper",
        "observation_id": observation.id, "rules_revision": observation.revision,
        "observation_started_at": observation.started_at, "window_to": WINDOW[1],
    })
    ladder = Ladder(session, threshold=FakeThreshold(), metrics=lambda *_: metrics)
    session.get(StrategyRow, sid).params_json = {"period": 99}
    if method == "evaluate":
        assert ladder.evaluate(sid, metrics) is None
    else:
        with pytest.raises(LadderError, match="forward_evidence"):
            ladder.promote(sid, by="operator")
    assert Registry(session).get(sid).rung == "paper"
    assert ladder.history(sid) == []


def test_current_observation_allows_promotion(session, evidence):
    sid, observation = evidence
    metrics = make_metrics().model_copy(update={
        "forward_verified": True, "trade_source": "journal_paper",
        "observation_id": observation.id, "rules_revision": observation.revision,
        "observation_started_at": observation.started_at, "window_to": WINDOW[1],
    })
    ladder = Ladder(session, threshold=FakeThreshold(), metrics=lambda *_: metrics)
    assert ladder.promote(sid, by="operator").to_rung == "micro"


def test_hash_detects_dirty_code_and_normalizes_checkout_newlines(tmp_path):
    path = tmp_path / "rules.py"
    path.write_bytes(b"period = 10\r\n")
    first = source_hash(tmp_path)
    path.write_bytes(b"period = 10\n")
    assert source_hash(tmp_path) == first
    path.write_bytes(b"period = 20\n")
    assert source_hash(tmp_path) != first


def test_source_change_invalidates_observation_without_git_commit(session, evidence, monkeypatch):
    sid, observation = evidence
    monkeypatch.setattr("lab.core.forward.source_hash", lambda _: "changed")
    assert current_observation(session, sid) is None
    assert current_observation(session, sid, start=True).revision != observation.revision


def test_measure_wrapper_keeps_only_verified_trades_and_persists_evidence(session, evidence):
    from contextlib import contextmanager

    from lab.core.measure import history
    from lab.ops.measure import run_measure

    @contextmanager
    def scope():
        yield session

    sid, observation = evidence
    result = run_measure(sid, "paper", WINDOW, session_scope=scope, store=object())
    assert result.metrics.n_trades == 1
    assert result.metrics.forward_verified
    assert result.metrics.observation_id == observation.id
    assert result.metrics.rules_revision == observation.revision
    assert history(session, sid)[0].metrics.observation_id == observation.id

    session.get(FillRow, "f1").recorded_at = None
    result = run_measure(sid, "paper", WINDOW, session_scope=scope, store=object())
    assert result.metrics.n_trades == 0
    assert result.metrics.forward_excluded == {"unverified_signal_or_execution": 1}
    assert result.threshold.status == "insufficient"


@pytest.mark.parametrize("override", ["trades", "extra_metrics", "capital", "params"])
def test_wrapper_cannot_replace_frozen_evidence(session, evidence, override):
    from lab.ops.measure import run_measure

    with pytest.raises(ValueError, match="реестра"):
        run_measure(evidence[0], "paper", WINDOW, session_scope=lambda: None, **{override: {}})


def test_backtest_snapshot_without_current_revision_cannot_promote(session):
    sid = Registry(session).add(manifest()).id
    ladder = Ladder(session, threshold=FakeThreshold(), metrics=lambda *_: make_metrics())
    assert ladder.evaluate(sid) is None
    with pytest.raises(LadderError, match="rules_revision"):
        ladder.promote(sid, by="operator")
    assert Registry(session).get(sid).rung == "backtest"
