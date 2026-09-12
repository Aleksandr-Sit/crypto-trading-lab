#!/usr/bin/env python
"""Сколько стоит держать ШОРТ листинга тридцать дней — фандинг по факту.

Замер эффекта листинга (`scripts/listing_effect.py`) дал +16.2% за месяц на торгуемом
подмножестве. Это доход ДО платы за удержание, а у бессрочного контракта она есть:
каждые восемь часов стороны обмениваются ставкой. Для шорта положительная ставка —
доход (платят лонги), отрицательная — расход.

У свежих перпов ставка бывает экстремальной в обе стороны, и её знак заранее неизвестен:
разогнанную монету лонгуют с плечом (ставка высоко положительная, шорту платят),
но если все бросаются шортить — ставка уходит в минус и платит уже шорт.

Качаются только месяцы вокруг входа, а не вся история: 194 монеты по 66 месяцев — это
почти тринадцать тысяч запросов к архиву, а нужно две сотни.

    python scripts/listing_funding.py --root /app/data
    python scripts/listing_funding.py --days 30 --root /app/data
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import sqrt
from pathlib import Path
from statistics import fmean, median, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.backfill_cex import FundingArchive  # noqa: E402


def window_rates(
    archive: FundingArchive, base: str, start: datetime, end: datetime
) -> list[tuple[datetime, Decimal]]:
    """Ставки за окно [start, end). Отсутствие месяца в архиве — не ошибка."""
    instrument = f"{base}/USDT:USDT"
    try:
        return archive.history(instrument, start, end)
    except Exception:  # noqa: BLE001 — одна монета не роняет прогон
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listings", default="listings-tradable.json")
    ap.add_argument("--days", type=int, default=30, help="горизонт удержания шорта")
    ap.add_argument("--limit", type=int, default=0, help="сколько монет взять (0 — все)")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    path = root / args.listings if not Path(args.listings).is_absolute() else Path(args.listings)
    rows = json.loads(path.read_text())
    if args.limit:
        rows = rows[: args.limit]
    archive = FundingArchive()

    totals: list[float] = []
    by_year: dict[int, list[float]] = {}
    counts: list[int] = []
    missing = 0
    print(f"монет в выборке: {len(rows)}, горизонт {args.days} дн")
    for i, row in enumerate(rows, 1):
        entry = date.fromisoformat(row["entry"])
        start = datetime(entry.year, entry.month, entry.day, tzinfo=UTC)
        end = start + timedelta(days=args.days)
        rates = window_rates(archive, row["base"], start, end)
        if not rates:
            missing += 1
            continue
        # Сумма ставок за окно, в процентах. Для ШОРТА это знак дохода как есть:
        # положительная ставка означает, что лонги платят шортам.
        total = float(sum(r for _, r in rates)) * 100
        totals.append(total)
        counts.append(len(rates))
        by_year.setdefault(entry.year, []).append(total)
        if i % 40 == 0:
            print(f"  {i}/{len(rows)}")

    if len(totals) < 20:
        print(f"\nставок нашлось только у {len(totals)} монет — мало для вывода")
        return 0
    se = stdev(totals) / sqrt(len(totals)) if len(totals) > 1 else 0.0
    print(f"\nставки нашлись у {len(totals)} монет, не нашлось у {missing}")
    print(f"выплат за окно: медиана {median(counts):.0f} (ожидается {args.days * 3})")
    print(f"\nФАНДИНГ ЗА {args.days} ДНЕЙ ШОРТА, % от номинала")
    print(
        f"  средняя {fmean(totals):+.2f}   медиана {median(totals):+.2f}"
        f"   шум (2σ) ±{2 * se:.2f}"
    )
    vals = sorted(totals)
    qs = [(0.05, "5%"), (0.25, "25%"), (0.75, "75%"), (0.95, "95%")]
    print("  " + "   ".join(f"{lab}: {vals[int(len(vals) * q)]:+.2f}" for q, lab in qs))
    good = sum(1 for t in totals if t > 0) / len(totals) * 100
    print(f"  шорту ПЛАТЯТ в {good:.0f}% случаев")

    print(f"\n{'год':7}{'монет':>8}{'средняя':>10}{'медиана':>10}")
    for year in sorted(by_year):
        v = by_year[year]
        print(f"{year:<7}{len(v):>8}{fmean(v):>9.2f}%{median(v):>9.2f}%")
    print(
        "\nЧитать так: положительное число — это ДОХОД шорта сверх движения цены,\n"
        "отрицательное — расход. Складывать с результатом замера эффекта листинга."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
