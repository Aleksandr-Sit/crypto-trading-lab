#!/usr/bin/env python
"""Перемер «до и после» стопа яруса размещения — на ОДНОМ окне, одних данных, одном коде.

Решение владельца 4 (27.09.2026): стоп правила размещения (`allocation: true`) считается от
ВЕРШИНЫ капитала стратегии, с открытой позицией по цене бара. До 28.09 замер считал его по
закрытым сделкам от стартового капитала. Различается только определение стопа:

  * «до»  — прежнее определение (признак яруса для замера подменён на «нет»), БЕЗ сохранения;
  * «после» — код как есть; сохраняется обычным снимком, если не передан `--no-save`.

Окно — как у последнего замера `ok` стратегии в режиме (`--mode`), либо явное `--window`.
Явное окно не сохраняется никогда: снимок на чужом окне лестница прочла бы как свежий замер.
Колонки сравнения — те же, что у `costs_remeasure.py`.

Первое применение — 28.09.2026, ротация золото/BTC, образ с новым кодом, хранилище явным путём:
    bash scripts/lab-oneoff.sh scripts/stop_remeasure.py \\
        -- --id cex-spot-external-rotation-gold-btc \\
        -- --id cex-spot-external-rotation-gold-btc --window 2020-08-26 2026-09-14
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from costs_remeasure import last_ok, summary  # noqa: E402

import lab.core.measure.runner as runner  # noqa: E402
from lab.core.measure.runner import _row_to_measurement  # noqa: E402
from lab.db import make_engine, make_session_factory, session_scope  # noqa: E402
from lab.ops.measure import make_measure  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _day(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", action="append", required=True, help="id стратегии (можно много)")
    ap.add_argument("--mode", default="backtest")
    ap.add_argument("--window", nargs=2, metavar=("С", "ПО"), help="ГГГГ-ММ-ДД ГГГГ-ММ-ДД")
    ap.add_argument("--no-save", action="store_true", help="не сохранять замер «после»")
    args = ap.parse_args()

    factory = make_session_factory(make_engine())

    def scope():
        return session_scope(factory)

    measure = make_measure(scope)
    real = runner.is_allocation

    worst = 0
    for sid in args.id:
        prev = last_ok(scope, sid, args.mode)
        if args.window:
            window = (_day(args.window[0]), _day(args.window[1]))
            label = "явное окно, без сохранения"
        elif prev is not None:
            window = (prev.window_from, prev.window_to)
            label = f"как у замера {prev.id}"
        else:
            print(f"\n{sid}: нет замера ok в режиме {args.mode} — окно брать неоткуда")
            worst = 2
            continue
        print(f"\n=== {sid} · окно {window[0]:%d.%m.%Y}–{window[1]:%d.%m.%Y} ({label})",
              flush=True)

        runner.is_allocation = lambda _params: False  # прежнее определение стопа
        try:
            before = measure(strategy_id=sid, mode=args.mode, window=window, session=None)
        finally:
            runner.is_allocation = real
        print(f"  «до» посчитан: {before.status}", flush=True)
        extra = {"session": None} if (args.no_save or args.window) else {}
        after = measure(strategy_id=sid, mode=args.mode, window=window, **extra)
        print(f"  «после» посчитан: {after.status}, снимок {after.id or 'не сохранён'}",
              flush=True)
        if before.status != "ok" or after.status != "ok":
            print(f"  причина: до — {before.reason}; после — {after.reason}")
            worst = 1
            continue
        cols = [("до (закрытые)", summary(before)), ("после (вершина)", summary(after))]
        if prev is not None and not args.window:
            cols.insert(0, (f"замер {prev.id}", summary(_row_to_measurement(prev, cached=True))))
        width = max(len(k) for k in cols[0][1])
        print(f"  {'':{width}}  " + "  ".join(f"{name:>22}" for name, _ in cols))
        for key in cols[0][1]:
            print(f"  {key:{width}}  " + "  ".join(f"{c[key]:>22}" for _, c in cols))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
