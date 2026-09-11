#!/usr/bin/env python
"""Есть ли отскок после ВЫМЫВАНИЯ ПЛЕЧА — проверка гипотезы напрямую по данным.

Зачем отдельно от замера стратегии. Замер отвечает «сколько заработали бы правила»
и молчит о том, есть ли эффект вообще: при восемнадцати сделках вердикт всегда будет
`insufficient`, и непонятно, мало событий или нет сигнала. Здесь считается сам эффект —
доходность после события против доходности обычного дня, без издержек и без стопа.

Сравнение именно с ОБЫЧНЫМ ДНЁМ, а не с нулём: у рынка есть свой дрейф, и «+1% за пять
дней» ничего не значит, пока не известно, что обычная пятидневка даёт +0.6%.

    python scripts/flush_events.py
    python scripts/flush_events.py --oi-drop 5 --price-drop 3 --horizons 1,3,5,10
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.positioning import PositioningStore  # noqa: E402
from lab.data.store import CandleStore  # noqa: E402

DEFAULT = "BTC,ETH,SOL,XRP,DOGE,AVAX,LINK,ADA"


def events(
    ps: PositioningStore,
    cs: CandleStore,
    name: str,
    window: tuple[datetime, datetime],
    oi_drop: float,
    price_drop: float,
    horizons: list[int],
) -> tuple[list[datetime], dict[int, list[float]], dict[int, list[float]]]:
    """Даты вымываний, доходности после них и доходности всех дней."""
    days = ps.daily("binance", name, *window)
    bars = {c.ts: c.close for c in cs.read("binance", name, "1d", *window)}
    stamps = [d["ts"] for d in days if d["ts"] in bars]
    oi = {d["ts"]: float(d["open_interest"]) for d in days}
    hits: list[datetime] = []
    after: dict[int, list[float]] = {h: [] for h in horizons}
    usual: dict[int, list[float]] = {h: [] for h in horizons}
    longest = max(horizons)
    for i in range(1, len(stamps) - longest):
        now, prev = stamps[i], stamps[i - 1]
        if oi.get(prev, 0) <= 0 or bars[prev] <= 0:
            continue
        shrank = (oi[prev] - oi[now]) / oi[prev] * 100
        fell = float((bars[prev] - bars[now]) / bars[prev] * 100)
        # Оба условия обязательны: сжатие интереса на растущей цене — фиксация прибыли,
        # а не принудительные закрытия.
        hit = shrank >= oi_drop and fell >= price_drop
        if hit:
            hits.append(now)
        for h in horizons:
            ret = float((bars[stamps[i + h]] - bars[now]) / bars[now] * 100)
            usual[h].append(ret)
            if hit:
                after[h].append(ret)
    return hits, after, usual


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bases", default=DEFAULT)
    ap.add_argument("--oi-drop", type=float, default=5.0, help="сжатие интереса за сутки, %%")
    ap.add_argument("--price-drop", type=float, default=3.0, help="падение цены за сутки, %%")
    ap.add_argument("--horizons", default="1,3,5,10")
    ap.add_argument("--days", type=int, default=1480)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    to = datetime.now(UTC)
    window = (to - timedelta(days=args.days), to)
    ps, cs = PositioningStore(args.root), CandleStore(args.root)

    pooled_after: dict[int, list[float]] = {h: [] for h in horizons}
    pooled_usual: dict[int, list[float]] = {h: [] for h in horizons}
    all_dates: set[datetime] = set()
    total = 0
    print(f"порог: интерес −{args.oi_drop}%, цена −{args.price_drop}% за сутки\n")
    print(f"{'инструмент':12}{'событий':>9}   " + "".join(f"{h:>4}дн" for h in horizons))
    for base in [b.strip() for b in args.bases.split(",") if b.strip()]:
        name = f"{base}/USDT:USDT"
        try:
            hits, after, usual = events(
                ps, cs, name, window, args.oi_drop, args.price_drop, horizons
            )
        except Exception as err:  # noqa: BLE001 — нет данных по инструменту, не падаем
            print(f"{base:12}{'—':>9}   {type(err).__name__}")
            continue
        if not usual[horizons[0]]:
            print(f"{base:12}{'—':>9}   нет данных")
            continue
        total += len(hits)
        all_dates.update(hits)
        for h in horizons:
            pooled_after[h] += after[h]
            pooled_usual[h] += usual[h]
        cells = "".join(f"{mean(after[h]):>6.1f}" if after[h] else f"{'—':>6}" for h in horizons)
        print(f"{base:12}{len(hits):>9}   {cells}")

    if not total:
        print("\nсобытий не нашлось")
        return 0
    print(f"\nвсего событий {total}, из них РАЗНЫХ дат {len(all_dates)}")
    print("  (совпадающие даты — одно рыночное событие, а не независимые наблюдения)\n")
    print(f"{'горизонт':10}{'после вымывания':>17}{'обычный день':>15}{'разница':>10}{'доля роста':>13}")
    for h in horizons:
        a, u = pooled_after[h], pooled_usual[h]
        up = sum(1 for x in a if x > 0) * 100 / len(a)
        print(
            f"{h:>4} дн   {mean(a):>16.2f}%{mean(u):>14.2f}%{mean(a) - mean(u):>9.2f}{up:>12.0f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
