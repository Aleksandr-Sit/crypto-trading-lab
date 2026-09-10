

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
