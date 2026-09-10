#!/usr/bin/env python
"""Прогнать недельный цикл лаборатории РУКАМИ — то же, что делает планировщик в воскресенье.

Зачем отдельный вход: `lab service worker --once` только регистрирует задания и выходит,
а сами они запускаются по расписанию. Проверить цикл целиком до наступления воскресенья
было нечем, и он ни разу не отрабатывал от начала до конца — все замеры делались руками.

Цикл делает три вещи подряд:
  1. меряет каждую живую стратегию на свежем окне (`remeasure.window_days`);
  2. для прошедших порог считает устойчивость по скользящим окнам;
  3. отдаёт метрики лестнице (`ladder.evaluate`) — она и двигает ступени.

Ступень выше `micro` лестница сама не поднимает (`OperatorRequired`), реальными деньгами
здесь не рискуют. Бюджет времени — `remeasure.budget_minutes` в `config/discovery.yaml`.

    python scripts/run_weekly_cycle.py            # полный цикл
    python scripts/run_weekly_cycle.py --dry-run  # только показать, кого будет мерить
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab.core.ladder import Ladder  # noqa: E402
from lab.core.registry import Registry  # noqa: E402
from lab.db import make_engine, make_session_factory, session_scope  # noqa: E402
from lab.ops.jobs import weekly_remeasure  # noqa: E402
from lab.ops.measure import make_measure  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="показать состав и выйти")
    args = ap.parse_args()

    factory = make_session_factory(make_engine())

    def scope():
        return session_scope(factory)

    with scope() as session:
        rows = Registry(session).list()
        live = [r for r in rows if r.status in ("candidate", "measuring", "passed", "degraded")]
        print(f"живых стратегий: {len(live)} из {len(rows)}")
        for r in live:
            print(f"  {r.id:52} {r.rung:9} {r.status}")
    if args.dry_run:
        return 0

    print("\n--- цикл пошёл ---", flush=True)
    report = weekly_remeasure(scope, measure=make_measure(scope), ladder_factory=Ladder)
    print(report.text())
    print(
        f"\nизмерено {len(report.measured)}, переходов {len(report.transitions)}, "
        f"ошибок {len(report.failed)}, не успели {len(report.skipped)}"
    )
    for sid, err in sorted(report.failed.items()):
        print(f"  ошибка {sid}: {err[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
