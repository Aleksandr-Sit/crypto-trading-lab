"""Search attempts persist independently of candidate lifecycle and snapshot saving."""

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from lab.core.measure.research import begin, finish, variant_count
from lab.db.models import ResearchTrialRow


def test_repeats_and_failures_remain_in_ledger_without_resetting_search(session):
    a = begin(session, "candidate-a", {"rules": "v1", "window": "one"})
    finish(session, a["trial_id"], "failed", "no data")
    b = begin(session, "candidate-a", {"rules": "v1", "window": "one"})
    assert b["variants"] == 1
    c = begin(session, "candidate-b", {"rules": "v2", "window": "one"})
    assert c["variants"] == 2
    assert variant_count(session) == 2
    rows = list(session.scalars(select(ResearchTrialRow).order_by(ResearchTrialRow.id)))
    assert len(rows) == 3
    assert rows[0].status == "failed" and rows[0].reason == "no data"
    assert rows[1].status == "running"  # interrupted attempts are not silently erased


def test_new_search_count_invalidates_old_admission(session):
    from lab.core.forward import current_observation
    from lab.core.ladder import Ladder
    from lab.core.registry import Registry
    from lab.db.models import StrategyRow
    from tests.core.test_ladder import FakeThreshold, make_metrics, manifest

    strategy = Registry(session).add(manifest())
    row = session.get(StrategyRow, strategy.id)
    row.rung = "semi"
    observation = current_observation(session, row.id, start=True)
    evidence = begin(session, row.id, {"rules": "v1"})
    finish(session, evidence["trial_id"], "ok")
    m = make_metrics().model_copy(update={
        "research": evidence, "forward_verified": True, "trade_source": "journal_live",
        "observation_id": observation.id, "rules_revision": observation.revision,
        "observation_started_at": observation.started_at,
        "window_to": observation.started_at + timedelta(days=1),
    })
    ladder = Ladder(session, threshold=FakeThreshold())
    assert ladder._admission(row, m, "semi").status == "passed"
    begin(session, "other-strategy", {"rules": "v2"})
    result = ladder._admission(row, m, "semi")
    assert result.status == "insufficient"
    assert any(c.name == "research_current" for c in result.criteria)


def test_research_cli_exposes_failed_attempts_and_global_count(session, monkeypatch, capsys):
    import lab.cli as cli

    evidence = begin(session, "rejected-candidate", {"rules": "v1"})
    finish(session, evidence["trial_id"], "failed", "no candles")

    @contextmanager
    def scope():
        yield session

    monkeypatch.setattr(cli, "_scope", lambda: scope)
    assert cli.cmd_measure_trials(SimpleNamespace(strategy_id=None, limit=20)) == 0
    output = capsys.readouterr().out
    assert "rejected-candidate" in output and "no candles" in output
    assert "журнале: 1" in output


def test_stability_refreshes_interval_for_trials_added_during_rolling(session):
    from lab.core.measure import metrics
    from lab.core.measure.metrics import refresh_search_interval
    from tests.core.test_measure_threshold import WINDOW, _trade

    trades = [_trade(i, "5" if i < 20 else "-3") for i in range(30)]
    m = metrics(trades, None, capital=Decimal(1000), window=WINDOW,
                extra={"research": {"tracked": True, "variants": 1}})
    updated = refresh_search_interval(m, trades, 5)
    assert updated.research["variants"] == 5
    assert updated.ev_ci95 == m.ev_ci95
    assert updated.ev_search_ci[0] <= m.ev_search_ci[0]
