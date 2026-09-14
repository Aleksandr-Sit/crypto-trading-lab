#!/usr/bin/env python
"""Предсказывают ли крупные ликвидации отскок? Прямая проверка на данных Coinalyze.

Ликвидации — единственный ряд, которого нет в архивах бирж, и ради него заводился ключ.
Гипотеза стандартная и правдоподобная: массовый вынос лонгов — это вынужденные продажи,
после которых давление уходит, и цена отскакивает.

**Контроль здесь важнее самой проверки.** Ликвидации лонгов случаются РОВНО ТОГДА, когда
цена падает. Значит «после ликвидаций цена растёт» может быть просто «после падения цена
растёт» — тот же капкан, в котором на этой неделе погибли TDSequential и BbandRsi
(`docs/research/freqtrade-2026-09-14.md`). Поэтому считается и то, и другое:

1. доходность вперёд после всплеска против ОБЫЧНОГО дня;
2. она же против обычного дня С ТАКИМ ЖЕ ходом цены (корзины по недавнему движению).

Всплеск нормируется скользящей медианой за 30 дней: объём ликвидаций растёт вместе
с рынком, и «10 миллионов» в 2022 и в 2026 — разные события.

    python scripts/liquidation_check.py --root /app/data --horizon 5
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
# Монета → перпы главных площадок (суммируем сами: агрегата у источника нет)
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data")
    ap.add_argument("--horizon", type=int, default=5, help="суток вперёд")
    ap.add_argument("--days", type=int, default=1500)
    ap.add_argument("--multiple", type=float, default=3.0, help="во сколько раз выше медианы")
    args = ap.parse_args()

    key = environment().get("COINALYZE_API_KEY", "").strip()
    if not key:
        print("COINALYZE_API_KEY не задан", file=sys.stderr)
        return 2

    cs = CandleStore(args.root)
    base_days: dict[object, list[float]] = defaultdict(list)
    hit: dict[str, dict[object, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_bucket: dict[int, list[float]] = defaultdict(list)
    hit_bucket: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    counts: dict[str, int] = defaultdict(int)
    covered: list[str] = []
    # Разбивка по годам обязательна: поток тейкеров прошёл все проверки и развалился
    # именно здесь — весь плюс дали бычьи годы, а в падающие он терял больше обычного дня.
    by_year: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    base_year: dict[int, list[float]] = defaultdict(list)

    for asset, (symbols, instrument) in ASSETS.items():
        liq = liquidations(symbols, key, args.days)
        if not liq:
            print(f"{asset}: ликвидаций не получено", file=sys.stderr)
            continue
        rows = cs.query(
            "select ts, open::DOUBLE o, close::DOUBLE c from {candles} order by ts",
            "binance", instrument, "1d",
        )
        price = {r["ts"].date(): (float(r["o"]), float(r["c"])) for r in rows if r["c"]}
        days = sorted(d for d in liq if d in price)
        if len(days) < 200:
            print(f"{asset}: пересечение рядов мало ({len(days)})", file=sys.stderr)
            continue
        covered.append(f"{asset}({len(days)})")

        longs = [liq[d]["long"] for d in days]
        shorts = [liq[d]["short"] for d in days]
        med_long = rolling_median(longs, NORM_DAYS)
        med_short = rolling_median(shorts, NORM_DAYS)
        h = args.horizon

        for i in range(NORM_DAYS, len(days) - h - 1):
            day = days[i]
            entry = price[days[i + 1]][0]  # вход по СЛЕДУЮЩЕМУ открытию
            exit_ = price[days[i + 1 + h]][0]
            if entry <= 0:
                continue
            ret = (exit_ / entry - 1) * 100
            base_days[day].append(ret)
            moved = (price[day][1] / price[days[i - h]][1] - 1) * 100
            bucket = max(-10, min(10, int(moved // 2)))
            by_bucket[bucket].append(ret)

            checks = {
                "вынос лонгов": med_long[i] and longs[i] > med_long[i] * args.multiple,
                "вынос шортов": med_short[i] and shorts[i] > med_short[i] * args.multiple,
                "вынос обоих": (
                    med_long[i] and med_short[i]
                    and longs[i] > med_long[i] * args.multiple
                    and shorts[i] > med_short[i] * args.multiple
                ),
            }
            for name, fired in checks.items():
                if fired:
                    hit[name][day].append(ret)
                    hit_bucket[name][bucket].append(ret)
                    by_year[name][day.year].append(ret)
                    counts[name] += 1
            base_year[day.year].append(ret)

    if not covered:
        print("данных нет", file=sys.stderr)
        return 1

    print(f"Ряды: {', '.join(covered)}")
    print(f"Горизонт {args.horizon} суток, всплеск = выше медианы за {NORM_DAYS} дней "
          f"в {args.multiple:g} раза, вход по следующему открытию")
    base_daily = [fmean(v) for v in base_days.values() if v]
    base_mean = fmean(base_daily) if base_daily else 0.0
    print(f"обычный день: {base_mean:+.3f}% за горизонт, независимых дат {len(base_daily)}\n")

    print(f"{'событие':16}{'сигналов':>9}{'дат':>6}{'среднее':>10}{'против обычного':>17}"
          f"{'2σ шума':>10}  вердикт")
    for name in hit:
        daily = [fmean(v) for v in hit[name].values() if v]
        if len(daily) < 10:
            print(f"{name:16}{counts[name]:>9}{len(daily):>6}   наблюдений мало")
            continue
        mean = fmean(daily)
        diff = mean - base_mean
        noise = 2 * stdev(daily) / (len(daily) ** 0.5) if len(daily) > 1 else 0.0
        verdict = (
            "нет: внутри шума" if abs(diff) < noise
            else "нет: меньше издержек" if diff < COST_PCT
            else "ЕСТЬ СИГНАЛ"
        )
        print(f"{name:16}{counts[name]:>9}{len(daily):>6}{mean:>+10.3f}{diff:>+17.3f}"
              f"{noise:>10.3f}  {verdict}")

    print()
    print("Контроль: сравнение с обычным днём ПРИ ТОМ ЖЕ ходе цены")
    print(f"{'событие':16}{'событие':>10}{'обычный':>10}{'разница':>10}  вердикт")
    for name, buckets in hit_bucket.items():
        total = sum(len(v) for v in buckets.values())
        if total < 50:
            print(f"{name:16}   наблюдений мало ({total})")
            continue
        rule = sum(fmean(v) * len(v) for v in buckets.values() if v) / total
        peer = sum(
            fmean(by_bucket[b]) * len(v) for b, v in buckets.items() if v and by_bucket[b]
        ) / total
        diff = rule - peer
        verdict = "сводится к ходу цены" if diff < COST_PCT else "остаётся после контроля"
        print(f"{name:16}{rule:>+10.3f}{peer:>+10.3f}{diff:>+10.3f}  {verdict}")

    print()
    print("По ГОДАМ (согласованность по инструментам ничего не доказывает — монеты ходят вместе)")
    years = sorted(base_year)
    header = "".join(f"{y:>10}" for y in years)
    print(f"{'событие':16}{header}")
    print(f"{'обычный день':16}" + "".join(
        f"{fmean(base_year[y]):>+10.2f}" if base_year[y] else f"{'—':>10}" for y in years
    ))
    for name in by_year:
        cells = []
        for y in years:
            vals = by_year[name][y]
            if len(vals) >= 10:
                cells.append(f"{fmean(vals) - fmean(base_year[y]):>+10.2f}")
            else:
                cells.append(f"{'—':>10}")
        print(f"{name:16}" + "".join(cells))
    print("  Числа — РАЗНИЦА с обычным днём того же года. Прочерк: меньше 10 наблюдений.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
