#!/usr/bin/env python
"""Разница фандинга между биржами (кандидат B3): хватает ли её на издержки входа.

Идея: если Binance платит шортам больше, чем Bybit, встают в лонг там, где платят меньше,
и в шорт там, где платят больше. Позиция рыночно-нейтральна, доход — разница ставок.

Вопрос не в том, бывает ли разница (бывает всегда), а в том, **окупает ли она вход**.
Позиция открывается четырьмя сделками (две ноги на двух биржах) и закрывается ещё четырьмя;
по тейкеру это около 0.2% от номинала. Разница в 5% годовых отбивает такой круг за две
недели — и всё это время она должна сохраняться. Поэтому здесь считаются три вещи подряд:

1. насколько велика разница в годовых;
2. сколько дней её надо держать, чтобы выйти в ноль по издержкам;
3. **сохраняется ли она столько**, — то есть какой она оказывается на самом деле после
   входа, а не в момент, когда мы её заметили.

Третий пункт решает всё: разница возникает на всплеске, а всплеск по определению спадает.

    python scripts/funding_spread.py --root /app/data
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean, median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.funding import FundingStore  # noqa: E402

DEFAULT_BASES = "BTC,ETH,SOL,XRP,DOGE,BNB,ADA,AVAX,LINK"
ROUND_TRIP = 0.20  # четыре ноги по тейкеру, % от номинала
PER_YEAR = 3 * 365  # выплат фандинга в году


def annual(rate: float) -> float:
    return rate * PER_YEAR * 100


def _hour(ts: datetime) -> datetime:
    """Момент расчёта с точностью до часа: миллисекунды у бирж свои."""
    return ts.replace(minute=0, second=0, microsecond=0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bases", default=DEFAULT_BASES)
    ap.add_argument("--a", default="binance")
    ap.add_argument("--b", default="bybit")
    ap.add_argument("--days", type=int, default=900)
    ap.add_argument("--hold", default="3,9,45", help="сколько выплат держим (8ч каждая)")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    fs = FundingStore(args.root)
    to = datetime.now(UTC)
    window = (to - timedelta(days=args.days), to)
    holds = [int(h) for h in args.hold.split(",") if h.strip()]

    # Наблюдение — момент расчёта: девять монет в одну минуту платят согласованно.
    per_moment: dict[datetime, list[float]] = defaultdict(list)
    series: dict[str, list[tuple[datetime, float]]] = {}
    print(f"{'инструмент':14}{'выплат':>8}{'|разница| годовых':>19}{'медиана':>10}")
    for base in [b.strip() for b in args.bases.split(",") if b.strip()]:
        name = f"{base}/USDT:USDT"
        # Метки бирж совпадают по смыслу, но не побайтово: у Binance в архиве
        # «08:00:00.007». Сравнение на точное равенство давало ПУСТОЕ пересечение
        # при полностью совпадающих данных — округляем к часу расчёта.
        ra = {_hour(r.ts): float(r.rate) for r in fs.read(args.a, name, *window)}
        rb = {_hour(r.ts): float(r.rate) for r in fs.read(args.b, name, *window)}
        common = sorted(set(ra) & set(rb))
        if len(common) < 100:
            print(f"{base:14}{len(common):>8}   мало общих выплат")
            continue
        diffs = [(ts, ra[ts] - rb[ts]) for ts in common]
        series[base] = diffs
        for ts, d in diffs:
            per_moment[ts].append(d)
        absann = [abs(annual(d)) for _, d in diffs]
        print(f"{base:14}{len(common):>8}{fmean(absann):>18.2f}%{median(absann):>9.2f}%")

    if not series:
        print("\nнет пересечения по данным — сначала собрать фандинг второй биржи")
        return 1

    pooled = sorted((ts, fmean(v)) for ts, v in per_moment.items() if v)
    absann = sorted(abs(annual(d)) for _, d in pooled)
    print(f"\nвсего моментов расчёта: {len(pooled)} ({pooled[0][0]:%Y-%m-%d} … {pooled[-1][0]:%Y-%m-%d})")
    for q, label in ((0.5, "медиана"), (0.75, "верхняя четверть"), (0.9, "верхняя десятая")):
        v = absann[int(len(absann) * q)]
        days = ROUND_TRIP / v * 365 if v > 0 else float("inf")
        print(f"  {label:18}: |разница| {v:>6.2f}% годовых → окупает вход за {days:>5.1f} дн")

    print(f"\nЧто разница даёт НА САМОМ ДЕЛЕ после входа (верхняя десятая часть всплесков)")
    print(f"{'держим':10}{'дней':>7}{'разница в момент входа':>25}{'реально получено':>19}{'доля':>8}")
    edge = absann[int(len(absann) * 0.9)]
    for h in holds:
        got: list[float] = []
        entry: list[float] = []
        for diffs in series.values():
            for i in range(len(diffs) - h):
                now = abs(annual(diffs[i][1]))
                if now < edge:
                    continue
                sign = 1 if diffs[i][1] > 0 else -1
                # Заходим по знаку сегодняшней разницы и держим h выплат.
                got.append(fmean(sign * annual(d) for _, d in diffs[i : i + h]))
                entry.append(now)
        if not got:
            continue
        realized = fmean(got)
        share = realized / fmean(entry) * 100 if entry else 0
        print(
            f"{h:<10}{h / 3:>7.1f}{fmean(entry):>24.2f}%{realized:>18.2f}%{share:>7.0f}%"
        )
    print(
        f"\nИздержки входа — {ROUND_TRIP}% от номинала (четыре ноги по тейкеру), и это «реально\n"
        "получено» надо сравнивать с ними, а не с разницей в момент входа. Доля меньше\n"
        "половины означает, что всплеск спадает быстрее, чем мы успеваем его собрать."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
