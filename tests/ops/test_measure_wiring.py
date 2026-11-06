"""Связка замера (R11, R12, R14): систему никто не просит мерить руками —
`worker`, кнопка «В замер» и CLI зовут `core.measure.run` через одну обёртку.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from lab.core.registry import Registry
from lab.data import CandleStore
from lab.ops.measure import MeasureUnavailable, make_measure
from lab.strategies import registry as code_registry
from tests.fixtures.synthetic import synthetic_candles

STRATEGY_ID = "cex-spot-indicator-pifagor-forever-sma-v0"
NOW = datetime(2026, 9, 7, tzinfo=UTC)
WINDOW = (NOW - timedelta(days=400), NOW)


@pytest.fixture
def scope(session):
    @contextmanager
    def _scope():
        yield session

    return _scope


def _store(tmp_path, instruments=("BTC/USDT",)):
    store = CandleStore(tmp_path / "data")
    for instrument in instruments:
        candles = synthetic_candles(
            420,
            "trend",
            drift_pct="0.3",
            tf="1d",
            start=WINDOW[0] - timedelta(days=20),
            instrument=instrument,
        )
        store.write("binance", instrument, "1d", candles)
    return store


def _register(session):
    manifest = code_registry.manifest(STRATEGY_ID)
    return Registry(session).add(manifest)


def test_measure_wrapper_runs_core_measure_from_registry_and_store(session, scope, tmp_path):
    """Обёртка сама достаёт стратегию из реестра кода и свечи из хранилища."""
    _register(session)
    measure = make_measure(scope, store=_store(tmp_path))

    m = measure(strategy_id=STRATEGY_ID, mode="backtest", window=WINDOW)

    assert m.strategy_id == STRATEGY_ID
    assert m.status == "ok", m.reason
    assert m.metrics is not None
    assert m.data_hash and m.code_version
    assert m.metrics.research["variants"] == 1
    assert m.metrics.research["tracked"]


def test_measure_wrapper_reports_missing_candles_as_incomplete(session, scope, tmp_path):
    """Нет свечей и нет фида — снимок `incomplete` с причиной, а не выдуманные цифры."""
    _register(session)
    measure = make_measure(
        scope, store=CandleStore(tmp_path / "empty"), feed_factory=lambda venue: None
    )

    m = measure(strategy_id=STRATEGY_ID, mode="backtest", window=WINDOW)

    assert m.status == "incomplete"
    assert m.metrics is None
    assert "свеч" in m.reason or "данн" in m.reason
    from sqlalchemy import select

    from lab.db.models import ResearchTrialRow

    assert session.scalar(select(ResearchTrialRow)).status == "incomplete"


def test_unsaved_measurements_are_still_research_attempts(session, scope, tmp_path):
    from sqlalchemy import func, select

    from lab.db.models import MeasurementRow, ResearchTrialRow

    _register(session)
    measure = make_measure(scope, store=_store(tmp_path))
    a = measure(strategy_id=STRATEGY_ID, mode="backtest", window=WINDOW, session=None)
    b = measure(strategy_id=STRATEGY_ID, mode="backtest", window=WINDOW, session=None)
    assert a.id is None and b.id is None
    assert a.metrics.research["variants"] == b.metrics.research["variants"] == 1
    assert session.scalar(select(func.count()).select_from(MeasurementRow)) == 0
    assert session.scalar(select(func.count()).select_from(ResearchTrialRow)) == 2
    assert session.scalar(select(ResearchTrialRow).limit(1)).result["n_trades"] is not None


def test_measure_wrapper_names_the_strategy_it_cannot_build(session, scope, tmp_path):
    """Стратегии нет в коде и ветка бэктестится — честный отказ с именем, а не молчание."""
    from lab.contracts import Branch, StopSpec, StrategyManifest

    manifest = StrategyManifest(
        slug="no-such-code",
        branch=Branch.CEX_SPOT,
        venue="binance",
        source_kind="indicator",
        source_ref="https://example.invalid",
        instruments=["BTC/USDT"],
        timeframe="1d",
        params={},
        can_backtest=True,
        stop=StopSpec(max_dd_pct=25),
        description="нет кода",
    )
    strategy = Registry(session).add(manifest)
    measure = make_measure(scope, store=_store(tmp_path))

    with pytest.raises(MeasureUnavailable, match=strategy.id):
        measure(strategy_id=strategy.id, mode="backtest", window=WINDOW)
    from sqlalchemy import select

    from lab.db.models import ResearchTrialRow

    trial = session.scalar(select(ResearchTrialRow))
    assert trial.status == "failed" and "MeasureUnavailable" in trial.reason


def test_worker_registers_remeasure_job(session, scope, tmp_path):
    """Без `measure=` задание `remeasure` не появляется — центральное требование не исполняется."""
    from lab.ops.worker import Worker

    worker = Worker(scope, executors={})
    ids = [job.id for job in worker.jobs()]

    assert "remeasure" in ids
    assert "discovery" in ids and "expiry" in ids


def test_worker_measure_is_the_same_wrapper(session, scope, tmp_path):
    """`Worker.measure` — та же обёртка: зовёт `core.measure.run` и пишет снимок."""
    from lab.ops.worker import Worker

    _register(session)
    worker = Worker(scope, executors={}, store=_store(tmp_path))

    m = worker.measure(strategy_id=STRATEGY_ID, mode="backtest", window=WINDOW)

    assert m.strategy_id == STRATEGY_ID and m.status == "ok", m.reason


def test_candidate_button_measures_and_reports_the_result(session, scope, tmp_path):
    """Кнопка «В замер»: кандидат заводится, замер идёт, в карточку возвращается результат."""
    from lab.discovery import candidate_hook
    from lab.ops.measure import make_measure

    candidate = Registry(session).enqueue_candidate(
        "wallet", "0xabc", {"chain": "solana", "venue": "solana", "branch": "meme"}
    )
    calls: list[dict] = []
    measure = make_measure(scope, store=_store(tmp_path))

    def spy(**kw):
        calls.append(kw)
        return measure(**kw)

    hook = candidate_hook(scope, measure=spy)
    text = hook(str(candidate.id), "accept")

    assert calls and calls[0]["strategy_id"].startswith("copy-")
    assert "замер" in text.lower()


def test_candidate_decision_flags_measured(session, scope, tmp_path):
    from lab.discovery import decide

    candidate = Registry(session).enqueue_candidate(
        "wallet", "0xdef", {"chain": "solana", "venue": "solana", "branch": "meme"}
    )
    result = decide(session, candidate.id, "accept", measure=lambda **kw: "снимок")
    assert result.measured is True and result.strategy_id


def test_candidate_queue_is_ordered_by_measure_cost(session):
    """В6: первым мерим то, что дешевле замерить, а не то, что раньше пришло."""
    reg = Registry(session)
    reg.enqueue_candidate("wallet", "0x1", {"branch": "meme"})  # только форвард, дорого
    reg.enqueue_candidate("repo", "gh/2", {"branch": "cex-spot"})  # бэктест на свечах, дёшево
    reg.enqueue_candidate("repo", "gh/3", {"branch": "dex-perp"})

    order = [c.ref for c in reg.candidates(decision="pending")]

    assert order == ["gh/2", "gh/3", "0x1"]


def test_remeasure_job_measures_live_strategies_end_to_end(session, scope, tmp_path):
    """Задание `remeasure` из планировщика реально мерит: снимок появляется в базе."""
    from lab.contracts import Status
    from lab.core.measure import history
    from lab.db.models import StrategyRow
    from lab.ops.worker import Worker

    _register(session)
    session.get(StrategyRow, STRATEGY_ID).status = Status.MEASURING.value
    session.flush()
    worker = Worker(scope, executors={}, store=_store(tmp_path))
    job = next(j for j in worker.jobs() if j.id == "remeasure")

    report = job.func()

    assert report.measured == [STRATEGY_ID], report.failed
    assert history(session, STRATEGY_ID), "снимок замера не сохранён"


def test_paper_wrapper_does_not_turn_candles_into_paper_trades(session, scope, tmp_path):
    from lab.db.models import StrategyRow

    _register(session)
    session.get(StrategyRow, STRATEGY_ID).rung = "paper"
    session.flush()
    measure = make_measure(scope, store=_store(tmp_path))
    m = measure(strategy_id=STRATEGY_ID, mode="paper", window=WINDOW)
    assert m.status == "ok"
    assert m.metrics.trade_source == "journal_paper"
    assert m.metrics.n_trades == 0
    assert m.threshold.status == "insufficient"


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_journal_trade_selection_separates_execution_modes(monkeypatch, mode):
    from decimal import Decimal
    from types import SimpleNamespace

    from lab.contracts import Costs
    from lab.core.journal import Journal
    from lab.ops.measure import _journal_trades

    def trade(execution_mode, pnl):
        return SimpleNamespace(
            mode=execution_mode, closed_at=NOW, opened_at=NOW - timedelta(hours=1),
            instrument="BTC/USDT", side="long", qty=Decimal(1),
            entry_price=Decimal(100), exit_price=Decimal(100 + pnl),
            pnl_gross=Decimal(pnl), costs=Costs(),
        )

    monkeypatch.setattr(
        Journal, "closed_trades", lambda *_: [trade("paper", 3), trade("live", 7)]
    )
    rows = _journal_trades(None, STRATEGY_ID, WINDOW, mode=mode)
    assert len(rows) == 1
    assert rows[0].pnl_gross == Decimal(3 if mode == "paper" else 7)
