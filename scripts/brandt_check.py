#!/usr/bin/env python
"""Правила Peter Brandt на дневных BTC/ETH: несёт ли информацию вход по фигуре.

П.8 плана трейдеров, третий пункт порядка (`teaching-traders-2026-10-03.md`). Правила проверки
и вердикта объявлены до прогона — `docs/research/brandt-2026-10-11.md` (коммит `59d8d1a`);
здесь только их исполнение.

* Фигура — формальное приближение горизонтальной фигуры: граница `B` = максимум хаев `N` дней,
  касания (хай ≥ B − 0.5·ATR14) разнесены не меньше чем на N/4 дней, закрытие дня `t` выше `B`.
  Шорт — зеркально: цены умножаются на −1, и работает та же логика лонга.
* Вход — открытие `t+1`; позиция пополам, ведение по записке Factor 06.12.2023 (LDR, безубыток
  после 1R, 3DTSR, измеренный ход, выход по MA8 у позиционной половины).
* Контроль К1 — случайный день ±365 того же инструмента, те же относительные стопы и цель, те же
  правила по данным. К2 — пробой Donchian того же `N` без условия касаний.

Доходность — лог, в %, за вычетом издержек 0.20% за круг. Шум — по блокам «месяц входа».

    py -3 scripts/brandt_check.py --cache ../lab-data/brandt
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import urllib.request
from collections import defaultdict
from datetime import UTC, date, datetime
from math import log, sqrt
from pathlib import Path
from statistics import fmean

START = date(2017, 8, 17)
END = date(2026, 10, 10)
EARLY = (date(2013, 1, 1), date(2017, 8, 16))  # Bitstamp, только справка
NS = (28, 56, 84)
MAIN_N = 56
COST = 0.20  # % за круг
WINDOW = 365  # К1: случайный день в ±WINDOW от сигнала
DRAWS = 2000
ATR_N = 14
TOUCH_ATR = 0.5

Bars = tuple[list[date], list[float], list[float], list[float], list[float]]


# -- данные -----------------------------------------------------------------------------


def binance_daily(symbol: str, cache: Path) -> Bars:
    path = cache / f"binance_{symbol}_1d.json"
    if not path.exists():
        rows: list[list] = []
        start = datetime(START.year, START.month, START.day, tzinfo=UTC)
        start_ms = int(start.timestamp() * 1000)
        while True:
            url = (
                "https://api.binance.com/api/v3/klines"
                f"?symbol={symbol}&interval=1d&startTime={start_ms}&limit=1000"
            )
            with urllib.request.urlopen(url, timeout=30) as r:
                chunk = json.load(r)
            if not chunk:
                break
            for k in chunk:
                d = datetime.fromtimestamp(k[0] / 1000, tz=UTC).date()
                rows.append([d.isoformat(), float(k[1]), float(k[2]), float(k[3]), float(k[4])])
            if len(chunk) < 1000:
                break
            start_ms = chunk[-1][0] + 86_400_000
        cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows), encoding="utf-8")
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows = [r for r in rows if START <= date.fromisoformat(r[0]) <= END]
    return unpack(rows)


def bitstamp_daily(path: Path) -> Bars:
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r["timestamp"][:10])
            if EARLY[0] <= d <= EARLY[1]:
                px = [float(r[k]) for k in ("open", "high", "low", "close")]
                rows.append([d.isoformat(), *px])
    return unpack(rows)


def unpack(rows: list[list]) -> Bars:
    days = [date.fromisoformat(r[0]) for r in rows]
    for a, b in zip(days, days[1:], strict=False):
        if (b - a).days != 1:
            print(f"  ! дыра в данных: {a} → {b}")
    return days, *([r[i] for r in rows] for i in range(1, 5))


def mirror(bars: Bars) -> Bars:
    """Шорт как лонг на зеркальных ценах: хай ↔ −лой."""
    d, o, h, lo, c = bars
    return d, [-x for x in o], [-x for x in lo], [-x for x in h], [-x for x in c]


# -- индикаторы -------------------------------------------------------------------------


def atr(h: list[float], lo: list[float], c: list[float]) -> list[float | None]:
    """ATR14 по дню i включительно (простое среднее TR)."""
    tr = [h[0] - lo[0]] + [
        max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))
    ]
    out: list[float | None] = [None] * len(c)
    for i in range(ATR_N - 1, len(c)):
        out[i] = fmean(tr[i - ATR_N + 1 : i + 1])
    return out


def sma(c: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    for i in range(n - 1, len(c)):
        out[i] = fmean(c[i - n + 1 : i + 1])
    return out


# -- сигнал -----------------------------------------------------------------------------


def pattern_at(bars: Bars, a: list, t: int, n: int, touches: bool) -> dict | None:
    """Пробой в день t (лонг-логика). None — если сигнала нет."""
    _, o, h, lo, c = bars
    if t < n + ATR_N or t + 1 >= len(c) or a[t - 1] is None:
        return None
    w = range(t - n, t)
    b = max(h[j] for j in w)
    if c[t] <= b:
        return None
    if touches:
        tol = b - TOUCH_ATR * a[t - 1]
        hits = [j for j in w if h[j] >= tol]
        if hits[-1] - hits[0] < n / 4:
            return None
    low_w = min(lo[j] for j in w)
    # LDR: лой дня пробоя, если не меньше половины бара ниже границы, иначе лой t−1.
    ldr = lo[t] if (b - lo[t]) >= (h[t] - lo[t]) / 2 else lo[t - 1]
    return {
        "t": t,
        "ldr": ldr,
        "pos0": min(lo[t - 1], ldr),
        "target": b + (b - low_w),
    }


# -- ведение позиции --------------------------------------------------------------------


def run_trade(bars: Bars, ma8: list, e: int, entry: float, ldr: float, pos0: float,
              target: float | None, v2: bool) -> tuple[tuple[int, float], tuple[int, float]]:
    """Две половины: (индекс выхода, цена выхода) торговой и позиционной. Лонг-логика."""
    _, o, h, lo, c = bars
    n = len(c)
    r = entry - ldr

    # торговая половина
    stop, hi, hi_low, setup = ldr, None, None, None
    tr_exit = (n - 1, c[-1])
    for k in range(e, n):
        eff = stop if setup is None else max(stop, setup)
        if lo[k] <= eff:
            tr_exit = (k, min(o[k], eff))
            break
        if target is not None and h[k] >= target:
            tr_exit = (k, max(o[k], target))
            break
        if c[k] >= entry + r:
            stop = max(stop, entry)
        if hi is None or h[k] > hi:
            hi, hi_low, setup = h[k], lo[k], None
        elif setup is None and c[k] < hi_low:
            setup = lo[k]

    # позиционная половина
    stop, hi, hi_low, setup, ma_lvl = pos0, None, None, None, None
    pos_target = None if v2 else target
    pos_exit = (n - 1, c[-1])
    for k in range(e, n):
        if k >= e + 1:
            stop = max(stop, ldr)
        eff = stop
        if setup is not None and setup < entry:  # 3DTSR — только если выход был бы в убыток
            eff = max(eff, setup)
        if ma_lvl is not None:
            eff = max(eff, ma_lvl)
        if lo[k] <= eff:
            pos_exit = (k, min(o[k], eff))
            break
        if pos_target is not None and h[k] >= pos_target:
            pos_exit = (k, max(o[k], pos_target))
            break
        if hi is None or h[k] > hi:
            hi, hi_low, setup = h[k], lo[k], None
        elif setup is None and c[k] < hi_low:
            setup = lo[k]
        if (c[k] > entry and ma8[k] is not None and ma8[k - 1] is not None
                and ma8[k] < ma8[k - 1] and h[k] < ma8[k]):
            ma_lvl = lo[k]
    return tr_exit, pos_exit


def trade_result(entry: float, ldr: float, side: int, exits) -> dict:
    """Итог в лог-% (за вычетом издержек), в R и простой доходности. Цены могут быть зеркальными."""
    logs = [side * log(px / entry) * 100 for _, px in exits]
    simple = [(px - entry) / abs(entry) for _, px in exits]
    rs = [(px - entry) / (entry - ldr) for _, px in exits]
    return {
        "ret": fmean(logs) - COST,
        "simple": fmean(simple) - COST / 100,
        "R": fmean(rs),
        "exit": max(k for k, _ in exits),
    }


# -- прогон -----------------------------------------------------------------------------


def trades_for(bars: Bars, side: int, n: int, touches: bool, v2: bool) -> tuple[list[dict], int]:
    b = bars if side > 0 else mirror(bars)
    days, o, h, lo, c = b
    a = atr(h, lo, c)
    ma8 = sma(c, 8)
    out, skipped, busy_until = [], 0, -1
    for t in range(len(c)):
        if t < busy_until:
            continue
        sig = pattern_at(b, a, t, n, touches)
        if sig is None:
            continue
        e = t + 1
        entry = o[e]
        if entry <= sig["ldr"]:
            skipped += 1
            continue
        exits = run_trade(b, ma8, e, entry, sig["ldr"], sig["pos0"], sig["target"], v2)
        res = trade_result(entry, sig["ldr"], side, exits)
        res.update(
            day=days[t], t=t, e=e, entry=entry, ldr=sig["ldr"], pos0=sig["pos0"],
            target=sig["target"], hold=res["exit"] - e + 1,
            open_at_end=exits[0][0] == len(c) - 1 or exits[1][0] == len(c) - 1,
        )
        out.append(res)
        busy_until = res["exit"]
    return out, skipped


def control_one(bars_side: Bars, ma8: list, tr: dict, u: int, v2: bool) -> float | None:
    """К1: та же сделка с входом по открытию u+1, те же относительные уровни."""
    _, o, *_ = bars_side
    e = u + 1
    if e >= len(o):
        return None
    entry = o[e]
    k = entry / tr["entry"]
    ldr, pos0 = tr["ldr"] * k, tr["pos0"] * k
    target = tr["target"] * k if tr["target"] is not None else None
    exits = run_trade(bars_side, ma8, e, entry, ldr, pos0, target, v2)
    return trade_result(entry, ldr, 1 if entry > 0 else -1, exits)["ret"]


def controls(bars: Bars, side: int, trades: list[dict], n: int, v2: bool, rng: random.Random,
             draws: int) -> list[list[float]]:
    """Для каждой сделки — `draws` значений К1 (одинаковый порядок наборов у всех сделок)."""
    b = bars if side > 0 else mirror(bars)
    ma8 = sma(b[4], 8)
    lo_ok, hi_ok = n + ATR_N, len(b[4]) - 2
    out = []
    for tr in trades:
        lo_u, hi_u = max(lo_ok, tr["t"] - WINDOW), min(hi_ok, tr["t"] + WINDOW)
        vals = []
        for _ in range(draws):
            v = control_one(b, ma8, tr, rng.randint(lo_u, hi_u), v2)
            vals.append(v if v is not None else 0.0)
        out.append(vals)
    return out


def block_stats(pairs: list[tuple[int, float]]) -> tuple[float, float, int]:
    """Среднее по сделкам, 2σ по блокам, число блоков (как в `youtube_check.py`)."""
    sums: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for blk, x in pairs:
        sums[blk][0] += x
        sums[blk][1] += 1
    nb = len(sums)
    total = sum(cnt for _, cnt in sums.values())
    if nb < 2 or not total:
        return (fmean(x for _, x in pairs) if pairs else 0.0), float("inf"), nb
    m = sum(s for s, _ in sums.values()) / total
    resid = sum((s - m * cnt) ** 2 for s, cnt in sums.values())
    return m, 2 * sqrt(resid * nb / (nb - 1)) / total, nb


def blk(d: date) -> int:
    return d.year * 12 + d.month


def cell(assets: dict[str, Bars], side: int, n: int, touches: bool, v2: bool, draws: int,
         seed: int) -> dict:
    rng = random.Random(seed)
    trades, ctl, skipped = [], [], 0
    for name, bars in assets.items():
        tr, sk = trades_for(bars, side, n, touches, v2)
        skipped += sk
        for x in tr:
            x["asset"] = name
        trades += tr
        ctl += controls(bars, side, tr, n, v2, rng, draws) if draws else [[0.0]] * len(tr)
    if not trades:
        return {"n": 0, "skipped": skipped}
    m, m2s, nb = block_stats([(blk(x["day"]), x["ret"]) for x in trades])
    for x, cv in zip(trades, ctl, strict=True):
        x["ctl"] = fmean(cv)
        x["exc"] = x["ret"] - x["ctl"]
    ex, ex2s, _ = block_stats([(blk(x["day"]), x["exc"]) for x in trades])
    if draws:
        real = fmean(x["ret"] for x in trades)
        set_means = [fmean(cv[i] for cv in ctl) for i in range(draws)]
        p = sum(1 for s in set_means if s >= real) / draws
    else:
        p = float("nan")
    return {
        "n": len(trades), "skipped": skipped, "blocks": nb, "trades": trades,
        "mean": m, "m2s": m2s, "win": sum(x["ret"] > 0 for x in trades) / len(trades),
        "R": fmean(x["R"] for x in trades), "hold": fmean(x["hold"] for x in trades),
        "ctl": fmean(x["ctl"] for x in trades), "exc": ex, "ex2s": ex2s, "p": p,
        "open": sum(x["open_at_end"] for x in trades),
    }


# -- счёт с риском 1% -------------------------------------------------------------------


def equity(trades: list[dict], dates: dict[str, list[date]]) -> tuple[float, float, float]:
    """Реализованный счёт: риск 1% депозита на сделку, позиция ≤ ×3. Итог ×, CAGR %, просадка %."""
    eq, peak, dd = 1.0, 1.0, 0.0
    events = []
    for x in trades:
        d = dates[x["asset"]]
        events.append((d[x["e"]], 0, x))
        events.append((d[x["exit"]], 1, x))
    events.sort(key=lambda ev: (ev[0], ev[1]))
    size: dict[int, float] = {}
    for _, kind, x in events:
        if kind == 0:
            stop_frac = abs(x["entry"] - (x["ldr"] + x["pos0"]) / 2) / abs(x["entry"])
            size[id(x)] = min(0.01 * eq / stop_frac, 3 * eq) if stop_frac > 0 else 3 * eq
        else:
            eq += size.pop(id(x)) * x["simple"]
            peak = max(peak, eq)
            dd = min(dd, eq / peak - 1)
    years = (END - START).days / 365.25
    cagr = (eq ** (1 / years) - 1) * 100 if eq > 0 else -100.0
    return eq, cagr, dd * 100


def hold_stats(bars: Bars) -> tuple[float, float, float]:
    _, o, h, lo, c = bars
    years = (END - START).days / 365.25
    peak, dd = c[0], 0.0
    for x in c:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1)
    return c[-1] / o[0], ((c[-1] / o[0]) ** (1 / years) - 1) * 100, dd * 100


# -- печать -----------------------------------------------------------------------------


def fmt(r: dict) -> str:
    if not r["n"]:
        return f"{'0':>4}  (пропущено {r['skipped']})"
    return (
        f"{r['n']:>4} {r['blocks']:>4} {r['skipped']:>3} {r['win'] * 100:>5.0f}% {r['hold']:>5.1f}"
        f" {r['mean']:>+7.2f} ±{r['m2s']:<5.2f} {r['R']:>+6.2f}"
        f" {r['ctl']:>+7.2f} {r['exc']:>+7.2f} ±{r['ex2s']:<5.2f} {r['p']:>6.3f}"
    )


HEAD = (
    "      сделок блок проп  win%  дней  итог,лог%  ±2σ     R     К1    избыток  ±2σ     p"
)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("../lab-data/brandt"))
    ap.add_argument(
        "--bitstamp", type=Path, default=Path("../lab-data/candles-cache/bitstamp_btcusd_1d.csv")
    )
    ap.add_argument("--draws", type=int, default=DRAWS)
    ap.add_argument("--seed", type=int, default=20261011)
    args = ap.parse_args()

    assets = {s: binance_daily(f"{s}USDT", args.cache) for s in ("BTC", "ETH")}
    for s, b in assets.items():
        print(f"{s}: {b[0][0]} … {b[0][-1]}, {len(b[0])} дней")
    dates = {s: b[0] for s, b in assets.items()}

    main_cells = {}
    for side, sname in ((1, "ЛОНГ"), (-1, "ШОРТ")):
        print(f"\n=== {sname}: фигура (касания), BTC+ETH, К1 — {args.draws} наборов ===")
        print(HEAD)
        for v2 in (False, True):
            for n in NS:
                r = cell(assets, side, n, True, v2, args.draws, args.seed + n)
                tag = f"{'V2' if v2 else 'V1'} N={n:<3}"
                print(f"{tag} {fmt(r)}")
                if not v2:
                    main_cells[(side, n)] = r
        print("К2 — пробой Donchian без касаний (V1, К1 не считается):")
        for n in NS:
            r = cell(assets, side, n, False, False, 0, 0)
            print(f"   N={n:<3}  сделок {r['n']:>3}  итог {r.get('mean', 0):>+7.2f}"
                  f" ±{r.get('m2s', 0):.2f}  win {r.get('win', 0) * 100:.0f}%"
                  f"  R {r.get('R', 0):+.2f}")

    print("\n=== главная клетка N=56, V1: по инструментам и годам ===")
    for side, sname in ((1, "ЛОНГ"), (-1, "ШОРТ")):
        r = main_cells[(side, MAIN_N)]
        if not r["n"]:
            print(f"{sname}: сделок нет")
            continue
        print(f"\n{sname}: открыты на конец данных — {r['open']}")
        for a in assets:
            xs = [x for x in r["trades"] if x["asset"] == a]
            if xs:
                print(f"  {a}: {len(xs):>3} сделок, итог {fmean(x['ret'] for x in xs):+.2f},"
                      f" избыток к К1 {fmean(x['exc'] for x in xs):+.2f}")
        by_year: dict[int, list[dict]] = defaultdict(list)
        for x in r["trades"]:
            by_year[x["day"].year].append(x)
        tot = sum(x["exc"] for x in r["trades"])
        print("  год  сделок  итог   избыток  доля избытка")
        for y in sorted(by_year):
            xs = by_year[y]
            s = sum(x["exc"] for x in xs)
            share = s / tot * 100 if tot else float("nan")
            print(f"  {y}  {len(xs):>5}  {fmean(x['ret'] for x in xs):+6.2f}"
                  f"  {fmean(x['exc'] for x in xs):+7.2f}  {share:+6.0f}%")
        eq, cagr, dd = equity(r["trades"], dates)
        print(f"  счёт 1% риска (≤×3): ×{eq:.2f}, {cagr:+.1f}% годовых, просадка {dd:.1f}%")

    for a, b in assets.items():
        m, cagr, dd = hold_stats(b)
        print(f"{a} купил и держи: ×{m:.1f}, {cagr:+.1f}% годовых, просадка {dd:.1f}%")

    print("\n=== вердикт по объявленным правилам (N=56, V1) ===")
    for side, sname in ((1, "ЛОНГ"), (-1, "ШОРТ")):
        r = main_cells[(side, MAIN_N)]
        if not r["n"]:
            print(f"{sname}: сделок нет → failed")
            continue
        c1 = r["mean"] > 0 and r["mean"] > r["m2s"]
        c2 = r["exc"] > r["ex2s"] and r["p"] <= 0.025
        c3 = all(main_cells[(side, n)].get("exc", -1) > 0 for n in NS)
        by_year = defaultdict(list)
        for x in r["trades"]:
            by_year[x["day"].year].append(x["exc"])
        full = {y: v for y, v in by_year.items() if len(v) >= 2}
        tot = sum(x["exc"] for x in r["trades"])
        pos_years = sum(1 for v in full.values() if sum(v) > 0)
        c4 = bool(full) and pos_years * 2 >= len(full) and tot > 0 and all(
            sum(v) <= tot / 2 for v in by_year.values())
        verdict = "passed" if c1 and c2 and c3 and c4 else "failed"
        note = " — плюс даёт выход по тренду, а не фигура" if c1 and not c2 else ""
        print(f"{sname}: 1 итог>2σ {c1}; 2 избыток К1 {c2}; 3 плато {c3}; 4 годы {c4}"
              f" ({pos_years}/{len(full)} лет) → {verdict}{note}")

    # справка: ранняя эпоха BTC (Bitstamp), главная клетка
    if args.bitstamp.exists():
        early = {"BTC-bitstamp": bitstamp_daily(args.bitstamp)}
        print(f"\n=== справка: BTC Bitstamp {EARLY[0]} … {EARLY[1]}, N=56, V1 ===")
        print(HEAD)
        for side, sname in ((1, "ЛОНГ"), (-1, "ШОРТ")):
            r = cell(early, side, MAIN_N, True, False, min(args.draws, 500), args.seed)
            print(f"{sname:<9} {fmt(r)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
