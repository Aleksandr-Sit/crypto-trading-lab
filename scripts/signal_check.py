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

    python scripts/signal_check.py --metric taker_ratio
    python scripts/signal_check.py --metric top_positions_ratio --bases BTC,ETH,SOL
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

DEFAULT_BASES = "BTC,ETH,SOL,XRP,DOGE,AVAX,LINK,ADA"
METRICS = (
    "open_interest",
    "open_interest_change",  # считается здесь: суточное изменение интереса, %
    "top_accounts_ratio",
    "top_positions_ratio",
    "accounts_ratio",
    "taker_ratio",
)


def series(
    ps: PositioningStore,
    cs: CandleStore,
    name: str,
    window: tuple[datetime, datetime],
    metric: str,
    horizons: list[int],
) -> list[tuple[float, dict[int, float]]]:
    """Пары «значение показателя за день → доходности вперёд»."""
    days = ps.daily("binance", name, *window)
    bars = {c.ts: c.close for c in cs.read("binance", name, "1d", *window)}
    rows = [d for d in days if d["ts"] in bars]
    out: list[tuple[float, dict[int, float]]] = []
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
        forward = {
            h: float((bars[rows[i + h]["ts"]] - price) / price * 100) for h in horizons
        }
        out.append((value, forward))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metric", default="taker_ratio", choices=METRICS)
    ap.add_argument("--bases", default=DEFAULT_BASES)
    ap.add_argument("--horizons", default="1,3,5,10")
    ap.add_argument("--tail-pct", type=float, default=10.0, help="размер хвоста, %%")
    ap.add_argument("--days", type=int, default=1480)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    to = datetime.now(UTC)
    window = (to - timedelta(days=args.days), to)
    ps, cs = PositioningStore(args.root), CandleStore(args.root)

    pooled: list[tuple[float, dict[int, float]]] = []
    for base in [b.strip() for b in args.bases.split(",") if b.strip()]:
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
    values = sorted(v for v, _ in pooled)
    k = max(1, int(len(values) * args.tail_pct / 100))
    low_edge, high_edge = values[k], values[-k]
    low = [f for v, f in pooled if v <= low_edge]
    high = [f for v, f in pooled if v >= high_edge]

    print(f"\nпоказатель: {args.metric} | дней {len(pooled)}")
    print(f"нижние {args.tail_pct:.0f}%: значение ≤ {low_edge:.4f} ({len(low)} дней)")
    print(f"верхние {args.tail_pct:.0f}%: значение ≥ {high_edge:.4f} ({len(high)} дней)\n")
    print(f"{'горизонт':10}{'нижний хвост':>15}{'верхний хвост':>16}{'обычный день':>15}{'разброс':>11}")
    for h in horizons:
        lo = mean(f[h] for f in low)
        hi = mean(f[h] for f in high)
        al = mean(f[h] for _, f in pooled)
        print(f"{h:>4} дн   {lo:>14.2f}%{hi:>15.2f}%{al:>14.2f}%{hi - lo:>10.2f}")
    print(
        "\nЧитать так: разброс между хвостами — это ВСЁ, что показатель обещает.\n"
        "Меньше двух-трёх десятых процента — круг по издержкам съест его целиком."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
