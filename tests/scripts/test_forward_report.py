import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from lab.contracts import Costs
from lab.core.forward import current_observation
from lab.core.measure.types import ClosedTrade
from lab.core.registry import Registry
from lab.db.models import ForwardObservationRow, OutboxRow, StrategyRow, SystemFlagRow
from lab.strategies.registry import build

spec = importlib.util.spec_from_file_location(
    "forward_report", Path(__file__).resolve().parents[2] / "scripts/forward_report.py",
)
fr = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fr
spec.loader.exec_module(fr)
NOW = datetime(2026, 11, 8, 19, 0, tzinfo=UTC)  # Sunday 23:00 Samara


@pytest.fixture
def observed(session):
    record = Registry(session).add(build(fr.ID).manifest)
    row = session.get_one(StrategyRow, record.id)
    row.rung, row.status = "paper", "measuring"
    observation = current_observation(session, fr.ID, start=True, at=NOW - timedelta(days=2))
    flag = SystemFlagRow(key=f"virtual_forward:{fr.ID}", updated_by="worker", value={
        "observation_id": observation.id, "checked_at": NOW.isoformat(),
        "started_at": (NOW - timedelta(days=2)).isoformat(), "marks_at": NOW.isoformat(),
        "archive_pending": 0, "archive_missing": 0, "archive_mismatches": 0,
        "open_positions": [], "unrealized_pnl": "0",
    })
    session.add(flag)
    session.flush()
    return observation, flag


def test_empty_report_never_calls_missing_trades_profit(session, observed):
    result = fr.report(session, fr.collect(session, NOW), NOW, weekly=True)
    assert "недостаточно данных" in result and "нет зачтённых сделок" in result
    assert "EV: 0.00" not in result and "P&L: 0.00" not in result
    assert "зачтено закрытых сделок 0" in result
    assert len(list(session.scalars(select(ForwardObservationRow)))) == 1
    assert not list(session.scalars(select(OutboxRow)))


def test_preview_without_observation_does_not_create_one(session):
    result = fr.report(session, fr.collect(session, NOW), NOW)
    assert "нет контракта" in result
    assert not list(session.scalars(select(ForwardObservationRow)))


def test_daily_and_weekly_are_enqueued_once_and_atomically(session, observed):
    assert len(fr.cycle(session, NOW, "operator")) == 2
    session.flush()
    assert fr.cycle(session, NOW + timedelta(seconds=20), "operator") == []
    rows = list(session.scalars(select(OutboxRow)))
    assert len(rows) == 2
    assert all(row.kind == "forward_report" and row.payload["parse_mode"] is None for row in rows)
    assert any("Недельный" in row.payload["text"] for row in rows)
    assert any("Ежедневный" in row.payload["text"] for row in rows)


def test_stale_alert_deduplicates_and_recovers(session, observed):
    _, flag = observed
    fr.cycle(session, NOW, "operator")
    stale = NOW + timedelta(minutes=4)
    assert len(fr.cycle(session, stale, "operator")) == 1
    assert fr.cycle(session, stale + timedelta(seconds=30), "operator") == []
    flag.value = {**flag.value, "checked_at": (stale + timedelta(minutes=1)).isoformat()}
    session.flush()
    assert len(fr.cycle(session, stale + timedelta(minutes=1), "operator")) == 1
    rows = list(session.scalars(select(OutboxRow).order_by(OutboxRow.id)))
    assert "восстановлены" in rows[-1].payload["text"]


def test_pending_archive_has_grace_period_and_recovers(session, observed):
    _, flag = observed
    flag.value = {**flag.value, "archive_pending": 2}
    session.flush()
    fr.cycle(session, NOW, "operator")
    later = NOW + timedelta(minutes=16)
    flag.value = {**flag.value, "checked_at": later.isoformat()}
    session.flush()
    assert len(fr.cycle(session, later, "operator")) == 1
    assert fr.cycle(session, later, "operator") == []
    flag.value = {**flag.value, "archive_pending": 0}
    session.flush()
    assert len(fr.cycle(session, later, "operator")) == 1


def test_open_positions_are_not_an_outage(session, observed):
    _, flag = observed
    flag.value = {**flag.value, "open_positions": [{"instrument": "BTC/USDT"}]}
    session.flush()
    assert fr.collect(session, NOW)["problems"] == []


def test_version_drift_alerts_without_starting_another_observation(session, observed):
    row = session.get_one(StrategyRow, fr.ID)
    row.params_json = {**row.params_json, "rebalance_days": 8}
    session.flush()
    result = fr.report(session, fr.collect(session, NOW), NOW, weekly=True)
    assert "версия наблюдения не подтверждена" in result
    assert len(list(session.scalars(select(ForwardObservationRow)))) == 1


def test_manual_message_does_not_reset_scheduled_dedup(session, observed):
    fr.cycle(session, NOW, "operator")
    assert len(fr.cycle(session, NOW, "operator", send_now="daily")) == 1
    assert fr.cycle(session, NOW, "operator") == []
    assert fr.cycle(session, NOW, "operator", send_now="daily") == []


def test_weekly_metrics_use_closed_ev_and_do_not_claim_continuous_drawdown(
    session, observed, monkeypatch,
):
    trades = [ClosedTrade(
        instrument="BTC/USDT", side="long", qty=Decimal(1), entry_price=Decimal(100),
        exit_price=Decimal(110), opened_at=NOW - timedelta(days=1),
        closed_at=NOW - timedelta(hours=1), pnl_gross=Decimal(10), costs=Costs(fee=Decimal(2)),
    )]
    monkeypatch.setattr(fr, "_journal_trades", lambda *args, **kwargs: trades)

    class Store:
        def read(self, *args):
            return []

    result = fr.report(session, fr.collect(session, NOW), NOW, weekly=True, store=Store())
    assert "P&L: 8.00 USD" in result and "EV: 8.00 USD/сделку" in result
    assert "издержки" in result.lower() and "2.00 USD" in result
    assert "непрерывной кривой нет" in result
    assert "выборка/блоки недостаточны" in result
    assert "Бенчмарк btc_dca: н/д" in result


def test_bad_timestamps_and_errors_do_not_expose_raw_details(session, observed):
    _, flag = observed
    flag.value = {**flag.value, "checked_at": "broken", "error": "postgresql://secret:pass@db"}
    session.flush()
    result = fr.report(session, fr.collect(session, NOW), NOW)
    assert "ошибка сбора" in result and "secret" not in result and "pass@" not in result


def test_periods_use_samara_and_do_not_replay_before_observation():
    start = datetime(2026, 11, 6, 19, 26, tzinfo=UTC)
    assert fr.due_periods(start, start) == []
    before = datetime(2026, 11, 7, 5, 4, tzinfo=UTC)
    assert fr.due_periods(before, start) == []
    after = before + timedelta(minutes=1)
    assert [kind for kind, _ in fr.due_periods(after, start)] == ["daily"]
    assert [kind for kind, _ in fr.due_periods(NOW, start)] == ["daily", "weekly"]
