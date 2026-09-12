#!/usr/bin/env python
"""Собрать данные перпов для замера стратегии листингов и составить их вселенную.

Прямой расчёт (`scripts/listing_effect.py`) шёл по СПОТОВЫМ рядам: там есть цена первого
дня и путь после него. Движку этого мало — торгуется бессрочный контракт, и мерить надо
его: у перпа свой ряд, свой стакан и свой фандинг, который и оказался главным расходом.

Перпы в хранилище собраны только по ликвидной сотне, поэтому монеты из выборки надо
докачать. Скрипт делает три вещи по порядку:

1. читает торгуемые листинги (`listings-tradable.json` из `listing_effect.py --dump`);
2. заказывает дневные свечи и ставки фандинга по их перпам;
3. пишет файл вселенной для регистрации стратегии.

Глубина берётся от САМОГО РАННЕГО листинга в выборке, а не «побольше на всякий случай»:
194 инструмента по шесть лет — это сотни лишних месячных архивов и час работы впустую.

    python scripts/listing_universe.py --root /app/data
    python scripts/listing_universe.py --limit 20 --root /app/data
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.backfill_cex import backfill_funding, backfill_venue  # noqa: E402
from lab.data.funding import FundingStore  # noqa: E402
from lab.data.store import CandleStore  # noqa: E402

UNIVERSE_FILE = "universe-listings.txt"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listings", default="listings-tradable.json")
    ap.add_argument("--limit", type=int, default=0, help="сколько монет взять (0 — все)")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--skip-candles", action="store_true")
    ap.add_argument("--skip-funding", action="store_true")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    rows = json.loads((root / args.listings).read_text())
    if args.limit:
        rows = rows[: args.limit]
    names = sorted({f"{r['base']}/USDT:USDT" for r in rows})
    earliest = min(date.fromisoformat(r["entry"]) for r in rows)
    days = (datetime.now(UTC).date() - earliest).days + 40
    print(f"инструментов: {len(names)}, самый ранний листинг {earliest}, глубина {days} дн")

    (root / UNIVERSE_FILE).write_text("\n".join(names) + "\n")
    print(f"вселенная записана: {root / UNIVERSE_FILE}")

    if not args.skip_candles:
        store = CandleStore(root)
        results = backfill_venue(store, "binance", names, args.tf, days)
        ok = sum(1 for r in results if r.error is None)
        rowsn = sum(r.rows_written for r in results)
        print(f"свечи: рядов {ok} из {len(results)}, строк записано {rowsn}")
        for r in results[:10]:
            if r.error:
                print(f"  {r.instrument}: {r.error}")

    if not args.skip_funding:
        fs = FundingStore(root)
        got = backfill_funding(fs, "binance", names, days)
        ok = sum(1 for _, _, err in got if err is None)
        total = sum(n for _, n, _ in got)
        print(f"фандинг: рядов {ok} из {len(got)}, ставок записано {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
