#!/usr/bin/env python
"""Срочная кривая и ролл кэш-энд-керри (кандидат B2): где премия и когда её брать.

Кэш-энд-керри — единственная стратегия лаборатории с подтверждённым преимуществом
(+4.41% годовых, 8 окон из 8 в плюсе). Но мерилась она «купил и держал до гашения»,
а сама механика переноса между контрактами не мерилась ни разу. Здесь считается то,
от чего этот перенос зависит:

* **годовая премия по сроку до гашения.** Если у дальнего контракта премия в годовых выше,
  держать надо дальний; если она набирается в последние недели — ближний. Ответ определяет,
  когда катить позицию, и это единственное решение, которое в этой стратегии принимается.
* **сравнение с фандингом бессрочного.** Обе ноги берут одну и ту же премию за плечо
  (корреляция 0.91), но берут по-разному: у срочного она зафиксирована в цене на входе,
  у бессрочного плавает каждые восемь часов. Что из этого платило больше — вопрос замера.

Обе ноги читаются ЧАСОВЫМИ свечами и сопоставляются по ОДНОЙ метке времени. Сравнение цен
из разных часов уже давало ошибку в 7 процентных пунктов: −6.65% вместо +1.09%.

    python scripts/term_structure.py --root /app/data
    python scripts/term_structure.py --bases BTC --root /app/data
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.funding import FundingStore  # noqa: E402
from lab.data.store import CandleStore  # noqa: E402

SETTLE_HOUR = 8  # квартальные контракты Binance гасятся в 08:00 UTC
BUCKETS = ((90, 60), (60, 30), (30, 14), (14, 7), (7, 2), (2, 0))
CODE = re.compile(r"-(\d{6})$")


def expiries(root: Path, base: str) -> list[tuple[str, datetime]]:
    """Срочные контракты инструмента: имя ряда и момент гашения."""
    venue = root / "candles" / "venue=binance"
    prefix = f"{base}_USDT_USDT-"
    out: list[tuple[str, datetime]] = []
    for d in sorted(venue.glob(f"instrument={prefix}*")):
        m = CODE.search(d.name)
        if not m:
            continue
        code = m.group(1)
        when = datetime(
            2000 + int(code[:2]), int(code[2:4]), int(code[4:6]), SETTLE_HOUR, tzinfo=UTC
        )
        out.append((f"{base}/USDT:USDT-{code}", when))
    return sorted(out, key=lambda p: p[1])


def closes(cs: CandleStore, name: str, tf: str) -> dict[datetime, float]:
    rows = cs.query("select ts, close::DOUBLE as close from {candles}", "binance", name, tf)
    return {r["ts"]: float(r["close"]) for r in rows if r["close"]}


def bucket_of(days: float) -> str | None:
    for hi, lo in BUCKETS:
        if lo < days <= hi:
            return f"{lo}–{hi} дн"
    return None


def stats(vals: list[float]) -> tuple[float, float]:
    """Средняя и её удвоенная ошибка. Наблюдение — ДЕНЬ, а не часовой замер."""
    if not vals:
        return 0.0, 0.0
    m = fmean(vals)
    se = stdev(vals) / sqrt(len(vals)) if len(vals) > 1 else 0.0
    return m, 2 * se


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bases", default="BTC,ETH")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    cs, fs = CandleStore(root), FundingStore(root)

    for base in [b.strip() for b in args.bases.split(",") if b.strip()]:
        contracts = expiries(root, base)
        spot = closes(cs, f"{base}/USDT", "1h")
        if not contracts or not spot:
            print(f"{base}: нет данных")
            continue
        print(f"\n=== {base}: {len(contracts)} срочных контрактов, спот {len(spot)} часов ===")

        # По одному наблюдению в день: соседние часы одного контракта — не независимы.
        by_bucket: dict[str, dict[tuple[str, object], list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        by_year: dict[int, dict[tuple[str, object], list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for name, when in contracts:
            fut = closes(cs, name, "1h")
            for ts, px in fut.items():
                s = spot.get(ts)
                if not s or px <= 0:
                    continue
                days = (when - ts).total_seconds() / 86400
                if days <= 0:
                    continue
                ann = (px / s - 1) * 365 / days * 100
                slot = bucket_of(days)
                if slot:
                    by_bucket[slot][(name, ts.date())].append(ann)
                by_year[ts.year][(name, ts.date())].append(ann)

        print(f"\n{'до гашения':14}{'дней-контрактов':>17}{'премия, годовых':>18}{'шум (2σ)':>11}")
        for hi, lo in BUCKETS:
            slot = f"{lo}–{hi} дн"
            daily = [fmean(v) for v in by_bucket[slot].values()]
            if not daily:
                continue
            m, err = stats(daily)
            print(f"{slot:14}{len(daily):>17}{m:>17.2f}%{err:>11.2f}")

        print(f"\n{'год':7}{'дней-контрактов':>17}{'премия, годовых':>18}{'фандинг, годовых':>19}")
        for year in sorted(by_year):
            daily = [fmean(v) for v in by_year[year].values()]
            if not daily:
                continue
            start = datetime(year, 1, 1, tzinfo=UTC)
            end = datetime(year + 1, 1, 1, tzinfo=UTC)
            rates = fs.read("binance", f"{base}/USDT:USDT", start, end)
            fund = fmean(float(r.rate) for r in rates) * 3 * 365 * 100 if rates else 0.0
            print(f"{year:<7}{len(daily):>17}{fmean(daily):>17.2f}%{fund:>18.2f}%")

    print(
        "\nЧитать так: если премия в годовых РАСТЁТ к гашению — катить позицию поздно;\n"
        "если падает — катить рано, в дальний контракт. Сравнение с фандингом отвечает,\n"
        "стоило ли вообще брать срочный контракт вместо бессрочного."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
