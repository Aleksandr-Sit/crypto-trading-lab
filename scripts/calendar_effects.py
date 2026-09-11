#!/usr/bin/env python
"""Календарные эффекты (кандидат C4): есть ли систематика во ВРЕМЕНИ, а не в цене.

Три гипотезы классической торговли, перенесённые на круглосуточный рынок:

* **час расчёта фандинга.** Расчёт в 00/08/16 UTC. Если ставка положительная, лонги платят,
  и часть из них закрывается ДО расчёта, чтобы не платить, — цена должна проседать в час
  перед расчётом и восстанавливаться после. Это единственный из трёх эффектов, у которого
  есть прямая денежная причина, а не «так сложилось».
* **день недели.** В выходные стакан тоньше, маркет-мейкеры уводят лимиты, движения резче.
* **экспирация опционов.** Deribit гасит месячные контракты в последнюю пятницу месяца,
  08:00 UTC; вокруг страйка с наибольшим интересом цену «прижимает».

Считается доходность бара `(close-open)/open`, сравнение — с ОБЫЧНЫМ баром, потому что
у рынка есть свой дрейф. Никаких издержек здесь нет: сначала надо понять, есть ли валовый
эффект вообще. Круг по тейкеру на перпах Binance — около 0.10%, по мейкеру около 0.04%;
эффект меньше этого не переживёт исполнения ни при каких правилах.

Разбор по годам печатается всегда. Поток тейкеров прошёл четыре проверки подряд и
развалился именно на годах, поэтому здесь она не опция.

    python scripts/calendar_effects.py --root /app/data
    python scripts/calendar_effects.py --part hours --bases BTC,ETH
"""

from __future__ import annotations

import argparse
import sys
from calendar import FRIDAY
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

DEFAULT_BASES = "BTC,ETH,SOL,XRP,DOGE,AVAX,LINK,ADA"
SETTLEMENT = (0, 8, 16)  # часы расчёта фандинга, UTC
BEFORE = tuple((h - 1) % 24 for h in SETTLEMENT)
DAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


class Acc:
    """Сумма и счёт вместо списка значений: рядов десятки, баров миллионы."""

    __slots__ = ("n", "total")

    def __init__(self) -> None:
        self.total = 0.0
        self.n = 0

    def add(self, x: float) -> None:
        self.total += x
        self.n += 1

    @property
    def mean(self) -> float:
        return self.total / self.n if self.n else 0.0


def bars(cs: CandleStore, name: str, tf: str, year: int):
    """Бары одного инструмента за один год — окно держит память ограниченной."""
    start = datetime(year, 1, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC)
    for c in cs.read("binance", name, tf, start, end):
        if c.open and c.open > 0:
            yield c.ts, float((c.close - c.open) / c.open * 100)


def last_friday(d: date) -> bool:
    """Последняя пятница месяца — день месячной экспирации на Deribit."""
    if d.weekday() != FRIDAY:
        return False
    return (d + timedelta(days=7)).month != d.month


def table(title: str, rows: list[tuple[str, Acc]], usual: Acc) -> None:
    print(f"\n{title}")
    print(f"{'группа':16}{'баров':>9}{'средняя':>11}{'против обычного':>18}")
    for label, acc in rows:
        if not acc.n:
            continue
        print(f"{label:16}{acc.n:>9}{acc.mean:>10.4f}%{acc.mean - usual.mean:>17.4f}")
    print(f"{'обычный бар':16}{usual.n:>9}{usual.mean:>10.4f}%")


def by_year(title: str, years: dict[int, tuple[Acc, Acc]]) -> None:
    """Эффект и фон по годам. Скачущий знак означает режим, а не закономерность."""
    print(f"\n{title} — ПО ГОДАМ")
    print(f"{'год':7}{'баров':>9}{'эффект':>11}{'фон':>11}{'разница':>11}")
    signs: list[float] = []
    for year in sorted(years):
        hit, usual = years[year]
        if not hit.n or not usual.n:
            continue
        diff = hit.mean - usual.mean
        signs.append(diff)
        print(f"{year:<7}{hit.n:>9}{hit.mean:>10.4f}%{usual.mean:>10.4f}%{diff:>11.4f}")
    if len(signs) < 2:
        return
    pos = sum(1 for s in signs if s > 0)
    print(f"знак совпадает в {max(pos, len(signs) - pos)} годах из {len(signs)}")


