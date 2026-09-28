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
DATES_FILE = "listing-dates.json"


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
    # До докачки, а не после: иначе старый файл стоил бы часа загрузок перед отказом.
    missing = [r["base"] for r in rows if "listed" not in r]
    if missing:
        print(
            f"в {args.listings} нет дня листинга у {len(missing)} монет (файл старше "
            "28.09.2026) — пересобрать: listing_effect.py --dump-tradable"
        )
        return 1
    # Глубина СВОЯ у каждого инструмента. Общая глубина «от самого раннего листинга»
    # заставляла бы качать монете, вышедшей в 2026-м, четыре года пустых архивов:
    # на 194 инструментах это тысячи лишних файлов и часы работы впустую.
    today = datetime.now(UTC).date()
    plan: dict[str, int] = {}
    for r in rows:
        name = f"{r['base']}/USDT:USDT"
        need = (today - date.fromisoformat(r["entry"])).days + 40
        plan[name] = max(plan.get(name, 0), need)
    names = sorted(plan)
    print(f"инструментов: {len(names)}, глубина от {min(plan.values())} до {max(plan.values())} дн")

    (root / UNIVERSE_FILE).write_text("\n".join(names) + "\n")
    print(f"вселенная записана: {root / UNIVERSE_FILE}")

    store = CandleStore(root)
    fs = FundingStore(root)
    bars_written = 0
    rates_written = 0
    failed: list[str] = []
    for i, name in enumerate(names, 1):
        days = plan[name]
        if not args.skip_candles:
            try:
                for res in backfill_venue(store, "binance", [name], args.tf, days):
                    bars_written += res.rows_written
                    if res.error:
                        failed.append(f"свечи {name}: {res.error}")
            except Exception as err:  # noqa: BLE001 — один инструмент не роняет прогон
                failed.append(f"свечи {name}: {type(err).__name__}")
        if not args.skip_funding:
            try:
                for _, n, err in backfill_funding(fs, "binance", [name], days):
                    rates_written += n
                    if err:
                        failed.append(f"фандинг {name}: {err}")
            except Exception as err:  # noqa: BLE001
                failed.append(f"фандинг {name}: {type(err).__name__}")
        if i % 20 == 0:
            print(f"  {i}/{len(names)}: свечей {bars_written}, ставок {rates_written}")

    # Даты листинга уходят в ПАРАМЕТРЫ стратегии, а не определяются по потоку: у этой
    # выборки перп запущен раньше спота, и первый бар перпа — другое событие.
    # Пишется день ЛИСТИНГА (`listed`), а не первый полный день (`entry`): стратегия сама
    # отсчитывает от него сутки. До 28.09.2026 здесь стоял `entry`, и вход шёл на сутки
    # позже карточки (`scripts/listing_dates_fix.py`). Наличие `listed` проверено в начале.
    dates = {f"{r['base']}/USDT:USDT": r["listed"] for r in rows}
    (root / DATES_FILE).write_text(json.dumps(dates, indent=1, sort_keys=True))
    print(f"даты листинга записаны: {root / DATES_FILE} ({len(dates)})")

    print(f"\nсвечей записано {bars_written}, ставок {rates_written}")
    if failed:
        print(f"не получилось у {len(failed)}:")
        for line in failed[:10]:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
