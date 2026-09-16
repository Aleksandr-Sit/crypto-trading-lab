#!/usr/bin/env python
"""Прогнать лестницу для ОДНОЙ стратегии по её последнему замеру.

`lab measure run` считает снимок, но ступень не двигает: двигает её лестница
(`core.ladder.Ladder.evaluate`), а зовёт лестницу недельный цикл — по всем живым
стратегиям сразу. Когда нужно провести одну (прошла порог, ждать воскресенья незачем),
гонять весь цикл дорого: он перемеряет всё подряд.

Сам порог здесь не пересчитывается заново из воздуха — берётся последний снимок из базы,
тот же, что печатает `lab measure show`. Поэтому перед вызовом должен быть свежий замер.

Устойчивость по скользящим окнам считается ТАК ЖЕ, как в воскресном цикле
(`attach_stability`): только если остальные критерии пройдены. До 16.09.2026 скрипт
её не считал вовсе и поднял бы на `paper` шорт листингов — порог на одном окне взят,
на окнах провален (9 из 16). Поэтому здесь строже, чем в цикле: если устойчивость была
нужна, но не легла в порог (прогон упал, окон мало), ступень НЕ двигается.

Ступень выше `micro` лестница не поднимает никогда (`OperatorRequired`) — реальными
деньгами отсюда не рискуют.

    python scripts/evaluate_strategy.py --id cex-spot-external-rotation-gold-btc
    python scripts/evaluate_strategy.py --id … --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab.core.ladder import Ladder, default_threshold_fn  # noqa: E402
from lab.core.registry import Registry  # noqa: E402
from lab.core.risk import DbHaltSwitch  # noqa: E402
from lab.db import make_engine, make_session_factory, session_scope  # noqa: E402
from lab.db.base import utcnow  # noqa: E402
from lab.ops.jobs.remeasure import attach_stability  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", required=True, help="id стратегии в реестре базы")
    ap.add_argument("--dry-run", action="store_true", help="показать состояние и выйти")
    args = ap.parse_args()

    factory = make_session_factory(make_engine())

    def scope():
        return session_scope(factory)

    with scope() as session:
        row = Registry(session).get(args.id)
        print(f"{row.id}: ступень {row.rung}, статус {row.status}")
        if args.dry_run:
            return 0

        # Сборка та же, что у worker и недельного цикла: порог из конфига и общий
        # рубильник стопа. Иначе лестница здесь судила бы по другим правилам.
        ladder = Ladder(
            session, threshold=default_threshold_fn(), halt=DbHaltSwitch(session)
        )
        metrics = ladder.latest_metrics(args.id)
        if metrics is None:
            print("замера нет — сначала `lab measure run`")
            return 1
        metrics, problem = attach_stability(scope, row, metrics, utcnow())
        if problem:
            print(f"ступень не тронута: {problem}")
            return 2
        st = metrics.stability
        if st is not None:
            print(
                f"устойчивость: в плюс {st.profitable} из {st.windows} окон, "
                f"впереди бенчмарка {st.ahead} из {st.compared}"
            )

        before = (row.rung, row.status)
        ladder.evaluate(args.id, metrics=metrics)
        session.flush()
        after = Registry(session).get(args.id)
        if (after.rung, after.status) == before:
            print("ступень не изменилась — смотрите критерии порога в `lab measure show`")
        else:
            print(f"стало: ступень {after.rung}, статус {after.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
