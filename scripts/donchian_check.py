#!/usr/bin/env python
"""Расхождение по Donchian NASDAQ/BTC: две гипотезы вместо догадок.

Авторы (Quantpedia, «Silicon vs. Satoshi», 2019–2026) заявили CAGR 43.7% при Sharpe 1.69
и просадке −16.5%. Наш первый прогон (`scripts/rotation_check.py --rule donchian`) не дал
выше 16.9% ни на одном окне — разрыв в тридцать пунктов, и списывать его на «у них ETF,
у нас индекс» нельзя: дивиденды QQQ это полпроцента годовых, а не тридцать.

Поиск в описании работы дал две конкретные различия, и обе проверяются здесь:

1. **Приоритет.** Авторы пробовали ДВА варианта — кто выбирается, если пробой у обоих.
   У нас был жёстко NASDAQ, а он пробивает максимумы куда чаще биткойна, поэтому правило
   почти всё время сидело в индексе и пропускало крипту.
2. **Единица окна.** У них окно 5–50 **торговых** дней, у нас — календарных. Наше «20»
   это примерно 14 торговых: другая сетка, другой ответ.

Печатается вся сетка по обоим измерениям, плюс ориентиры. Никакого «лучшего окна»:
смысл проверки в том, есть ли ПЛАТО, а не пик.

    python scripts/donchian_check.py --root /app/data --start 2019-01-01
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

SERIES = {"BTC": ("bitstamp", "BTC/USD"), "NDX": ("yahoo", "NDX")}
# Восемь горизонтов, как у авторов (5-50 торговых дней).
WINDOWS = (5, 10, 15, 20, 25, 30, 40, 50)


def closes(cs: CandleStore, key: str) -> dict[date, float]:
    venue, name = SERIES[key]
    rows = cs.query("select ts, close::DOUBLE c from {candles} order by ts", venue, name, "1d")
    return {r["ts"].date(): float(r["c"]) for r in rows if r["c"] and r["c"] > 0}


def stats(curve: list[tuple[date, float]]) -> tuple[float, float, float]:
    rets = [b / a - 1 for (_, a), (_, b) in zip(curve, curve[1:], strict=False) if a > 0]
    days = (curve[-1][0] - curve[0][0]).days
    if days <= 0 or not rets:
        return 0.0, 0.0, 0.0
    total = curve[-1][1] / curve[0][1]
    cagr = (total ** (365.25 / days) - 1) * 100 if total > 0 else -100.0
    per_year = len(rets) / (days / 365.25)
    sd = stdev(rets) if len(rets) > 1 else 0.0
    sharpe = fmean(rets) / sd * sqrt(per_year) if sd else 0.0
    peak, dd = curve[0][1], 0.0
    for _, v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return cagr, sharpe, dd * 100


def run(
    btc: dict, ndx: dict, window: int, *, priority: str, unit: str, cost_bps: float,
    start: date, end: date,
) -> tuple[list[tuple[date, float]], float]:
    """Ротация по пробою канала. `unit`: trading — шагаем по дням ТОРГОВ NASDAQ."""
    if unit == "trading":
        days = [d for d in sorted(ndx) if start <= d <= end]
    else:
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def price(series: dict, d: date) -> float | None:
        for back in range(7):
            v = series.get(d - timedelta(days=back))
            if v is not None:
                return v
        return None

    equity, held = 1.0, "CASH"
    curve: list[tuple[date, float]] = []
    hist_b: list[float] = []
    hist_n: list[float] = []
    prev_b = prev_n = None
    switches = 0
    for d in days:
        p_b, p_n = price(btc, d), price(ndx, d)
        if p_b is None or p_n is None:
            continue
        if held == "BTC" and prev_b:
            equity *= p_b / prev_b
        elif held == "NDX" and prev_n:
            equity *= p_n / prev_n
        if len(hist_b) >= window and len(hist_n) >= window:
            up_b = p_b > max(hist_b[-window:])
            up_n = p_n > max(hist_n[-window:])
            first, second = ("BTC", "NDX") if priority == "btc" else ("NDX", "BTC")
            hit = {"BTC": up_b, "NDX": up_n}
            want = first if hit[first] else (second if hit[second] else "CASH")
            if want != held:
                equity *= 1 - cost_bps / 10_000 * (2 if held != "CASH" and want != "CASH" else 1)
                held = want
                switches += 1
        hist_b.append(p_b)
        hist_n.append(p_n)
        prev_b, prev_n = p_b, p_n
        curve.append((d, equity))
    return curve, switches


def hold(series: dict, start: date, end: date) -> list[tuple[date, float]]:
    days = [d for d in sorted(series) if start <= d <= end]
    if not days:
        return []
    base = series[days[0]]
    return [(d, series[d] / base) for d in days]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    args = ap.parse_args()

    cs = CandleStore(args.root)
    btc, ndx = closes(cs, "BTC"), closes(cs, "NDX")
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    print(f"Период {start} .. {end}, издержки {args.cost_bps:.0f} б.п. за переключение")
    for name, series in (("BTC", btc), ("NDX", ndx)):
        curve = hold(series, start, end)
        if curve:
            c, s, d = stats(curve)
            print(f"  ориентир {name} купить и держать: {c:+7.2f}%  Sharpe {s:.2f}  просадка {d:.1f}%")
    print("\nЗаявлено авторами: CAGR +43.70%, Sharpe 1.69, просадка −16.5%\n")

    print(f"{'окно':>5} {'единица':>11} {'приоритет':>10} {'CAGR':>9} {'Sharpe':>8} "
          f"{'просадка':>10} {'переключений':>13}")
    for unit in ("trading",):
        for priority in ("ndx", "btc"):
            for window in WINDOWS:
                curve, switches = run(
                    btc, ndx, window, priority=priority, unit=unit,
                    cost_bps=args.cost_bps, start=start, end=end,
                )
                if len(curve) < 100:
                    continue
                c, s, d = stats(curve)
                print(f"{window:>5} {unit:>11} {priority:>10} {c:>+9.2f} {s:>8.2f} "
                      f"{d:>10.1f} {switches:>13}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
