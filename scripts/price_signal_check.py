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

Условия F1–F3 — правила, которые группы трейдеров отдают программистам в ботов
(`docs/research/bots-2026-09-20.md`). У всех трёх есть выписанные правила и внешний
замер, с которым можно сверяться:

* `noise_break` — внутридневной импульс через «коридор шума» (Zarattini–Aziz–Barbon,
  SSRN 4824172): граница дня = открытие × (1 ± средний ход к этому времени суток
  за последние N дней). Выход за коридор — заявка на продолжение до закрытия суток.
  Поправки на ночные разрывы нет: у крипты нет ночи, и это как раз проверяется.
* `orb`        — пробой диапазона открытия (Zarattini–Aziz, SSRN 4416622): диапазон
  первых R баров сессии, вход на первом баре, закрывшемся за его пределами,
  удержание до закрытия сессии. Сессия — сутки UTC либо `--session-start`.
* `sweep`      — снятие ликвидности и возврат, механическое ядро SMC/ICT: бар уходит
  за вчерашний экстремум и закрывается обратно внутрь; вход против выноса.

**Контроль у F1–F3 — плацебо, а не «обычный бар».** Событие у них направленное,
а обычный бар — нет: в растущие годы ненаправленная группа получает дрейф рынка
в подарок и сравнение перекашивается. Поэтому контрольной группе направление даётся
броском монеты от отпечатка времени (`_placebo_sign`): под нулевой гипотезой обе группы
имеют нулевое ожидание, и разница — это ровно то, что знает правило.

    python scripts/price_signal_check.py --condition overnight --venue yahoo \\
        --instruments SPX,NDX,DAX --tf 1d --root /app/data
    python scripts/price_signal_check.py --condition btc_lead --instruments ETH/USDT,SOL/USDT \\
        --tf 1h --root /app/data
    python scripts/price_signal_check.py --condition noise_break --venue bybit \\
        --instruments BTC/USDT:USDT,ETH/USDT:USDT --tf 15m --root /app/data
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

CONDITIONS = (
    "overnight",
    "weekend",
    "btc_lead",
    "reversal",
    "month_turn",
    "noise_break",
    "orb",
    "sweep",
)
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
        "select ts, open::DOUBLE as o, high::DOUBLE as h, low::DOUBLE as l, "
        "close::DOUBLE as c from {candles} order by ts",
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


def _placebo_sign(ts: datetime) -> int:
    """Направление контрольной группе — броском монеты от отпечатка времени.

    Детерминировано (один бар всегда получает один и тот же знак, прогон повторяем),
    но с ценой не связано никак. Нужно, чтобы контроль не получал в подарок дрейф рынка:
    событие у F1–F3 направленное, а «обычный бар» — нет, и в растущие годы сравнение
    ненаправленной группы с направленной перекашивается само собой.
    """
    return 1 if hashlib.blake2b(ts.isoformat().encode(), digest_size=2).digest()[0] % 2 else -1


def _sessions(
    rows: list[dict], start: time | None, hours: int = 24
) -> list[tuple[object, list[dict]]]:
    """Бары по сессиям. Сессия — сутки UTC, либо окно `hours` от `start` до `start`."""
    out: dict[object, list[dict]] = defaultdict(list)
    for r in rows:
        ts = r["ts"]
        if start is None:
            out[ts.date()].append(r)
            continue
        shifted = ts - timedelta(hours=start.hour, minutes=start.minute)
        # Пояс берётся у самой метки: в хранилище они с поясом, а datetime.combine даёт
        # наивную, и вычитание падает с TypeError. Отказ громкий, но ловится только
        # на запуске с --session-start, то есть мимо обычного прогона.
        begins = datetime.combine(shifted.date(), start).replace(tzinfo=ts.tzinfo)
        offset = (ts - begins).total_seconds() / 3600
        if 0 <= offset < hours:
            out[shifted.date()].append(r)
    return sorted(out.items())


def run_noise_break(
    rows: list[dict], hit: Bucket, rest: Bucket, years: dict, lookback: int
) -> None:
    """Коридор шума: граница суток = открытие × (1 ± средний ход к этому времени суток).

    hit — ПЕРВЫЙ за сутки бар, закрывшийся за коридором, знак по направлению выхода;
    rest — бары внутри коридора, знак монетой. Вход у обеих групп — по открытию
    СЛЕДУЮЩЕГО бара (шаг 3 порядка проверки), удержание до закрытия суток.

    Наблюдение — сутки: 96 баров одного дня это одно свидетельство, а не 96.
    """
    history: dict[int, list[float]] = defaultdict(list)
    for key, day in _sessions(rows, None):
        if len(day) < 8:
            continue
        d_open, d_close = day[0]["o"], day[-1]["c"]
        fired = False
        for i, bar in enumerate(day[:-1]):
            past = history[i]
            if len(past) >= lookback:
                sigma = fmean(past[-lookback:])
                entry = day[i + 1]["o"]
                ret = (d_close / entry - 1) * 100
                side = 1 if bar["c"] > d_open * (1 + sigma) else 0
                side = -1 if bar["c"] < d_open * (1 - sigma) else side
                if side and not fired:
                    fired = True
                    hit.add(key, side * ret)
                    years[key.year][0].add(key, side * ret)
                elif not side:
                    sgn = _placebo_sign(bar["ts"])
                    rest.add(key, sgn * ret)
                    years[key.year][1].add(key, sgn * ret)
            history[i].append(abs(bar["c"] / d_open - 1))