def hours(cs: CandleStore, names: list[str], years: range) -> None:
    """Профиль по часам суток и проверка гипотезы о расчёте фандинга."""
    per_hour: dict[int, Acc] = defaultdict(Acc)
    yearly: dict[int, tuple[Acc, Acc]] = defaultdict(lambda: (Acc(), Acc()))
    for name in names:
        for year in years:
            for ts, ret in bars(cs, name, "1h", year):
                per_hour[ts.hour].add(ret)
                hit, usual = yearly[year]
                if ts.hour in BEFORE:
                    hit.add(ret)
                elif ts.hour not in SETTLEMENT:
                    # Час расчёта исключён из фона: он сам часть проверяемого эффекта.
                    usual.add(ret)

    total = sum(a.n for a in per_hour.values())
    if not total:
        print("часовых рядов нет")
        return
    print(f"\nЧАС СУТОК (UTC), баров {total}")
    print(f"{'час':6}{'баров':>9}{'средняя':>11}   пометка")
    for h in range(24):
        acc = per_hour[h]
        mark = "расчёт" if h in SETTLEMENT else ("перед расчётом" if h in BEFORE else "")
        print(f"{h:<6}{acc.n:>9}{acc.mean:>10.4f}%   {mark}")

    before = Acc()
    at = Acc()
    rest = Acc()
    for h, acc in per_hour.items():
        dst = before if h in BEFORE else at if h in SETTLEMENT else rest
        dst.total += acc.total
        dst.n += acc.n
    table(
        "Гипотеза: лонги выходят перед расчётом и возвращаются после",
        [("перед расчётом", before), ("час расчёта", at)],
        rest,
    )
    by_year("Час перед расчётом", yearly)


def weekdays(cs: CandleStore, names: list[str], years: range) -> None:
    per_day: dict[int, Acc] = defaultdict(Acc)
    yearly: dict[int, tuple[Acc, Acc]] = defaultdict(lambda: (Acc(), Acc()))
    for name in names:
        for year in years:
            for ts, ret in bars(cs, name, "1d", year):
                per_day[ts.weekday()].add(ret)
                hit, usual = yearly[year]
                (hit if ts.weekday() >= 5 else usual).add(ret)

    if not sum(a.n for a in per_day.values()):
        print("дневных рядов нет")
        return
    weekend = Acc()
    workday = Acc()
    for d, acc in per_day.items():
        dst = weekend if d >= 5 else workday
        dst.total += acc.total
        dst.n += acc.n
    table(
        "ДЕНЬ НЕДЕЛИ",
        [(DAYS[d], per_day[d]) for d in range(7)] + [("выходные", weekend)],
        workday,
    )
    by_year("Выходные", yearly)


def expiry(cs: CandleStore, names: list[str], years: range) -> None:
    """Последняя пятница месяца и день после неё против обычного дня."""
    day_of = Acc()
    day_after = Acc()
    usual = Acc()
    yearly: dict[int, tuple[Acc, Acc]] = defaultdict(lambda: (Acc(), Acc()))
    for name in names:
        for year in years:
            rows = list(bars(cs, name, "1d", year))
            for i, (ts, ret) in enumerate(rows):
                prev_expiry = i > 0 and last_friday(rows[i - 1][0].date())
                hit, other = yearly[year]
                if last_friday(ts.date()):
                    day_of.add(ret)
                    hit.add(ret)
                elif prev_expiry:
                    day_after.add(ret)
                else:
                    usual.add(ret)
                    other.add(ret)
    if not usual.n:
        print("дневных рядов нет")
        return
    table(
        "ЭКСПИРАЦИЯ ОПЦИОНОВ (последняя пятница месяца)",
        [("день гашения", day_of), ("день после", day_after)],
        usual,
    )
    by_year("День гашения", yearly)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--part", default="all", choices=("all", "hours", "weekday", "expiry"))
    ap.add_argument("--bases", default=DEFAULT_BASES)
    ap.add_argument("--from-year", type=int, default=2020)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    years = range(args.from_year, datetime.now(UTC).year + 1)
    cs = CandleStore(args.root)
    names = [f"{b.strip()}/USDT:USDT" for b in args.bases.split(",") if b.strip()]
    print(f"инструменты: {len(names)} | годы: {years.start}–{years.stop - 1}")

    if args.part in ("all", "hours"):
        hours(cs, names, years)
    if args.part in ("all", "weekday"):
        weekdays(cs, names, years)
    if args.part in ("all", "expiry"):
        expiry(cs, names, years)
    print(
        "\nПорог осмысленности: круг по мейкеру ≈ 0.04%, по тейкеру ≈ 0.10%.\n"
        "Эффект меньше этого не переживёт исполнения ни при каких правилах."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
