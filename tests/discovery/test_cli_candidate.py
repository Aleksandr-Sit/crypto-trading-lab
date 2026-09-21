"""Шов R15.1: решение по кандидату принимается не только кнопкой карточки.

Кнопки живут на карточке, а карточек уходит `max_cards_per_scan` за прогон (десять).
Всё, что осталось в очереди, до сих пор нельзя было ни принять, ни отклонить ниоткуда:
21.09.2026 так набралось 5075 записей без единого решения.
"""

from __future__ import annotations

from lab.cli import main


def test_reject_all_without_filter_demands_confirmation(capsys):
    """Пакетное отклонение без отбора стёрло бы всю очередь одной опечаткой.

    Отказ выносится ДО базы, поэтому тесту не нужно ни соединения, ни данных.
    """
    code = main(["candidate", "reject", "--all"])

    assert code == 2
    assert "--yes" in capsys.readouterr().err
