#!/usr/bin/env python
"""Предсказывают ли крупные ликвидации отскок? Прямая проверка на данных Coinalyze.

Ликвидации — единственный ряд, которого нет в архивах бирж, и ради него заводился ключ.
Гипотеза стандартная и правдоподобная: массовый вынос лонгов — это вынужденные продажи,
после которых давление уходит, и цена отскакивает.

**Контроль здесь важнее самой проверки.** Ликвидации лонгов случаются РОВНО ТОГДА, когда
цена падает. Значит «после ликвидаций цена растёт» может быть просто «после падения цена
растёт» — тот же капкан, в котором погибли TDSequential и BbandRsi
(`docs/research/freqtrade-2026-09-14.md`). Поэтому считается и то, и другое:

1. доходность вперёд после всплеска против ОБЫЧНОГО дня;
2. она же против обычного дня С ТАКИМ ЖЕ ходом цены (корзины по недавнему движению).

**Сетка обязательна.** Горизонт и порог всплеска выбирает человек, а не данные, и первый
прогон (5 суток, порог ×3) — это ровно «объявленный оптимум», за который мы бракуем чужие
работы. Настоящее преимущество даёт ПЛАТО по сетке, а не отдельная удачная клетка.

Всплеск нормируется скользящей медианой за 30 дней: объём ликвидаций растёт вместе
с рынком, и одна и та же сумма в 2022 и в 2026 — разные события.

    python scripts/liquidation_check.py --root /app/data            # сетка целиком
    python scripts/liquidation_check.py --root /app/data --detail   # разбор одной клетки
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.config.env import environment  # noqa: E402
from lab.data.store import CandleStore  # noqa: E402

BASE = "https://api.coinalyze.net/v1"
# Монета → перпы главных площадок (агрегата у источника нет, суммируем сами)
# и наш спотовый ряд для цен.
ASSETS: dict[str, tuple[tuple[str, ...], str]] = {
    "BTC": (("BTCUSDT_PERP.A", "BTCUSDT.6", "BTCUSD_PERP.3"), "BTC/USDT"),
    "ETH": (("ETHUSDT_PERP.A", "ETHUSDT.6", "ETHUSD_PERP.3"), "ETH/USDT"),
    "SOL": (("SOLUSDT_PERP.A", "SOLUSDT.6", "SOLUSD_PERP.3"), "SOL/USDT"),
    "XRP": (("XRPUSDT_PERP.A", "XRPUSDT.6", "XRPUSD_PERP.3"), "XRP/USDT"),
    "DOGE": (("DOGEUSDT_PERP.A", "DOGEUSDT.6", "DOGEUSD_PERP.3"), "DOGE/USDT"),
}
NORM_DAYS = 30
COST_PCT = 0.10
HORIZONS = (1, 3, 5, 10, 20)
MULTIPLES = (2.0, 3.0, 4.0, 6.0, 8.0)
EVENTS = ("вынос лонгов", "вынос шортов", "вынос обоих")
MIN_SIGNALS = 30


def fetch(path: str, params: dict[str, str], key: str, pause: float = 1.6) -> list:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{BASE}/{path}?{query}", headers={"api_key": key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except Exception as err:  # noqa: BLE001
        print(f"  {path} {params.get('symbols')}: отказ {type(err).__name__}: {err}",
              file=sys.stderr)
        return []
    time.sleep(pause)
    return data if isinstance(data, list) else []


def liquidations(symbols: tuple[str, ...], key: str, days: int) -> dict:
    """Сумма ликвидаций по площадкам, по датам. Отказ одной площадки не отменяет остальные."""
    now = int(time.time())
    out: dict[object, dict[str, float]] = defaultdict(lambda: {"long": 0.0, "short": 0.0})
    got = 0
    for symbol in symbols:
        rows = fetch(
            "liquidation-history",
            {
                "symbols": symbol, "interval": "daily", "convert_to_usd": "true",
                "from": str(now - days * 86400), "to": str(now),
            },
            key,
        )
        for block in rows:
            for point in block.get("history", []):
                day = datetime.fromtimestamp(point["t"], UTC).date()
                out[day]["long"] += float(point.get("l") or 0)
                out[day]["short"] += float(point.get("s") or 0)
                got += 1
    return dict(out) if got else {}


def rolling_median(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(n, len(values)):
        window = [v for v in values[i - n : i] if v > 0]
        out[i] = median(window) if len(window) >= n // 2 else None
    return out


class Sample:
    """Наблюдения одного горизонта: исход, корзина хода цены и «во сколько раз» всплеск.

    Хранится не решение «сработало/нет», а само отношение к медиане: тогда одна загрузка
    данных обслуживает всю сетку порогов, и API дёргается один раз, а не двадцать пять.
    """

    def __init__(self) -> None:
        # Последнее поле — «независимое ли наблюдение»: при горизонте h доходности
        # соседних дней делят h−1 день из h, и считать их независимыми нельзя.
        self.rows: list[tuple[object, float, int, float, float, bool]] = []
        self.by_bucket: dict[int, list[float]] = defaultdict(list)
        self.by_day: dict[object, list[float]] = defaultdict(list)
        self.by_year: dict[int, list[float]] = defaultdict(list)

    def add(
        self, day, ret: float, bucket: int, long_x: float, short_x: float, solo: bool = True
    ) -> None:
        self.rows.append((day, ret, bucket, long_x, short_x, solo))
        self.by_bucket[bucket].append(ret)
        self.by_day[day].append(ret)
        self.by_year[day.year].append(ret)

    def fires(
        self, event: str, multiple: float, *, solo_only: bool = False
    ) -> list[tuple[object, float, int]]:
        out = []
        for day, ret, bucket, long_x, short_x, solo in self.rows:
            if solo_only and not solo:
                continue
            if event == "вынос лонгов":
                hit = long_x >= multiple
            elif event == "вынос шортов":
                hit = short_x >= multiple
            else:
                hit = long_x >= multiple and short_x >= multiple
            if hit:
                out.append((day, ret, bucket))
        return out


def collect(root: str, key: str, days: int) -> tuple[dict[int, Sample], list[str]]:
    """Один проход по источнику — все горизонты сразу. Запросы к API дороги, счёт дёшев."""
    cs = CandleStore(root)
    samples: dict[int, Sample] = {h: Sample() for h in HORIZONS}
    covered: list[str] = []

    for asset, (symbols, instrument) in ASSETS.items():
        liq = liquidations(symbols, key, days)
        if not liq:
            print(f"{asset}: ликвидаций не получено", file=sys.stderr)
            continue
        rows = cs.query(
            "select ts, open::DOUBLE o, close::DOUBLE c from {candles} order by ts",
            "binance", instrument, "1d",
        )
        price = {r["ts"].date(): (float(r["o"]), float(r["c"])) for r in rows if r["c"]}
        dates = sorted(d for d in liq if d in price)
        if len(dates) < 200:
            print(f"{asset}: пересечение рядов мало ({len(dates)})", file=sys.stderr)
            continue
        covered.append(f"{asset}({len(dates)})")

        longs = [liq[d]["long"] for d in dates]
        shorts = [liq[d]["short"] for d in dates]
        med_long = rolling_median(longs, NORM_DAYS)
        med_short = rolling_median(shorts, NORM_DAYS)

        for horizon in HORIZONS:
            sample = samples[horizon]
            for i in range(max(NORM_DAYS, horizon), len(dates) - horizon - 1):
                day = dates[i]
                entry = price[dates[i + 1]][0]  # вход по СЛЕДУЮЩЕМУ открытию
                exit_ = price[dates[i + 1 + horizon]][0]
                if entry <= 0 or not med_long[i] or not med_short[i]:
                    continue
                ret = (exit_ / entry - 1) * 100
                moved = (price[day][1] / price[dates[i - horizon]][1] - 1) * 100
                sample.add(
                    day,
                    ret,
                    max(-10, min(10, int(moved // 2))),
                    longs[i] / med_long[i],
                    shorts[i] / med_short[i],
                    solo=(i % horizon == 0),
                )
    return samples, covered


def controlled(
    sample: Sample, event: str, multiple: float, *, solo_only: bool = False
) -> tuple[int, float, float, float]:
    """Сколько сигналов, сырая разница, разница ПОСЛЕ контроля по ходу цены и 2σ шума.

    `solo_only` — считать только по НЕПЕРЕКРЫВАЮЩИМСЯ наблюдениям (каждое h-е). Это
    единственный честный способ оценить шум на длинном горизонте: доходности соседних
    дней делят почти всё окно, и «две тысячи наблюдений» на деле означают четыреста.
    """
    fired = sample.fires(event, multiple, solo_only=solo_only)
    if len(fired) < MIN_SIGNALS:
        return len(fired), 0.0, 0.0, 0.0

    by_day: dict[object, list[float]] = defaultdict(list)
    by_bucket: dict[int, list[float]] = defaultdict(list)
    for day, ret, bucket in fired:
        by_day[day].append(ret)
        by_bucket[bucket].append(ret)

    pool_day = sample.by_day
    pool_bucket = sample.by_bucket
    if solo_only:
        pool_day = defaultdict(list)
        pool_bucket = defaultdict(list)
        for day, ret, bucket, _, _, solo in sample.rows:
            if solo:
                pool_day[day].append(ret)
                pool_bucket[bucket].append(ret)

    base_daily = [fmean(v) for v in pool_day.values() if v]
    daily = [fmean(v) for v in by_day.values() if v]
    raw = fmean(daily) - fmean(base_daily) if daily and base_daily else 0.0
    noise = 2 * stdev(daily) / (len(daily) ** 0.5) if len(daily) > 1 else 0.0

    total = sum(len(v) for v in by_bucket.values())
    rule = sum(fmean(v) * len(v) for v in by_bucket.values() if v) / total
    peer = sum(
        fmean(pool_bucket[b]) * len(v)
        for b, v in by_bucket.items()
        if v and pool_bucket[b]
    ) / total
    return len(fired), raw, rule - peer, noise


def counts_table(samples: dict[int, Sample]) -> None:
    print("Сигналов по порогам (горизонт их число не меняет — только исход)")
    print(f"{'порог':>8}" + "".join(f"{e:>16}" for e in EVENTS))
    sample = samples[HORIZONS[0]]
    for multiple in MULTIPLES:
        cells = [f"{len(sample.fires(e, multiple)):>16}" for e in EVENTS]
        print(f"{'x' + format(multiple, 'g'):>8}" + "".join(cells))
    print()


def grid(samples: dict[int, Sample], *, controls: bool) -> None:
    what = "ПОСЛЕ КОНТРОЛЯ по ходу цены" if controls else "БЕЗ контроля (сырая)"
    print(f"Разница с обычным днём, {what}, п.п. за горизонт")
    print(f"Ищем ПЛАТО, а не лучшую клетку. Прочерк — меньше {MIN_SIGNALS} сигналов.\n")
    for event in EVENTS:
        print(f"  {event}")
        print(f"{'порог':>8}" + "".join(f"{str(h) + 'd':>11}" for h in HORIZONS))
        for multiple in MULTIPLES:
            cells = []
            for horizon in HORIZONS:
                n, raw, ctrl, _ = controlled(samples[horizon], event, multiple)
                value = ctrl if controls else raw
                cells.append(f"{value:>+11.2f}" if n >= MIN_SIGNALS else f"{'—':>11}")
            print(f"{'x' + format(multiple, 'g'):>8}" + "".join(cells))
        print()


def honest(samples: dict[int, Sample]) -> None:
    """Вердикт по НЕПЕРЕКРЫВАЮЩИМСЯ наблюдениям: эффект против собственного шума.

    Перекрытие завышает число наблюдений и потому занижает шум — на горизонте 20 суток
    в двадцать раз. Пока эффект не больше шума, посчитанного ЗДЕСЬ, находки нет.
    """
    print("ВЕРДИКТ по неперекрывающимся наблюдениям (эффект после контроля / 2σ шума)")
    print(f"Клетка засчитывается, только если эффект больше и шума, и издержек "
          f"({COST_PCT:.2f}%).")
    print()
    for event in EVENTS:
        print(f"  {event}")
        print(f"{'порог':>8}" + "".join(f"{str(h) + 'd':>18}" for h in HORIZONS))
        for multiple in MULTIPLES:
            cells = []
            for horizon in HORIZONS:
                n, _, ctrl, noise = controlled(
                    samples[horizon], event, multiple, solo_only=True
                )
                if n < MIN_SIGNALS:
                    cells.append(f"{'—':>18}")
                    continue
                mark = "*" if ctrl > noise and ctrl > COST_PCT else " "
                cells.append(f"{ctrl:>+8.2f} /{noise:>6.2f}{mark}")
            print(f"{'x' + format(multiple, 'g'):>8}" + "".join(cells))
        print()
    print("  * — эффект превышает собственный шум. Без звёздочки находки нет.")
    print()


def detail(samples: dict[int, Sample], event: str, horizon: int, multiple: float) -> None:
    sample = samples[horizon]
    n, raw, ctrl, noise = controlled(sample, event, multiple)
    print(f"Клетка: {event}, горизонт {horizon} сут, порог x{multiple:g}")
    print(f"  сигналов {n}, сырая разница {raw:+.3f}, после контроля {ctrl:+.3f}, "
          f"2σ шума {noise:.3f}")
    by_year: dict[int, list[float]] = defaultdict(list)
    for day, ret, _ in sample.fires(event, multiple):
        by_year[day.year].append(ret)
    print("  по годам (разница с обычным днём того же года):")
    for year in sorted(sample.by_year):
        vals = by_year.get(year, [])
        base = fmean(sample.by_year[year])
        cell = f"{fmean(vals) - base:+.2f}" if len(vals) >= 10 else "—"
        print(f"    {year}: {cell:>8}  (наблюдений {len(vals)})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data")
    ap.add_argument("--days", type=int, default=1500)
    ap.add_argument("--detail", action="store_true", help="разбор одной клетки по годам")
    ap.add_argument("--event", default="вынос шортов")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--multiple", type=float, default=3.0)
    args = ap.parse_args()

    key = environment().get("COINALYZE_API_KEY", "").strip()
    if not key:
        print("COINALYZE_API_KEY не задан", file=sys.stderr)
        return 2

    samples, covered = collect(args.root, key, args.days)
    if not covered:
        print("данных нет", file=sys.stderr)
        return 1
    print(f"Ряды: {', '.join(covered)}")
    print(f"Всплеск — выше медианы за {NORM_DAYS} дней; вход по следующему открытию; "
          f"круг по издержкам {COST_PCT:.2f}%\n")
    counts_table(samples)
    grid(samples, controls=False)
    grid(samples, controls=True)
    honest(samples)
    if args.detail:
        detail(samples, args.event, args.horizon, args.multiple)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
