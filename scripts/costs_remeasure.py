#!/usr/bin/env python
"""Перемер «до и после» смены модели издержек — на ОДНОМ окне, одних данных, одном коде.

Зачем: `lab measure run` берёт окно «последние N суток до сейчас», а сохранённый прежний
снимок сделан другим кодом и на другом окне. Сравнение с ним смешивает три причины
(тариф, код, окно), и разность нельзя приписать тарифу. Здесь различается только модель.

Для каждой стратегии:
  * окно — как у её последнего замера `ok` в выбранном режиме (сдвигать нельзя: у медленных
    правил сдвиг на сутки меняет состав сделок);
  * «до»  — текущая модель с отброшенными блоками `perp` и `version: 1`, БЕЗ сохранения;
  * «после» — текущая модель как есть; сохраняется обычным снимком (он и есть перемер),
    если не передан `--no-save`.
Прежний сохранённый снимок печатается третьей колонкой — для сверки: «до» на нынешнем
коде должно его воспроизвести, если с тех пор код замера этой стратегии не менялся.

Первое применение — 27.09.2026, `version: 2` (у Bybit спот и перпы раздельно):
    python scripts/costs_remeasure.py --id cex-perp-paper-day-open-fade \\
        --id cex-perp-preset-trailing-breakout-bot
Долгий прогон (15m, годы, восемь инструментов; гашение первого часа — ~7.5 мин на пару) —
через `run-detached.sh`, скрипт — копией в /root/costs-code, хранилище — ЯВНЫМ путём
(без него замер молча качает свечи с биржи, см. CLAUDE.md):
    run-detached.sh costs "cd /opt/crypto-trading-lab && docker compose \\
      -f deploy/docker-compose.yml run --rm --no-deps -T -v /root/costs-code:/costs \\
      -w /app -e LAB_DATA_ROOT=/app/data --entrypoint /app/.venv/bin/python worker \\
      /costs/costs_remeasure.py --id … </dev/null"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from lab.core.costs import CostModel, load_costs  # noqa: E402
from lab.core.measure.runner import _row_to_measurement  # noqa: E402
from lab.db import make_engine, make_session_factory, session_scope  # noqa: E402
from lab.db.models import MeasurementRow  # noqa: E402
from lab.ops.measure import make_measure  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def spot_only_model() -> CostModel:
    """Модель «как до v2»: перпы по строке спота. Версия — 1, чтобы снимок не спутать."""
    cfg = load_costs()
    cex = {name: t.model_copy(update={"perp": None}) for name, t in cfg.cex.items()}
    return CostModel(cfg.model_copy(update={"version": 1, "cex": cex}))


def last_ok(scope, strategy_id: str, mode: str) -> MeasurementRow | None:
    with scope() as session:
        row = session.scalars(
            select(MeasurementRow)
            .where(
                MeasurementRow.strategy_id == strategy_id,
                MeasurementRow.mode == mode,
                MeasurementRow.status == "ok",
            )
            .order_by(MeasurementRow.id.desc())
            .limit(1)
        ).first()
        if row is not None:
            session.expunge(row)
        return row


def _num(value, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def summary(m) -> dict[str, str]:
    """Строки сравнения — одни для снимка из базы и для свежего замера."""
    mt = m.metrics
    c = mt.costs
    failed = m.threshold.failed_names() if m.threshold else []
    return {
        "сделок": str(mt.n_trades),
        "итог за окно, %": _num(mt.net_pnl_pct),
        "годовых от торговли, %": _num(mt.cagr_pct),
        "EV на сделку, $": _num(mt.ev_per_trade, 3),
        "просадка, %": _num(mt.max_dd_pct),
        "комиссия, $": _num(c.fee),
        "проскальзывание, $": _num(c.slippage),
        "фандинг, $": _num(c.funding),
        "комиссия / оборот, б.п.": _num(c.fee / c.turnover * 10_000 if c.turnover else None),
        "стоп стратегии": f"{mt.stopped_at:%d.%m.%Y}" if mt.stopped_at else "нет",
        "пропущено сигналов": str(mt.blocked_signals or 0),
        "порог": m.threshold.status if m.threshold else "—",
        "не прошло": ", ".join(failed) or "—",
        "модель": m.costs_version[:22],
        "код": (m.code_version or "")[:12],
        # Отпечаток прочитанных данных: без хранилища замер качает свечи с биржи, и «до»
        # с «после» сравнимы, только если отпечаток совпал.
        "данные": (m.data_hash or "")[:12],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", action="append", required=True, help="id стратегии (можно много)")
    ap.add_argument("--mode", default="backtest")
    ap.add_argument("--no-save", action="store_true", help="не сохранять замер «после»")
    args = ap.parse_args()

    factory = make_session_factory(make_engine())

    def scope():
        return session_scope(factory)

    measure = make_measure(scope)
    before_model, after_model = spot_only_model(), CostModel()
    print(f"модель «до»: {before_model.version}; «после»: {after_model.version}", flush=True)

    worst = 0
    for sid in args.id:
        prev = last_ok(scope, sid, args.mode)
        if prev is None:
            print(f"\n{sid}: нет замера ok в режиме {args.mode} — окно брать неоткуда")
            worst = 2
            continue
        window = (prev.window_from, prev.window_to)
        print(
            f"\n=== {sid} · окно {window[0]:%d.%m.%Y}–{window[1]:%d.%m.%Y} "
            f"(как у замера {prev.id})",
            flush=True,
        )
        # `session=None` поверх того, что подставит обёртка: снимок «до» в базу не идёт.
        before = measure(
            strategy_id=sid, mode=args.mode, window=window, costs=before_model, session=None
        )
        print(f"  «до» посчитан: {before.status}", flush=True)
        extra = {"session": None} if args.no_save else {}
        after = measure(
            strategy_id=sid, mode=args.mode, window=window, costs=after_model, **extra
        )
        print(f"  «после» посчитан: {after.status}, снимок {after.id or 'не сохранён'}",
              flush=True)
        if before.status != "ok" or after.status != "ok":
            print(f"  причина: до — {before.reason}; после — {after.reason}")
            worst = 1
            continue
        cols = [
            (f"замер {prev.id}", summary(_row_to_measurement(prev, cached=True))),
            ("до (v1)", summary(before)),
            ("после (v2)", summary(after)),
        ]
        width = max(len(k) for k in cols[0][1])
        print(f"  {'':{width}}  " + "  ".join(f"{name:>22}" for name, _ in cols))
        for key in cols[0][1]:
            print(f"  {key:{width}}  " + "  ".join(f"{c[key]:>22}" for _, c in cols))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
