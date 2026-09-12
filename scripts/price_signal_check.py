#!/usr/bin/env python
"""Условная доходность по свечам: один инструмент на весь ярус дешёвых проверок.

Каждая проверка — именованное условие, а не новый скрипт. Условие говорит, какие бары
считать «событием» и от какой цены к какой мерить исход. Остальное общее: сравнение
с обычным баром, шум по независимым моментам времени, ориентир там, где он уместен,
разбивка по годам.

Условия (D2–D6 из `docs/research/queue-2026-09-12.md`):

* `overnight`  — ночь против дня: доходность close→open против open→close. В акциях
  документировано, что вся премия набегает ночью. У крипты ночи нет, у индексов есть.
* `weekend`    — пятничный разрыв: доходность за выходные (пятница close → понедельник
  open) и что происходит потом. Для BTC это лор о «гэпе CME»: фьючерсы CME закрыты
  на выходные, и разрыв якобы закрывается.
* `btc_lead`   — опережение: час BTC → следующий час альта. Если альты догоняют
  с задержкой, прошлый час BTC предсказывает их следующий час.
* `reversal`   — краткосрочный разворот на часовых барах: после сильного часа —
  следующий час против.
* `month_turn` — начало месяца: последние два и первые три дня месяца против остальных.

    python scripts/price_signal_check.py --condition overnight --venue yahoo \\
        --instruments SPX,NDX,DAX --tf 1d --root /app/data
    python scripts/price_signal_check.py --condition btc_lead --instruments ETH/USDT,SOL/USDT \\
        --tf 1h --root /app/data
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

CONDITIONS = ("overnight", "weekend", "btc_lead", "reversal", "month_turn")
TAKER_ROUND = 0.10  # круг по тейкеру, %


class Bucket:
    """Наблюдения по ключу времени: инструменты в один момент — одно наблюдение."""

    __slots__ = ("cells",)

    def __init__(self) -> None:
        self.cells: dict[object, list[float]] = defaultdict(lambda: [0.0, 0.0])

    def add(self, key: object, x: float) -> None:
        c = self.cells[key]
        c[0] += x
        c[1] += 1

    def means(self) -> list[float]:
        return [t / n for t, n in self.cells.values()]

    @property
    def n(self) -> int:
        return len(self.cells)

    @property
    def mean(self) -> float:
        m = self.means()
        return fmean(m) if m else 0.0

    @property
    def se(self) -> float:
        m = self.means()
        return stdev(m) / sqrt(len(m)) if len(m) > 1 else 0.0


def bars(cs: CandleStore, venue: str, name: str, tf: str) -> list[dict]:
    rows = cs.query(
        "select ts, open::DOUBLE as o, close::DOUBLE as c from {candles} order by ts",
        venue,
        name,
        tf,
    )
    return [r for r in rows if r["o"] and r["c"] and r["o"] > 0 and r["c"] > 0]


def run_overnight(rows: list[dict], hit: Bucket, rest: Bucket, years: dict) -> None:
    """hit — ночь (close→следующий open), rest — день (open→close того же бара)."""
    for a, b in zip(rows, rows[1:], strict=False):
        d = b["ts"].date()
        night = (b["o"] / a["c"] - 1) * 100
        day = (b["c"] / b["o"] - 1) * 100
        hit.add(d, night)
        rest.add(d, day)
        years[d.year][0].add(d, night)
        years[d.year][1].add(d, day)


def run_weekend(rows: list[dict], hit: Bucket, rest: Bucket, years: dict) -> None:
    """hit — понедельник после движения выходных ВВЕРХ, rest — после движения ВНИЗ.

    Лор о «гэпе CME»: фьючерсы CME на BTC закрыты с вечера пятницы до вечера
    воскресенья, спот в это время торгуется, и якобы в понедельник цена возвращается
    к пятничному закрытию. Если так, hit < 0 < rest.

    У крипты выходные бары ЕСТЬ, у индексов их нет, и «движение выходных» считается
    по-разному: там это пятница→воскресенье, здесь пятница→открытие понедельника.
    Первая версия проверки искала подряд идущие пятницу и понедельник — у крипты таких
    пар не бывает вовсе, и замер молча нашёл ноль наблюдений.
    """
    by_day = {r["ts"].date(): r for r in rows}
    for r in rows:
        if r["ts"].weekday() != 0:  # интересует понедельник
            continue
        d = r["ts"].date()
        friday = by_day.get(d - timedelta(days=3))
        if friday is None:
            continue
        sunday = by_day.get(d - timedelta(days=1))
        move = ((sunday["c"] if sunday else r["o"]) / friday["c"] - 1) * 100
        if abs(move) < 0.3:
            continue
        monday = (r["c"] / r["o"] - 1) * 100
        (hit if move > 0 else rest).add(d, monday)
        years[d.year][0 if move > 0 else 1].add(d, monday)


def run_reversal(rows: list[dict], hit: Bucket, rest: Bucket, years: dict) -> None:
    """hit — следующий бар после сильного бара (верхняя десятая по |ходу|), знак ПРОТИВ."""
    moves = [abs(r["c"] / r["o"] - 1) * 100 for r in rows]
    edge = sorted(moves)[int(len(moves) * 0.9)] if moves else 0
    for i in range(len(rows) - 1):
        cur, nxt = rows[i], rows[i + 1]
        ret = (nxt["c"] / nxt["o"] - 1) * 100
        key = nxt["ts"]
        d = key.date()
        if moves[i] >= edge:
            sign = -1 if cur["c"] > cur["o"] else 1  # против прошлого бара
            hit.add(key, sign * ret)
            years[d.year][0].add(key, sign * ret)
        else:
            rest.add(key, ret)
            years[d.year][1].add(key, ret)


def run_month_turn(rows: list[dict], hit: Bucket, rest: Bucket, years: dict) -> None:
    """hit — последние два и первые три дня месяца, rest — остальные."""
    for i, r in enumerate(rows):
        d = r["ts"].date()
        ret = (r["c"] / r["o"] - 1) * 100
        nxt = rows[i + 1]["ts"].date() if i + 1 < len(rows) else None
        nxt2 = rows[i + 2]["ts"].date() if i + 2 < len(rows) else None
        turn = d.day <= 3 or (nxt and nxt.month != d.month) or (nxt2 and nxt2.month != d.month)
        (hit if turn else rest).add(d, ret)
        years[d.year][0 if turn else 1].add(d, ret)


def run_btc_lead(
    rows: list[dict], btc: dict[datetime, float], hit: Bucket, rest: Bucket, years: dict
) -> None:
    """hit — час альта после сильного часа BTC (верхняя десятая), в ту же сторону."""
    btc_moves = {ts: m for ts, m in btc.items()}
    edge = sorted(abs(m) for m in btc_moves.values())[int(len(btc_moves) * 0.9)]
    for a, b in zip(rows, rows[1:], strict=False):
        m = btc_moves.get(a["ts"])
        if m is None:
            continue
        ret = (b["c"] / b["o"] - 1) * 100
        key = b["ts"]
        d = key.date()
        if abs(m) >= edge:
            sign = 1 if m > 0 else -1
            hit.add(key, sign * ret)
            years[d.year][0].add(key, sign * ret)
        else:
            rest.add(key, ret)
            years[d.year][1].add(key, ret)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--condition", required=True, choices=CONDITIONS)
    ap.add_argument("--venue", default="binance")
    ap.add_argument("--instruments", required=True, help="имена рядов через запятую")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--from-year", type=int, default=2019)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    cs = CandleStore(args.root)
    hit, rest = Bucket(), Bucket()
    years: dict[int, tuple[Bucket, Bucket]] = defaultdict(lambda: (Bucket(), Bucket()))
    btc: dict[datetime, float] = {}
    if args.condition == "btc_lead":
        for r in bars(cs, "binance", "BTC/USDT", args.tf):
            btc[r["ts"]] = (r["c"] / r["o"] - 1) * 100

    for name in [s.strip() for s in args.instruments.split(",") if s.strip()]:
        rows = [r for r in bars(cs, args.venue, name, args.tf) if r["ts"].year >= args.from_year]
        if len(rows) < 200:
            print(f"{name}: ряда мало ({len(rows)})")
            continue
        if args.condition == "overnight":
            run_overnight(rows, hit, rest, years)
        elif args.condition == "weekend":
            run_weekend(rows, hit, rest, years)
        elif args.condition == "reversal":
            run_reversal(rows, hit, rest, years)
        elif args.condition == "month_turn":
            run_month_turn(rows, hit, rest, years)
        else:
            run_btc_lead(rows, btc, hit, rest, years)
        print(f"{name}: {len(rows)} баров")

    labels = {
        "overnight": ("ночь (close→open)", "день (open→close)"),
        "weekend": ("понедельник после разрыва ВВЕРХ", "понедельник после разрыва ВНИЗ"),
        "reversal": ("бар после сильного, ПРОТИВ него", "обычный бар"),
        "month_turn": ("рубеж месяца", "остальные дни"),
        "btc_lead": ("час альта после сильного часа BTC, в ту же сторону", "обычный час"),
    }[args.condition]
    if not hit.n or not rest.n:
        print("\nнаблюдений нет")
        return 0
    diff = hit.mean - rest.mean
    se = sqrt(hit.se**2 + rest.se**2)
    if abs(diff) < 2 * se:
        verdict = "в пределах шума"
    elif abs(diff) < TAKER_ROUND:
        verdict = "меньше издержек"
    else:
        verdict = "ПЕРЕЖИВАЕТ ОБА ПОРОГА"
    print(f"\n{'группа':52}{'моментов':>10}{'средняя':>10}")
    print(f"{labels[0]:52}{hit.n:>10}{hit.mean:>9.3f}%")
    print(f"{labels[1]:52}{rest.n:>10}{rest.mean:>9.3f}%")
    print(f"\nразница {diff:+.3f}  шум (2σ) ±{2 * se:.3f}  → {verdict}")

    print(f"\n{'год':7}{'моментов':>10}{'первая группа':>15}{'вторая':>10}{'разница':>10}")
    signs = []
    for year in sorted(years):
        a, b = years[year]
        if not a.n or not b.n:
            continue
        signs.append(a.mean - b.mean)
        print(f"{year:<7}{a.n:>10}{a.mean:>14.3f}%{b.mean:>9.3f}%{a.mean - b.mean:>10.3f}")
    if len(signs) > 1:
        pos = sum(1 for s in signs if s > 0)
        print(f"знак совпадает в {max(pos, len(signs) - pos)} годах из {len(signs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
