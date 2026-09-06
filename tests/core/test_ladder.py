"""Лестница доверия (истории 8–11, решение §7). Порог — заглушка, реестр — настоящий (Postgres).

Порог берётся через интерфейс `threshold(metrics, branch, rung) -> ThresholdResult`; реализацию
core.measure здесь не импортируем — только типы.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Branch, Rung, Signal, SignalOutcome, Status
from lab.core.journal import Journal
from lab.core.ladder import Ladder, OperatorRequired, Transition
from lab.core.measure.types import (
    CostsBreakdown,
    Criterion,
    Metrics,
    NotApplicable,
    SampleStatus,
    ThresholdResult,
)
from lab.core.registry import Registry
from lab.core.risk import MemoryHaltSwitch

D = Decimal
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def make_metrics(
    n_trades: int = 40, ev: str = "1.5", max_dd: str = "3", vs_btc: str = "2"
) -> Metrics:
    na = NotApplicable(reason="н/д")
    return Metrics(
        n_trades=n_trades,
        sample=SampleStatus(status="ok", n=n_trades, required=30, detail=f"{n_trades} из 30"),
        net_pnl=D(60),
        net_pnl_pct=D(6),
        ev_per_trade=D(ev),
        ev_ci95=(D("0.5"), D("2.5")),
        win_rate=D("0.55"),
        profit_factor=D("1.4"),
        payoff=D("1.1"),
        max_dd_pct=D(max_dd),
        dd_duration_days=D(2),
        sharpe=D("1.2"),
        sortino=D("1.5"),
        exposure_pct=D(50),
        costs_pct=D("0.1"),
        costs=CostsBreakdown(),
        btc_bh_pct=D(4),
        vs_btc=D(vs_btc),
        paper_vs_live_gap=na,
        copy_lag_cost=na,
        brier=na,
        resolution_return=na,
        mint_success_rate=na,
        mint_cost_failed=na,
        capital=D(1000),
        window_from=T0,
        window_to=T0 + timedelta(days=30),
    )


class FakeThreshold:
    """Заглушка правила порога: вердикт задаётся тестом, вызовы запоминаются."""

    def __init__(self, status: str = "passed") -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def __call__(self, metrics: Metrics, branch: Branch | str, rung: Rung | str) -> ThresholdResult:
        self.calls.append((str(branch), str(rung)))
        passed = self.status == "passed"
        criteria = [
            Criterion(name="n_trades", value=metrics.n_trades, limit=30, op=">=", passed=True),
            Criterion(name="ev", value=metrics.ev_per_trade, limit=D(0), op=">", passed=passed),
        ]
        sample = metrics.sample
        return ThresholdResult(status=self.status, criteria=criteria, sample=sample)  # type: ignore[arg-type]


def manifest(
    slug: str = "trend-v1", *, branch: str = "cex-perp", can_backtest: bool = True
) -> dict:
    return {
        "slug": slug,
        "branch": branch,
        "venue": "hyperliquid",
        "source_kind": "preset",
        "instruments": ["BTC-USDT"],
        "timeframe": "1h",
        "can_backtest": can_backtest,
        "stop": {"daily_pct": "3", "max_dd_pct": "10"},
        "params": {"ttl_s": 600},
    }


@pytest.fixture
def registry(session) -> Registry:
    return Registry(session)


@pytest.fixture
def threshold() -> FakeThreshold:
    return FakeThreshold()


@pytest.fixture
def cancelled() -> list[str]:
    return []


@pytest.fixture
def ladder(session, threshold, cancelled) -> Ladder:
    return Ladder(
        session,
        threshold=threshold,
        halt=MemoryHaltSwitch(),
        cancel_orders=lambda sid: cancelled.append(sid) or 2,
    )


def test_evaluate_promotes_one_rung_on_passed_threshold_and_records_numbers(
    registry, ladder, threshold
):
    s = registry.add(manifest())
    assert s.rung == Rung.BACKTEST

    t = ladder.evaluate(s.id, make_metrics())
    assert isinstance(t, Transition)
    assert (t.from_rung, t.to_rung, t.by) == (Rung.BACKTEST, Rung.PAPER, "system")
    assert threshold.calls == [("cex-perp", "backtest")]
    assert t.metrics_snapshot["metrics"]["n_trades"] == 40
    assert t.metrics_snapshot["threshold"]["status"] == "passed"
    assert registry.get(s.id).rung == Rung.PAPER
    assert registry.get(s.id).status == Status.MEASURING

    history = ladder.history(s.id)
    assert [(h.from_rung, h.to_rung) for h in history] == [(Rung.BACKTEST, Rung.PAPER)]
    assert history[0].metrics_snapshot == t.metrics_snapshot and history[0].by == "system"


def test_forward_only_strategy_starts_at_paper(registry, ladder):
    s = registry.add(manifest("mint-v1", branch="nft", can_backtest=False))
    t = ladder.evaluate(s.id)  # метрик ещё нет — только начальная ступень
    assert t is not None and (t.from_rung, t.to_rung) == (Rung.BACKTEST, Rung.PAPER)
    assert registry.get(s.id).rung == Rung.PAPER
    assert ladder.evaluate(s.id) is None  # повторно ступень не трогается


def test_semi_to_auto_only_by_operator(registry, ladder, threshold):
    s = registry.add(manifest())
    for _ in range(4):  # backtest → paper → micro → signal → semi
        assert ladder.evaluate(s.id, make_metrics()) is not None
    assert registry.get(s.id).rung == Rung.SEMI

    assert ladder.evaluate(s.id, make_metrics()) is None  # порог пройден, но выше — не сама
    assert registry.get(s.id).rung == Rung.SEMI
    assert registry.get(s.id).status == Status.PASSED

    with pytest.raises(OperatorRequired):
        ladder.promote(s.id, by="system")
    t = ladder.promote(s.id, by="operator")
    assert (t.from_rung, t.to_rung, t.by) == (Rung.SEMI, Rung.AUTO, "operator")
    assert registry.get(s.id).rung == Rung.AUTO


def test_failed_threshold_demotes_one_rung_with_reason(registry, ladder, threshold):
    s = registry.add(manifest())
    ladder.evaluate(s.id, make_metrics())
    ladder.evaluate(s.id, make_metrics())
    assert registry.get(s.id).rung == Rung.MICRO

    threshold.status = "failed"
    t = ladder.evaluate(s.id, make_metrics(ev="-0.3"))
    assert t is not None and (t.from_rung, t.to_rung) == (Rung.MICRO, Rung.PAPER)
    assert "ev" in t.reason and t.by == "system"
    assert t.metrics_snapshot["metrics"]["ev_per_trade"] == "-0.3"

    threshold.status = "insufficient"
    assert ladder.evaluate(s.id, make_metrics(n_trades=5)) is None
    assert registry.get(s.id).status == Status.MEASURING


def test_stop_breach_degrades_and_cancels_orders(registry, ladder, cancelled):
    s = registry.add(manifest())
    ladder.evaluate(s.id, make_metrics())
    t = ladder.breach(s.id, "дневной стоп −3.4 %", {"pnl_day_pct": "-3.4"})
    assert t.status == Status.DEGRADED and t.metrics_snapshot["cancelled_orders"] == 2
    assert cancelled == [s.id]
    assert registry.get(s.id).status == Status.DEGRADED
    assert ladder.evaluate(s.id, make_metrics()) is None  # degraded не двигается сама


def test_halt_all_and_resume_all_toggle_shared_switch(ladder):
    ladder.halt_all(by="operator")
    assert ladder.halted is True
    ladder.resume_all(by="operator")
    assert ladder.halted is False


def test_pending_signal_on_signal_rung_expires_by_manifest_ttl(session, registry, ladder):
    s = registry.add(manifest())
    for _ in range(3):  # → signal
        ladder.evaluate(s.id, make_metrics())
    assert registry.get(s.id).rung == Rung.SIGNAL
    journal = Journal(session)
    sig = journal.record_signal(
        Signal(
            strategy_id=s.id,
            decided_at=T0,
            instrument="BTC-USDT",
            side="buy",
            size=D(1),
            inputs_hash="h",
            ttl_s=60,  # у сигнала 60 с, но манифест говорит 600 с
        )
    )
    assert ladder.expire_signals(now=T0 + timedelta(seconds=300)) == []
    assert ladder.expire_signals(now=T0 + timedelta(seconds=601)) == [sig.id]
    assert journal.signal(sig.id).outcome == SignalOutcome.EXPIRED
