#!/usr/bin/env python
"""Премия за риск волатильности: подразумеваемая против реализованной.

Зачем это отдельно от `signal_check`. Тот меряет, предсказывает ли показатель
НАПРАВЛЕНИЕ цены, и по DVOL ответил отрицательно (E3, `docs/research/bots-2026-09-20.md`).
Но продажа волатильности зарабатывает не на направлении: она зарабатывает, если рынок
систематически просит за страховку дороже, чем та в итоге стоит. Это другое утверждение,
измеряемое другой величиной, и его в лаборатории не мерили ни разу.

Премия = подразумеваемая волатильность сегодня минус реализованная за следующие `--days`
суток. Положительная средняя означает, что продавец опционов в среднем в плюсе; величина
говорит, насколько. Это НЕ доходность сделки: она зависит от того, чем торговать
(страддл, стрэнгл, вариансный своп), от веги, спреда и от того, что убыток продавца
не ограничен. Здесь меряется только сама премия — шаг 1 порядка проверки гипотезы.

Три вещи, без которых замер соврёт:

1. **Перекрытие горизонтов.** Премия на `h` дней вперёд, посчитанная от каждого дня,
   перекрывается сама с собой: соседние наблюдения делят `h−1` день из `h`, независимых
   в `h` раз меньше, шум занижен примерно в корень из `h`. Вердикт выносится по каждому
   `h`-му наблюдению. Проект принимал эту болезнь за находку дважды.
2. **Годы.** Волатильность живёт режимами: премия, набранная в один спокойный год,
   ничего не говорит о правиле. Знак по годам печатается отдельно.
3. **Хвост важнее средней.** Продавец волатильности зарабатывает часто и помалу, а теряет
   редко и помногу. Поэтому печатается не только средняя, но и худшее наблюдение и доля
   дней, где премия отрицательна.
4. **Чем считать реализованную.** Оценка по дневным ЗАКРЫТИЯМ не видит внутридневного хода
   и систематически занижает волатильность: день, прошедший вниз на 5% и вернувшийся,
   даёт нулевую доходность и нулевой вклад. Премия, целиком состоящая из этого занижения,
   была бы артефактом метода, а не платой за страховку. Поэтому `--estimator parkinson`
   считает по максимуму и минимуму (Паркинсон, 1980) — при прочих равных такая оценка
   примерно на пятую часть эффективнее и заведомо не ниже. Если премия переживает смену
   оценки, дело не в методе.

    python scripts/vol_premium.py --series /app/data/external/dvol.csv \\
        --venue binance --instrument BTC/USDT --days 30 --root /app/data
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date
from math import log, sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

TRADING_YEAR = 365  # крипта торгуется без выходных


def read_series(path: str) -> dict[date, float]:
    """Ряд подразумеваемой волатильности: CSV с колонками date,value в годовых процентах."""
    out: dict[date, float] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                out[date.fromisoformat(row["date"][:10])] = float(row["value"])
            except (TypeError, ValueError):
                continue  # пустая строка источника — пропуск честнее выдуманного числа
    return out


def realized(bars: list[dict], days: int, estimator: str) -> dict[date, float]:
    """Реализованная волатильность ВПЕРЁД на `days` суток, в годовых процентах.

    `close` — по дневным логарифмическим доходностям, как считают подразумеваемую.
    `parkinson` — по дневному размаху: сумма ln(H/L)² с делителем 4·ln2. Эта оценка
    видит внутридневной ход, которого close-to-close не видит вовсе.
    """
    out: dict[date, float] = {}
    if estimator == "parkinson":
        terms = [
            (b["d"], log(b["h"] / b["l"]) ** 2)
            for b in bars
            if b["h"] and b["l"] and b["l"] > 0 and b["h"] >= b["l"]
        ]
        k = 4 * log(2)
        for i in range(len(terms) - days):
            window = [t for _, t in terms[i : i + days]]
            if len(window) < days:
                continue
            out[terms[i][0]] = sqrt(sum(window) / (k * days) * TRADING_YEAR) * 100
        return out

    rets: list[tuple[date, float]] = []
    for prev, cur in zip(bars, bars[1:], strict=False):
        if prev["c"] > 0 and cur["c"] > 0:
            rets.append((cur["d"], log(cur["c"] / prev["c"])))
    for i in range(len(rets) - days):
        window = [r for _, r in rets[i : i + days]]
        if len(window) < days or len(set(window)) < 2:
            continue
        out[rets[i][0]] = stdev(window) * sqrt(TRADING_YEAR) * 100
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", required=True, help="CSV подразумеваемой волатильности")
    ap.add_argument("--venue", default="binance")
    ap.add_argument("--instrument", default="BTC/USDT")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--days", type=int, default=30, help="горизонт реализованной, суток")
    ap.add_argument(
        "--estimator",
        default="close",
        choices=("close", "parkinson"),
        help="чем считать реализованную: по закрытиям или по размаху",
    )
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    implied = read_series(args.series)
    if not implied:
        print("ряд подразумеваемой пуст")
        return 0

    cs = CandleStore(args.root)
    rows = cs.query(
        "select ts, high::DOUBLE as h, low::DOUBLE as l, close::DOUBLE as c "
        "from {candles} order by ts",
        args.venue,
        args.instrument,
        args.tf,
    )
    bars = [
        {"d": r["ts"].date(), "h": r["h"], "l": r["l"], "c": r["c"]}
        for r in rows
        if r["c"] and r["c"] > 0
    ]
    real = realized(bars, args.days, args.estimator)
    print(
        f"подразумеваемая: {len(implied)} дней, реализованная ({args.estimator}): "
        f"{len(real)} дней"
    )

    pairs = sorted((d, implied[d], real[d]) for d in implied.keys() & real.keys())
    if len(pairs) < args.days * 3:
        print(f"пересечение рядов мало ({len(pairs)} дней)")
        return 0
    prem = [(d, iv - rv) for d, iv, rv in pairs]
    print(f"пересечение: {len(prem)} дней, {prem[0][0]} … {prem[-1][0]}")

    # Вердикт — по неперекрывающимся наблюдениям: премия считается на `days` вперёд.
    thin = prem[:: args.days]
    values = [p for _, p in prem]
    thin_values = [p for _, p in thin]
    two_se = 2 * stdev(thin_values) / sqrt(len(thin_values)) if len(thin_values) > 1 else 0.0
    mean_all, mean_thin = fmean(values), fmean(thin_values)
    verdict = "премия есть" if abs(mean_thin) > two_se else "в пределах шума"

    print()
    print(f"{'показатель':46}{'значение':>12}")
    print(f"{'средняя подразумеваемая, % годовых':46}{fmean([iv for _, iv, _ in pairs]):>12.2f}")
    print(f"{'средняя реализованная, % годовых':46}{fmean([rv for _, _, rv in pairs]):>12.2f}")
    print(f"{'премия, пунктов волатильности':46}{mean_all:>12.2f}")
    label = f"премия без перекрытия (каждый {args.days}-й день)"
    print(f"{label:46}{mean_thin:>12.2f}")
    print(f"{'шум 2σ по неперекрывающимся':46}{two_se:>12.2f}")
    print(f"{'независимых наблюдений':46}{len(thin_values):>12}")
    negative = 100 * sum(v < 0 for v in values) / len(values)
    print(f"{'доля дней с ОТРИЦАТЕЛЬНОЙ премией, %':46}{negative:>12.1f}")
    print(f"{'худший день, пунктов':46}{min(values):>12.2f}")
    print(f"{'лучший день, пунктов':46}{max(values):>12.2f}")
    print(f"вердикт → {verdict}")

    years: dict[int, list[float]] = defaultdict(list)
    for d, p in thin:
        years[d.year].append(p)
    print()
    print(f"{'год':7}{'независ.':>10}{'премия':>12}")
    signs = []
    for year in sorted(years):
        vals = years[year]
        signs.append(fmean(vals))
        print(f"{year:<7}{len(vals):>10}{fmean(vals):>12.2f}")
    if len(signs) > 1:
        pos = sum(1 for s in signs if s > 0)
        print(f"знак совпадает в {max(pos, len(signs) - pos)} годах из {len(signs)}")

    # Режим рынка: премия продавца может жить только в спокойные времена, и тогда
    # правило требует фильтра, а не постоянной позиции.
    edges = sorted(iv for _, iv, _ in pairs)
    low, high = edges[len(edges) // 3], edges[2 * len(edges) // 3]
    buckets: dict[str, list[float]] = defaultdict(list)
    for (_, iv, _), (_, p) in zip(pairs, prem, strict=False):
        name = "низкая" if iv <= low else ("высокая" if iv >= high else "средняя")
        buckets[name].append(p)
    print()
    print(f"{'уровень подразумеваемой':24}{'дней':>8}{'премия':>12}")
    for name in ("низкая", "средняя", "высокая"):
        vals = buckets.get(name) or []
        if vals:
            print(f"{name:24}{len(vals):>8}{fmean(vals):>12.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
