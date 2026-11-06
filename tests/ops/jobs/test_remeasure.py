

def test_remeasure_stops_at_the_time_budget():
    """Сервер общий: воскресный прогон не должен занимать его часами.

    В реестре двадцать стратегий, среди них вселенные по 649 инструментов. Рядом на тех же
    ядрах живьём торгует соседний проект. Не успели за отведённое время — стратегии честно
    перечисляются в отчёте, а не молча остаются неизмеренными.
    """
    from datetime import UTC, datetime, timedelta

    from lab.ops.jobs.remeasure import RemeasureReport

    window = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC))
    report = RemeasureReport(window=window)
    report.measured.append("cex-perp-api-basis-cash-carry-cross")
    report.skipped.extend(
        ["cex-spot-paper-xsmom-alts-weekly", "cex-spot-paper-xsmom-alts-weekly-funding"]
    )

    text = report.text()

    assert "не успели" in text
    assert "xsmom" in text
    assert timedelta(minutes=60), "бюджет задаётся в config/discovery.yaml"


# --- attach_stability: общая проверка устойчивости для цикла и ручного подъёма ----------

def _row(branch: str = "cex-perp"):
    from types import SimpleNamespace

    return SimpleNamespace(id="cex-perp-test-rule", branch=branch, rung="backtest")


def _stability(windows: int, profitable: int, ahead: int):
    from lab.core.measure.types import Stability

    return Stability(
        windows=windows,
        profitable=profitable,
        ahead=ahead,
        compared=windows,
        window_days=730,
        step_days=180,
    )


def test_attach_stability_skips_windows_when_base_threshold_failed():
    """Провалившей просадку устойчивость ничего не изменит — десятки замеров не гоняем."""
    from datetime import UTC, datetime

    from lab.ops.jobs.remeasure import attach_stability
    from tests.core.test_ladder import make_metrics

    calls: list[str] = []

    def stability_fn(sid, **_):
        calls.append(sid)
        raise AssertionError("не должна вызываться")

    metrics = make_metrics(max_dd="9")
    out, problem = attach_stability(
        None, _row(), metrics, datetime(2026, 9, 16, tzinfo=UTC), stability_fn=stability_fn
    )

    assert calls == []
    assert problem is None
    assert out.stability is None


def test_attach_stability_failed_windows_turn_passed_into_failed():
    """Шорт листингов 13.09.2026: порог на одном окне взят, на окнах 9 из 16 при 60%.

    Без устойчивости ручной подъём перевёл бы его на `paper`."""
    from datetime import UTC, datetime

    from lab.core.measure import threshold
    from lab.ops.jobs.remeasure import attach_stability
    from tests.core.test_ladder import make_metrics

    metrics = make_metrics()
    assert threshold(metrics, "cex-perp", rung="backtest").status == "insufficient"

    out, problem = attach_stability(
        None,
        _row(),
        metrics,
        datetime(2026, 9, 16, tzinfo=UTC),
        stability_fn=lambda sid, **_: (_stability(16, 9, 9), []),
    )

    assert problem is None
    assert out.stability.windows == 16
    verdict = threshold(out, "cex-perp", rung="backtest")
    assert verdict.status == "failed"
    assert "stability_profitable" in verdict.failed_names()


def test_attach_stability_reports_why_windows_did_not_count():
    """Упал прогон или окон мало — оба пути получают блокировку с причиной."""
    from datetime import UTC, datetime

    from lab.ops.jobs.remeasure import attach_stability
    from tests.core.test_ladder import make_metrics

    at = datetime(2026, 9, 16, tzinfo=UTC)

    def broken(sid, **_):
        raise RuntimeError("нет свечей")

    out, problem = attach_stability(None, _row(), make_metrics(), at, stability_fn=broken)
    assert out.stability is None
    assert problem and "нет свечей" in problem
    from lab.core.measure import threshold

    assert out.stability_error == problem
    assert threshold(out, "cex-perp", rung="backtest").status == "insufficient"

    out, problem = attach_stability(
        None, _row(), make_metrics(), at, stability_fn=lambda sid, **_: (_stability(3, 3, 3), [])
    )
    assert out.stability.windows == 3
    assert problem and "окон 3" in problem
    assert threshold(out, "cex-perp", rung="backtest").status == "insufficient"


def test_failed_recalculation_discards_old_success():
    from datetime import UTC, datetime

    from lab.core.measure import threshold
    from lab.ops.jobs.remeasure import attach_stability
    from tests.core.test_ladder import make_metrics

    metrics = make_metrics().model_copy(update={"stability": _stability(10, 10, 10)})

    def broken(*args, **kwargs):
        raise TimeoutError("архив недоступен")

    out, problem = attach_stability(
        None, _row(), metrics, datetime(2026, 9, 16, tzinfo=UTC), stability_fn=broken
    )
    assert problem
    assert out.stability is None
    assert threshold(out, "cex-perp", rung="backtest").status == "insufficient"


def test_stability_snapshot_is_saved_without_rewriting_original(session):
    from lab.core.measure import history
    from lab.core.measure.runner import _persist
    from lab.core.measure.types import Measurement
    from lab.core.registry import Registry
    from lab.ops.jobs.remeasure import save_stability
    from tests.core.test_ladder import make_metrics, manifest

    row = Registry(session).add(manifest())
    metrics = make_metrics()
    measured = _persist(session, Measurement(
        id=None, strategy_id=row.id, mode="backtest", window_from=metrics.window_from,
        window_to=metrics.window_to, data_hash="a" * 64, code_version="test",
        costs_version="test", status="ok", metrics=metrics,
    ))
    enriched = metrics.model_copy(update={"stability": _stability(10, 8, 8)})
    save_stability(session, measured, enriched, row)
    snapshots = history(session, row.id)
    assert len(snapshots) == 2
    assert snapshots[0].metrics.stability.windows == 10
    assert snapshots[0].threshold.status == "passed"
    assert snapshots[1].metrics.stability is None

    from lab.core.ladder import Ladder, default_threshold_fn

    ladder = Ladder(session, threshold=default_threshold_fn())
    assert ladder.latest_metrics(row.id).stability.windows == 10
    _persist(session, measured.model_copy(update={"status": "incomplete", "metrics": None}))
    assert ladder.latest_metrics(row.id) is None  # новый сбой не воскрешает старый успех
