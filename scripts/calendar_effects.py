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
у рынка есть свой дрейф. Издержки не вычитаются: сначала надо понять, есть ли валовый
эффект вообще. Круг по тейкеру на перпах Binance — около 0.10%, по мейкеру около 0.04%;
эффект меньше этого не переживёт исполнения ни при каких правилах.

**Наблюдение — это ДАТА, а не бар.** Восемь монет в один день ходят вместе, поэтому восемь
баров — это одно наблюдение, а не восемь; если считать шум по барам, он окажется втрое
меньше настоящего, и любая случайность сойдёт за находку. Поэтому бары сначала сводятся
по датам, и только потом считается разброс. Разбор по годам печатается всегда: поток
тейкеров прошёл четыре проверки подряд и развалился именно на годах.

    python scripts/calendar_effects.py --root /app/data
    python scripts/calendar_effects.py --part expiry --bases BTC,ETH
"""

from __future__ import annotations

import argparse
import sys
from calendar import FRIDAY
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

DEFAULT_BASES = "BTC,ETH,SOL,XRP,DOGE,AVAX,LINK,ADA"
SETTLEMENT = (0, 8, 16)  # часы расчёта фандинга, UTC
BEFORE = tuple((h - 1) % 24 for h in SETTLEMENT)
DAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MAKER_ROUND = 0.04  # круг по мейкеру, % — порог осмысленности эффекта


class Daily:
    """Наблюдения, сведённые по датам.

    Хранится сумма и счёт на дату, а не сами бары: часовых баров четыреста тысяч, а дат
    две с половиной тысячи. Средняя по датам совпадает с обычной средней только при равном
    числе баров в дне; расходится она как раз там, где данных по монете нет, — и это
    правильно, потому что день с одной монетой не должен весить как день с восемью.
    """

    __slots__ = ("by_date",)

    def __init__(self) -> None:
        self.by_date: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])

    def add(self, d: date, x: float) -> None:
        cell = self.by_date[d]
        cell[0] += x
        cell[1] += 1

    def means(self) -> list[float]:
        return [total / n for total, n in self.by_date.values()]

    @property
    def days(self) -> int:
        return len(self.by_date)

    @property
    def mean(self) -> float:
        vals = self.means()
        return fmean(vals) if vals else 0.0

    @property
    def se(self) -> float:
        """Стандартная ошибка средней — насколько средняя могла бы быть иной."""
        vals = self.means()
        return stdev(vals) / sqrt(len(vals)) if len(vals) > 1 else 0.0


def verdict(diff: float, se: float) -> str:
    """Два барьера подряд: сначала отличить от шума, потом окупить издержки."""
    if abs(diff) < 2 * se:
        return "в пределах шума"
    if abs(diff) < MAKER_ROUND:
        return "меньше издержек"
    return "ПЕРЕЖИВАЕТ ОБА ПОРОГА"


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


def table(title: str, rows: list[tuple[str, Daily]], usual: Daily) -> None:
    print(f"\n{title}")
    head = f"{'группа':16}{'дат':>7}{'средняя':>11}{'разница':>10}{'шум (2σ)':>11}   вердикт"
    print(head)
    for label, acc in rows:
        if not acc.days:
            continue
        diff = acc.mean - usual.mean
        se = sqrt(acc.se**2 + usual.se**2)
        print(
            f"{label:16}{acc.days:>7}{acc.mean:>10.4f}%{diff:>10.4f}"
            f"{2 * se:>11.4f}   {verdict(diff, se)}"
        )
    print(f"{'обычный бар':16}{usual.days:>7}{usual.mean:>10.4f}%")


def by_year(title: str, years: dict[int, tuple[Daily, Daily]]) -> None:
    """Эффект и фон по годам. Скачущий знак означает режим, а не закономерность."""
    print(f"\n{title} — ПО ГОДАМ")
    print(f"{'год':7}{'дат':>7}{'эффект':>11}{'фон':>11}{'разница':>11}{'шум (2σ)':>11}")
    signs: list[float] = []
    for year in sorted(years):
        hit, usual = years[year]
        if not hit.days or not usual.days:
            continue
        diff = hit.mean - usual.mean
        se = sqrt(hit.se**2 + usual.se**2)
        signs.append(diff)
        print(
            f"{year:<7}{hit.days:>7}{hit.mean:>10.4f}%{usual.mean:>10.4f}%"
            f"{diff:>11.4f}{2 * se:>11.4f}"
        )
    if len(signs) < 2:
        return
    pos = sum(1 for s in signs if s > 0)
    print(f"знак совпадает в {max(pos, len(signs) - pos)} годах из {len(signs)}")


def hours(cs: CandleStore, names: list[str], years: range) -> None:
    """Профиль по часам суток и проверка гипотезы о расчёте фандинга."""
    per_hour: dict[int, Daily] = defaultdict(Daily)
    before, at, rest = Daily(), Daily(), Daily()
    yearly: dict[int, tuple[Daily, Daily]] = defaultdict(lambda: (Daily(), Daily()))
    for name in names:
        for year in years:
            for ts, ret in bars(cs, name, "1h", year):
                d = ts.date()
                per_hour[ts.hour].add(d, ret)
                hit, usual = yearly[year]
                if ts.hour in BEFORE:
                    before.add(d, ret)
                    hit.add(d, ret)
                elif ts.hour in SETTLEMENT:
                    at.add(d, ret)  # час расчёта — сам часть эффекта, в фон не идёт
                else:
                    rest.add(d, ret)
                    usual.add(d, ret)

    if not rest.days:
        print("часовых рядов нет")
        return
    print(f"\nЧАС СУТОК (UTC), дат {rest.days}")
    print(f"{'час':6}{'средняя':>11}   пометка")
    for h in range(24):
        mark = "расчёт" if h in SETTLEMENT else ("перед расчётом" if h in BEFORE else "")
        print(f"{h:<6}{per_hour[h].mean:>10.4f}%   {mark}")
    table(
        "Гипотеза: лонги выходят перед расчётом и возвращаются после",
        [("перед расчётом", before), ("час расчёта", at)],
        rest,
    )
    by_year("Час перед расчётом", yearly)


def weekdays(cs: CandleStore, names: list[str], years: range) -> None:
    per_day: dict[int, Daily] = defaultdict(Daily)
    weekend, workday = Daily(), Daily()
    yearly: dict[int, tuple[Daily, Daily]] = defaultdict(lambda: (Daily(), Daily()))
    for name in names:
        for year in years:
            for ts, ret in bars(cs, name, "1d", year):
                d = ts.date()
                per_day[ts.weekday()].add(d, ret)
                hit, usual = yearly[year]
                (weekend if d.weekday() >= 5 else workday).add(d, ret)
                (hit if d.weekday() >= 5 else usual).add(d, ret)

    if not workday.days:
        print("дневных рядов нет")
        return
    table(
        "ДЕНЬ НЕДЕЛИ (сравнение с будним днём)",
        [(DAYS[d], per_day[d]) for d in range(7)] + [("выходные", weekend)],
        workday,
    )
    by_year("Выходные", yearly)


def expiry(cs: CandleStore, names: list[str], years: range) -> None:
    """Последняя пятница месяца и день после неё против обычного дня."""
    day_of, day_after, usual = Daily(), Daily(), Daily()
    yearly: dict[int, tuple[Daily, Daily]] = defaultdict(lambda: (Daily(), Daily()))
    for name in names:
        for year in years:
            rows = list(bars(cs, name, "1d", year))
            for i, (ts, ret) in enumerate(rows):
                d = ts.date()
                hit, other = yearly[year]
                if last_friday(d):
                    day_of.add(d, ret)
                    hit.add(d, ret)
                elif i > 0 and last_friday(rows[i - 1][0].date()):
                    day_after.add(d, ret)
                else:
                    usual.add(d, ret)
                    other.add(d, ret)
    if not usual.days:
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
        "\nДва порога подряд: разница должна быть больше собственного шума (2σ)\n"
        f"и больше круга по издержкам ({MAKER_ROUND}% по мейкеру, ~0.10% по тейкеру).\n"
        "Шум считается по ДАТАМ: монеты в один день — одно наблюдение, а не восемь."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
