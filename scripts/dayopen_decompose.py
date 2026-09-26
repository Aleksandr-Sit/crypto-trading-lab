#!/usr/bin/env python
"""Раскладка сделки «гашения первого часа суток UTC» на цену, фандинг и издержки по годам.

Зачем (26.09.2026). Прямая проверка (`price_signal_check`, `day_fade`) дала +0.244% на
сделку при шуме ±0.143 и порог издержек по кругу 0.10%; замер движком той же стратегии
(`cex-perp-paper-day-open-fade`, замер на 2160 сутках) — −14.6% за окно и отказ. Чтобы
понять, что съело эффект, каждая сделка правила пересчитывается прямо по свечам — те же
моменты, что у движка: решение по закрытию 00:45–01:00, вход по открытию 01:00, выход
по открытию 00:00 следующих суток — и делится на три части:

* цена — ход от входа к выходу со знаком позиции;
* фандинг — выплаты строго между входом и выходом (в 00:00 позиция уже закрыта:
  движок сначала исполняет выход, потом начисляет фандинг), лонг платит положительную ставку;
* издержки — круг по двум ставкам: как в `config/costs.yaml` для Bybit (тейкер 10 б.п.
  и спред 5 б.п. на сторону) и как в прямой проверке (0.10% за круг).

Шум — по датам: восемь монет в одни сутки — одно наблюдение (среднее по монетам за день).

    python scripts/dayopen_decompose.py --root /app/data
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from math import sqrt
from pathlib import Path
from statistics import fmean, pstdev

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab.data.funding import FundingStore  # noqa: E402
from lab.data.store import CandleStore  # noqa: E402

INSTRUMENTS = [
    f"{c}/USDT:USDT" for c in ("BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "DOGE", "LINK")
]
COST_ENGINE = 0.30  # % за круг: 2 × (10 б.п. тейкер + 5 б.п. спред), config/costs.yaml
COST_DIRECT = 0.10  # % за круг: порог прямой проверки
T0 = datetime(2020, 1, 1, tzinfo=UTC)
T1 = datetime(2027, 1, 1, tzinfo=UTC)


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def trades(store: CandleStore, funding: FundingStore, venue: str, instrument: str) -> list[dict]:
    """Сделки правила по одному инструменту: день, сторона, цена, фандинг — в процентах."""
    need = {time(0, 0), time(0, 45), time(1, 0)}
    bars: dict[datetime, tuple[float, float]] = {}
    for row in store.query(
        "select ts, open, close from {candles} order by ts", venue, instrument, "15m"
    ):
        ts = _utc(row["ts"])
        if ts.time() in need:
            bars[ts] = (float(row["open"]), float(row["close"]))
    rates = [(_utc(r.ts), float(r.rate)) for r in funding.read(venue, instrument, T0, T1)]
    out: list[dict] = []
    days = sorted({ts.date() for ts in bars})
    j = 0
    for d in days:
        start = datetime(d.year, d.month, d.day, tzinfo=UTC)
        first, lead, entry = (
            bars.get(start),
            bars.get(start + timedelta(minutes=45)),
            bars.get(start + timedelta(hours=1)),
        )
        exit_ = bars.get(start + timedelta(days=1))
        if not (first and lead and entry and exit_) or first[0] <= 0 or entry[0] <= 0:
            continue
        move = lead[1] / first[0] - 1
        if move == 0:
            continue
        side = -1 if move > 0 else 1  # вырос — шорт
        price = side * (exit_[0] / entry[0] - 1) * 100
        enter_at, exit_at = start + timedelta(hours=1), start + timedelta(days=1)
        while j < len(rates) and rates[j][0] <= enter_at:
            j += 1
        k, paid = j, 0.0
        while k < len(rates) and rates[k][0] < exit_at:
            paid += rates[k][1]
            k += 1
        out.append(
            {
                "day": d,
                "instrument": instrument,
                "price": price,
                "funding": -side * paid * 100,  # лонг платит положительную ставку
                "side": side,
            }
        )
    return out


def _by_date(rows: list[dict], key: str) -> tuple[float, float, int]:
    """Среднее и 2σ по независимым датам: сначала среднее по монетам за день."""
    daily: dict = defaultdict(list)
    for r in rows:
        daily[r["day"]].append(r[key])
    means = [fmean(v) for v in daily.values()]
    if len(means) < 2:
        return (means[0] if means else 0.0), 0.0, len(means)
    return fmean(means), 2 * pstdev(means) / sqrt(len(means)), len(means)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default="data")
    ap.add_argument("--venue", default="bybit")
    args = ap.parse_args()
    store, funding = CandleStore(args.root), FundingStore(args.root)

    rows: list[dict] = []
    for instrument in INSTRUMENTS:
        got = trades(store, funding, args.venue, instrument)
        first = got[0]["day"] if got else "—"
        print(f"{instrument:16} сделок {len(got):5}  с {first}")
        rows += got
    for r in rows:
        r["net_engine"] = r["price"] + r["funding"] - COST_ENGINE
        r["net_direct"] = r["price"] + r["funding"] - COST_DIRECT

    print(
        "\nВсе величины — % на сделку, среднее по датам; ± — шум 2σ по датам.\n"
        f"«чистыми» — цена + фандинг − круг: {COST_ENGINE}% (модель движка) и "
        f"{COST_DIRECT}% (порог прямой проверки).\n"
    )
    head = (
        f"{'год':6} {'дат':>5} {'сделок':>6}  {'цена':>15}  {'фандинг':>8}  "
        f"{'чистыми 0.30':>13}  {'чистыми 0.10':>13}"
    )
    print(head)
    years = sorted({r["day"].year for r in rows})
    for label, part in [(str(y), [r for r in rows if r["day"].year == y]) for y in years] + [
        ("всё", rows)
    ]:
        p, pn, n = _by_date(part, "price")
        f, _, _ = _by_date(part, "funding")
        e, _, _ = _by_date(part, "net_engine")
        dnet, _, _ = _by_date(part, "net_direct")
        print(
            f"{label:6} {n:5} {len(part):6}  {p:+7.3f} ±{pn:5.3f}  {f:+8.3f}  "
            f"{e:+13.3f}  {dnet:+13.3f}"
        )

    print("\nПо инструментам (всё время):")
    print(f"{'инструмент':16} {'сделок':>6}  {'цена':>8}  {'фандинг':>8}  {'шорт/лонг':>10}")
    for instrument in INSTRUMENTS:
        part = [r for r in rows if r["instrument"] == instrument]
        if not part:
            continue
        shorts = sum(1 for r in part if r["side"] < 0)
        print(
            f"{instrument:16} {len(part):6}  {fmean(r['price'] for r in part):+8.3f}  "
            f"{fmean(r['funding'] for r in part):+8.3f}  {shorts:>4}/{len(part) - shorts:<5}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
