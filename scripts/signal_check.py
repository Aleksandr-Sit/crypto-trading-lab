#!/usr/bin/env python
"""Есть ли у показателя предсказательная сила — проверка ДО написания стратегии.

Порядок работы, выведенный дорогой ценой. Стратегия на вымывании плеча писалась,
кодировалась, регистрировалась и мерилась — и дала `insufficient` на восемнадцати
сделках, то есть не ответила вообще ничего. Прямая проверка того же за минуту показала,
что эффекта нет. **Сначала проверяем, есть ли сигнал, и только потом пишем правила.**

Что считается: дни делятся на группы по значению показателя (верхние и нижние 10% его
собственного распределения), и для каждой группы берётся средняя доходность вперёд.
Сравнение — с ОБЫЧНЫМ днём, потому что у рынка есть свой дрейф.

Показатели берутся из хранилища позиционирования: открытый интерес и его суточное
изменение, соотношение лонг/шорт у крупных счетов и по всем счетам, поток тейкеров.

Ключ `--by-year` — обязательная последняя проверка, а не украшение. Поток тейкеров прошёл
четыре проверки подряд, включая согласованность по пяти инструментам, и развалился на
пятой: эффект жил только в 2023–2024 и менял знак в 2022, 2025 и 2026. Пять монет ходят
вместе, поэтому пять подтверждений — это одно подтверждение, повторённое пять раз;
настоящая независимая проверка — по ВРЕМЕНИ.

    python scripts/signal_check.py --metric taker_ratio
    python scripts/signal_check.py --metric taker_ratio --by-year
    python scripts/signal_check.py --metric top_positions_ratio --bases BTC,ETH,SOL
    python scripts/signal_check.py --csv data/external/fng.csv --days 3200 --by-year
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from math import sqrt
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.positioning import PositioningStore  # noqa: E402
from lab.data.store import CandleStore  # noqa: E402

DEFAULT_BASES = "BTC,ETH,SOL,XRP,DOGE,AVAX,LINK,ADA"
METRICS = (
    "open_interest",
    "open_interest_change",  # считается здесь: суточное изменение интереса, %
    "top_accounts_ratio",
    "top_positions_ratio",
    "accounts_ratio",
    "taker_ratio",
)

Row = tuple[datetime, float, dict[int, float]]


def series(
    ps: PositioningStore,
    cs: CandleStore,
    name: str,
    window: tuple[datetime, datetime],
    metric: str,
    horizons: list[int],
) -> list[Row]:
    """Тройки «день → значение показателя → доходности вперёд»."""
    days = ps.daily("binance", name, *window)
    bars = {c.ts: c.close for c in cs.read("binance", name, "1d", *window)}
    rows = [d for d in days if d["ts"] in bars]
    out: list[Row] = []
    longest = max(horizons)
    for i in range(1, len(rows) - longest):
        now = rows[i]["ts"]
        if metric == "open_interest_change":
            prev_oi = float(rows[i - 1]["open_interest"] or 0)
            if prev_oi <= 0:
                continue
            value = (float(rows[i]["open_interest"] or 0) - prev_oi) / prev_oi * 100
        else:
            raw = rows[i].get(metric)
            if raw is None:
                continue
            value = float(raw)
        price = bars[now]
        if price <= 0:
            continue
        forward = {h: float((bars[rows[i + h]["ts"]] - price) / price * 100) for h in horizons}
        out.append((now, value, forward))
    return out


def csv_series(
    cs: CandleStore,
    path: Path,
    asset: str,
    window: tuple[datetime, datetime],
    horizons: list[int],
) -> list[Row]:
    """То же, но показатель берётся из готового ряда `date,value` (`external_series.py`).

    Ряд один на весь рынок (страх и жадность, хешрейт, DVOL), поэтому и инструмент один:
    объединять нечего, и «согласованность по инструментам» здесь не возникает даже
    случайно. Проверка остаётся одна — по годам.
    """
    values: dict[object, float] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                values[datetime.fromisoformat(row["date"]).date()] = float(row["value"])
            except (ValueError, KeyError):
                continue
    bars = [c for c in cs.read("binance", asset, "1d", *window) if c.close > 0]
    out: list[Row] = []
    longest = max(horizons)
    for i in range(len(bars) - longest):
        ts = bars[i].ts
        value = values.get(ts.date())
        if value is None:
            continue
        price = float(bars[i].close)
        forward = {
            h: float((bars[i + h].close - bars[i].close) / bars[i].close * 100) for h in horizons
        }
        if price > 0:
            out.append((ts, value, forward))
    return out


def _thinned(pooled: list[Row], h: int) -> list[Row]:
    """Каждое h-е наблюдение по времени — лекарство от перекрытия ГОРИЗОНТОВ.

    Доходность на h дней вперёд, посчитанная от КАЖДОГО дня, перекрывается сама с собой:
    соседние наблюдения делят h−1 день из h. Независимых наблюдений в h раз меньше, чем
    строк, а шум занижен примерно в корень из h — то есть инструмент показывает находку
    там, где её нет. Так гипотеза про вынос шортов дала +1.38 при шуме 0.94 и выглядела
    первым сигналом в новых данных; на неперекрывающихся наблюдениях — +1.00 при шуме 2.31,
    то есть ничего (`docs/research/liquidations-2026-09-14.md`, раздел 6).

    Сжатие по датам (`_se_by_date`) от этого НЕ спасает: оно лечит перекрытие по
    инструментам, а это — перекрытие по времени, другая болезнь с тем же симптомом.
    """
    dates = sorted({ts for ts, _, _ in pooled})
    keep = set(dates[::h])
    return [r for r in pooled if r[0] in keep]


def _spread(pooled: list[Row], low_edge: float, high_edge: float, h: int) -> tuple[float, float]:
    """Разброс между хвостами и его шум (2σ) на переданном наборе наблюдений."""
    low = [f[h] for _, v, f in pooled if v <= low_edge]
    high = [f[h] for _, v, f in pooled if v >= high_edge]
    if not low or not high:
        return 0.0, 0.0
    noise = 2 * sqrt(
        _se_by_date(pooled, low_edge, h, "low") ** 2
        + _se_by_date(pooled, high_edge, h, "high") ** 2
    )
    return mean(high) - mean(low), noise


def _se_by_date(pooled: list[Row], edge: float, h: int, tail: str) -> float:
    """Стандартная ошибка средней хвоста, где наблюдение — дата, а не строка."""
    by_date: dict[datetime, list[float]] = defaultdict(list)
    for ts, v, f in pooled:
        if (tail == "low" and v <= edge) or (tail == "high" and v >= edge):
            by_date[ts].append(f[h])
    means = [mean(vals) for vals in by_date.values()]
    return stdev(means) / sqrt(len(means)) if len(means) > 1 else 0.0


def by_year(pooled: list[Row], low_edge: float, high_edge: float, h: int) -> None:
    """Тот же эффект, разложенный по годам. Порог ОБЩИЙ, иначе года несопоставимы."""
    years: dict[int, list[Row]] = defaultdict(list)
    for row in pooled:
        years[row[0].year].append(row)

    print(f"\nПО ГОДАМ, горизонт {h} дн (порог общий для всех лет)")
    print(f"{'год':6}{'в хвостах':>11}{'нижний':>10}{'верхний':>10}{'обычный':>10}{'разброс':>10}")
    spreads: list[float] = []
    for year in sorted(years):
        rows = years[year]
        low = [f[h] for _, v, f in rows if v <= low_edge]
        high = [f[h] for _, v, f in rows if v >= high_edge]
        if not low or not high:
            print(f"{year:<6}{len(low) + len(high):>11}{'мало наблюдений':>39}")
            continue
        usual = mean(f[h] for _, _, f in rows)
        spread = mean(high) - mean(low)
        spreads.append(spread)
        print(
            f"{year:<6}{len(low) + len(high):>11}{mean(low):>9.2f}%{mean(high):>9.2f}%"
            f"{usual:>9.2f}%{spread:>10.2f}"
        )

    if len(spreads) < 2:
        return
    positive = sum(1 for s in spreads if s > 0)
    agree = max(positive, len(spreads) - positive)
    print(f"\nЗнак разброса совпадает в {agree} годах из {len(spreads)}.")
    print(
        "Читать так: если знак скачет — эффекта нет, есть режим, который уже кончился.\n"
        "Согласованность по ИНСТРУМЕНТАМ этого не покажет: монеты ходят вместе, и пять\n"
        "подтверждений — это одно подтверждение, повторённое пять раз."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metric", default="taker_ratio", choices=METRICS)
    ap.add_argument("--bases", default=DEFAULT_BASES)
    ap.add_argument("--horizons", default="1,3,5,10")
    ap.add_argument("--tail-pct", type=float, default=10.0, help="размер хвоста, %%")
    ap.add_argument("--days", type=int, default=1480)
    ap.add_argument("--by-year", action="store_true", help="разложить эффект по годам")
    ap.add_argument("--year-horizon", type=int, default=0, help="горизонт для разбора по годам")
    ap.add_argument("--csv", default=None, help="готовый ряд date,value вместо позиционирования")
    ap.add_argument("--asset", default="BTC/USDT", help="инструмент сравнения для --csv")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    to = datetime.now(UTC)
    window = (to - timedelta(days=args.days), to)
    ps, cs = PositioningStore(args.root), CandleStore(args.root)

    pooled: list[Row] = []
    if args.csv:
        pooled = csv_series(cs, Path(args.csv), args.asset, window, horizons)
        print(f"{Path(args.csv).stem}: {len(pooled)} дней против {args.asset}")
        args.metric = Path(args.csv).stem
    for base in [] if args.csv else [b.strip() for b in args.bases.split(",") if b.strip()]:
        try:
            rows = series(ps, cs, f"{base}/USDT:USDT", window, args.metric, horizons)
        except Exception as err:  # noqa: BLE001 — нет данных по инструменту, не падаем
            print(f"{base}: {type(err).__name__}")
            continue
        if rows:
            pooled += rows
            print(f"{base}: {len(rows)} дней")

    if len(pooled) < 100:
        print("\nслишком мало данных для вывода")
        return 0

    # Хвосты считаются по ОБЪЕДИНЁННОМУ распределению: у разных монет уровни свои,
    # но нас интересует «необычно высоко/низко» в общем смысле.
    values = sorted(v for _, v, _ in pooled)
    k = max(1, int(len(values) * args.tail_pct / 100))
    low_edge, high_edge = values[k], values[-k]
    low = [f for _, v, f in pooled if v <= low_edge]
    high = [f for _, v, f in pooled if v >= high_edge]

    print(f"\nпоказатель: {args.metric} | дней {len(pooled)}")
    print(f"нижние {args.tail_pct:.0f}%: значение ≤ {low_edge:.4f} ({len(low)} дней)")
    print(f"верхние {args.tail_pct:.0f}%: значение ≥ {high_edge:.4f} ({len(high)} дней)\n")
    print(
        f"{'горизонт':10}{'нижний хвост':>15}{'верхний хвост':>16}"
        f"{'обычный день':>15}{'разброс':>10}{'шум':>8}"
        f"{'разброс б/п':>13}{'шум б/п':>10}{'наблюд.':>9}"
    )
    for h in horizons:
        lo = mean(f[h] for f in low)
        hi = mean(f[h] for f in high)
        al = mean(f[h] for _, _, f in pooled)
        # Шум — по ДАТАМ, а не по строкам: несколько монет в один день — одно наблюдение
        # (ревизия 12.09.2026; до неё скрипт печатал средние без меры шума вовсе).
        spread, noise = _spread(pooled, low_edge, high_edge, h)
        # «б/п» — без перекрытия: каждое h-е наблюдение. Вердикт выносится ПО НЕМУ.
        thin = _thinned(pooled, h)
        t_spread, t_noise = _spread(thin, low_edge, high_edge, h)
        n_thin = len({ts for ts, _, _ in thin})
        print(
            f"{h:>4} дн   {lo:>14.2f}%{hi:>15.2f}%{al:>14.2f}%"
            f"{spread:>10.2f}{noise:>8.2f}{t_spread:>13.2f}{t_noise:>10.2f}{n_thin:>9}"
        )
    print(
        "\nЧитать так: разброс между хвостами — это ВСЁ, что показатель обещает.\n"
        "Вердикт выносится по колонкам «б/п» (без перекрытия горизонтов): доходность\n"
        "вперёд от каждого дня перекрывается сама с собой, и обычный шум занижен примерно\n"
        "в корень из горизонта. Меньше своего шума — эффекта нет; меньше двух-трёх\n"
        "десятых процента — круг по издержкам съест его целиком."
    )

    if args.by_year:
        # По умолчанию второй горизонт: на однодневном шум перекрывает эффект.
        h = args.year_horizon or (horizons[1] if len(horizons) > 1 else horizons[0])
        if h not in horizons:
            print(f"\nгоризонт {h} не считался, добавьте его в --horizons")
            return 1
        by_year(pooled, low_edge, high_edge, h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