def run_orb(
    rows: list[dict],
    hit: Bucket,
    rest: Bucket,
    years: dict,
    range_bars: int,
    start: time | None,
    hours: int,
) -> None:
    """Пробой диапазона открытия: диапазон первых `range_bars` баров сессии.

    hit — первый бар, закрывшийся за диапазоном, знак по направлению; rest — бары
    без пробоя, знак монетой. Вход у обеих — по открытию следующего бара, удержание
    до закрытия сессии.
    """
    for key, day in _sessions(rows, start, hours):
        if len(day) < range_bars + 3:
            continue
        rng = day[:range_bars]
        hi = max(b["h"] for b in rng if b["h"])
        lo = min(b["l"] for b in rng if b["l"])
        if not hi or not lo or lo <= 0:
            continue
        s_close = day[-1]["c"]
        fired = False
        for i in range(range_bars, len(day) - 1):
            bar, entry = day[i], day[i + 1]["o"]
            ret = (s_close / entry - 1) * 100
            side = 1 if bar["c"] > hi else (-1 if bar["c"] < lo else 0)
            if side and not fired:
                fired = True
                hit.add(key, side * ret)
                years[key.year][0].add(key, side * ret)
            elif not side:
                sgn = _placebo_sign(bar["ts"])
                rest.add(key, sgn * ret)
                years[key.year][1].add(key, sgn * ret)


def run_sweep(rows: list[dict], hit: Bucket, rest: Bucket, years: dict, horizon: int) -> None:
    """Снятие вчерашнего экстремума и возврат внутрь — механическое ядро SMC/ICT.

    hit — бар, ушедший за вчерашний минимум и закрывшийся выше него (лонг) или
    зеркально (шорт); rest — обычные бары, знак монетой. Вход по открытию следующего
    бара, удержание `horizon` баров.
    """
    ext = {
        k: (max(b["h"] for b in v if b["h"]), min(b["l"] for b in v if b["l"]))
        for k, v in _sessions(rows, None)
    }
    keys = sorted(ext)
    prev = {cur: ext[p] for p, cur in zip(keys, keys[1:], strict=False)}
    for i in range(len(rows) - horizon - 1):
        bar = rows[i]
        pe = prev.get(bar["ts"].date())
        if pe is None or not bar["h"] or not bar["l"]:
            continue
        phi, plo = pe
        entry = rows[i + 1]["o"]
        if entry <= 0:
            continue
        ret = (rows[i + 1 + horizon]["c"] / entry - 1) * 100
        d = bar["ts"].date()
        side = 1 if (bar["l"] < plo and bar["c"] > plo) else 0
        side = -1 if (bar["h"] > phi and bar["c"] < phi) else side
        if side:
            hit.add(d, side * ret)
            years[d.year][0].add(d, side * ret)
        else:
            sgn = _placebo_sign(bar["ts"])
            rest.add(d, sgn * ret)
            years[d.year][1].add(d, sgn * ret)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--condition", required=True, choices=CONDITIONS)
    ap.add_argument("--venue", default="binance")
    ap.add_argument("--instruments", required=True, help="имена рядов через запятую")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--from-year", type=int, default=2019)
    ap.add_argument("--root", default="data")
    ap.add_argument("--lookback", type=int, default=14, help="дней в коридоре шума (noise_break)")
    ap.add_argument("--range-bars", type=int, default=1, help="баров в диапазоне открытия (orb)")
    ap.add_argument("--session-start", default=None, help="начало сессии ЧЧ:ММ UTC (orb)")
    ap.add_argument("--session-hours", type=int, default=24, help="длина сессии в часах (orb)")
    ap.add_argument("--horizon", type=int, default=4, help="баров удержания (sweep)")
    args = ap.parse_args()

    cs = CandleStore(args.root)
    hit, rest = Bucket(), Bucket()
    years: dict[int, tuple[Bucket, Bucket]] = defaultdict(lambda: (Bucket(), Bucket()))
    session_start: time | None = None
    if args.session_start:
        hh, _, mm = args.session_start.partition(":")
        session_start = time(int(hh), int(mm or 0))

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
        elif args.condition == "noise_break":
            run_noise_break(rows, hit, rest, years, args.lookback)
        elif args.condition == "orb":
            run_orb(rows, hit, rest, years, args.range_bars, session_start, args.session_hours)
        elif args.condition == "sweep":
            run_sweep(rows, hit, rest, years, args.horizon)
        else:
            run_btc_lead(rows, btc, hit, rest, years)
        print(f"{name}: {len(rows)} баров")

    labels = {
        "overnight": ("ночь (close→open)", "день (open→close)"),
        "weekend": ("понедельник после разрыва ВВЕРХ", "понедельник после разрыва ВНИЗ"),
        "reversal": ("бар после сильного, ПРОТИВ него", "обычный бар"),
        "month_turn": ("рубеж месяца", "остальные дни"),
        "btc_lead": ("час альта после сильного часа BTC, в ту же сторону", "обычный час"),
        "noise_break": ("выход за коридор шума, по направлению выхода", "бар внутри коридора, монетой"),
        "orb": ("пробой диапазона открытия, по направлению", "бар без пробоя, монетой"),
        "sweep": ("снятие вчерашнего экстремума и возврат, против выноса", "обычный бар, монетой"),
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
