#!/usr/bin/env python
"""Ротация между активами (Quantpedia, 2026): проверка на наших рядах ДО написания стратегии.

Два опубликованных правила, у которых есть точные параметры и цифры:

* **Dual momentum «золото против биткойна»** (Quantpedia, 06.05.2026). Раз в неделю: купить
  BTC, если его доходность за `lookback` недель выше золота И выше нуля; иначе золото
  по тому же условию; иначе кэш. Заявлено: CAGR 79.9%, Sharpe 1.64, просадка −43.9%
  на 2019–2026 при окне 8 недель. Издержки не моделировались.
* **Donchian-ротация «NASDAQ против биткойна»** (Quantpedia, 02.07.2026). Ежедневно:
  если QQQ выше своего максимума за `window` дней — QQQ; иначе если BTC выше своего —
  BTC; иначе кэш. Заявлено: CAGR 43.7%, Sharpe 1.69, просадка −16.5% на 2019–2025.
  Издержки не моделировались.

Обе — кросс-активные, и это их отличие от всего, что мы мерили: они не ищут
преимущество внутри крипты, а переключаются между крипто и классикой по тренду.

Что проверяется здесь и чего не было в источнике:

* **издержки** — круг по споту на каждое переключение (`--cost-bps`);
* **все окна, а не лучшее**: авторы отчитались об оптимальном окне из десяти,
  а это отбор по известному исходу. Печатается вся сетка;
* **по годам** — как всегда: режим, выданный за закономерность, виден только так;
* **ориентир** — BTC «купить и держать» и ровная половина BTC/золото.

Ряды: BTC — Bitstamp с 2011, золото и NASDAQ — Yahoo (XAUUSD, NDX) с 2006.

    python scripts/rotation_check.py --rule dual --root /app/data
    python scripts/rotation_check.py --rule donchian --root /app/data
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

SERIES = {
    "BTC": ("bitstamp", "BTC/USD"),
    "GOLD": ("yahoo", "XAUUSD"),
    "NDX": ("yahoo", "NDX"),
}


def closes(cs: CandleStore, key: str) -> dict[date, float]:
    venue, name = SERIES[key]
    rows = cs.query(
        "select ts, close::DOUBLE as c from {candles} order by ts", venue, name, "1d"
    )
    return {r["ts"].date(): float(r["c"]) for r in rows if r["c"] and r["c"] > 0}


def last_on_or_before(series: dict[date, float], d: date, back: int = 6) -> float | None:
    """Цена на дату или ближайшую ДО неё: у классики нет выходных, у крипты есть."""
    for k in range(back + 1):
        v = series.get(d - timedelta(days=k))
        if v is not None:
            return v
    return None


def equity_stats(curve: list[tuple[date, float]]) -> dict[str, float]:
    """CAGR, годовая волатильность, Sharpe (безриск 0), просадка — по кривой капитала."""
    if len(curve) < 3:
        return {"cagr": 0.0, "sharpe": 0.0, "dd": 0.0}
    rets = [b / a - 1 for (_, a), (_, b) in zip(curve, curve[1:], strict=False) if a > 0]
    days = (curve[-1][0] - curve[0][0]).days
    per_year = len(rets) / max(days / 365.25, 1e-9)
    total = curve[-1][1] / curve[0][1]
    cagr = (total ** (365.25 / days) - 1) * 100 if days > 0 and total > 0 else 0.0
    sd = stdev(rets) if len(rets) > 1 else 0.0
    sharpe = fmean(rets) / sd * sqrt(per_year) if sd else 0.0
    peak, dd = curve[0][1], 0.0
    for _, v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {"cagr": cagr, "sharpe": sharpe, "dd": dd * 100}


def _dual_vote(btc: dict, gold: dict, d: date, windows: tuple[int, ...]) -> str:
    """Голосование нескольких окон: что выбирает большинство.

    Проверка на устойчивость к параметру. Если преимущество живёт только на одном окне
    из семи — это отбор по известному исходу, и составной сигнал его не покажет.
    """
    votes: dict[str, int] = {"BTC": 0, "GOLD": 0, "CASH": 0}
    p_btc, p_gold = last_on_or_before(btc, d), last_on_or_before(gold, d)
    if p_btc is None or p_gold is None:
        return "CASH"
    for w in windows:
        b0 = last_on_or_before(btc, d - timedelta(weeks=w))
        g0 = last_on_or_before(gold, d - timedelta(weeks=w))
        if b0 is None or g0 is None:
            continue
        r_btc, r_gold = p_btc / b0 - 1, p_gold / g0 - 1
        if r_btc > r_gold and r_btc > 0:
            votes["BTC"] += 1
        elif r_gold > r_btc and r_gold > 0:
            votes["GOLD"] += 1
        else:
            votes["CASH"] += 1
    return max(votes, key=lambda k: votes[k])


def run_dual(
    btc: dict, gold: dict, weeks: int, cost: float, start: date, end: date,
    composite: tuple[int, ...] | None = None,
) -> list[tuple[date, float, str]]:
    """Еженедельная ротация BTC / золото / кэш. Возврат — кривая капитала и что держали."""
    equity, held = 1.0, "CASH"
    out: list[tuple[date, float, str]] = []
    d = start
    while d <= end:
        p_btc, p_gold = last_on_or_before(btc, d), last_on_or_before(gold, d)
        prev = d - timedelta(weeks=weeks)
        b0, g0 = last_on_or_before(btc, prev), last_on_or_before(gold, prev)
        if None in (p_btc, p_gold, b0, g0):
            d += timedelta(weeks=1)
            continue
        # Доход за прошедшую неделю по ТОМУ, что держали: решение о смене — после.
        if out:
            _, _, was = out[-1]
            last_b = last_on_or_before(btc, d - timedelta(weeks=1))
            last_g = last_on_or_before(gold, d - timedelta(weeks=1))
            if was == "BTC" and last_b:
                equity *= p_btc / last_b
            elif was == "GOLD" and last_g:
                equity *= p_gold / last_g
        if composite:
            want = _dual_vote(btc, gold, d, composite)
        else:
            r_btc, r_gold = p_btc / b0 - 1, p_gold / g0 - 1
            if r_btc > r_gold and r_btc > 0:
                want = "BTC"
            elif r_gold > r_btc and r_gold > 0:
                want = "GOLD"
            else:
                want = "CASH"
        if want != held:
            equity *= 1 - cost / 10_000 * (2 if held != "CASH" and want != "CASH" else 1)
            held = want
        out.append((d, equity, held))
        d += timedelta(weeks=1)
    return out


def run_donchian(
    btc: dict, ndx: dict, window: int, cost: float, start: date, end: date
) -> list[tuple[date, float, str]]:
    """Ежедневная ротация: NDX на пробое своего максимума, иначе BTC на своём, иначе кэш."""
    equity, held = 1.0, "CASH"
    out: list[tuple[date, float, str]] = []
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    hist_b: list[float] = []
    hist_n: list[float] = []
    prev_b = prev_n = None
    for d in days:
        p_b, p_n = last_on_or_before(btc, d), last_on_or_before(ndx, d)
        if p_b is None or p_n is None:
            continue
        if held == "BTC" and prev_b:
            equity *= p_b / prev_b
        elif held == "NDX" and prev_n:
            equity *= p_n / prev_n
        # Пробой — выше максимума ПРЕДЫДУЩИХ `window` дней (сегодняшняя цена не в окне).
        if len(hist_b) >= window and len(hist_n) >= window:
            if p_n > max(hist_n[-window:]):
                want = "NDX"
            elif p_b > max(hist_b[-window:]):
                want = "BTC"
            else:
                want = "CASH"
            if want != held:
                equity *= 1 - cost / 10_000 * (2 if held != "CASH" and want != "CASH" else 1)
                held = want
        hist_b.append(p_b)
        hist_n.append(p_n)
        prev_b, prev_n = p_b, p_n
        out.append((d, equity, held))
    return out


def by_year(curve: list[tuple[date, float, str]]) -> dict[int, float]:
    years: dict[int, list[float]] = {}
    for (d0, e0, _), (d1, e1, _) in zip(curve, curve[1:], strict=False):
        years.setdefault(d1.year, []).append(e1 / e0 - 1)
    return {y: (fmean(v) * len(v)) * 100 for y, v in years.items()}


def buy_hold(series: dict, start: date, end: date, step: int) -> list[tuple[date, float]]:
    out = []
    d = start
    while d <= end:
        p = last_on_or_before(series, d)
        if p is not None:
            out.append((d, p))
        d += timedelta(days=step)
    if not out:
        return []
    base = out[0][1]
    return [(d, p / base) for d, p in out]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rule", required=True, choices=("dual", "donchian"))
    ap.add_argument("--from", dest="start", default="2019-01-01")
    ap.add_argument("--cost-bps", type=float, default=10.0, help="круг по споту за переключение")
    ap.add_argument("--composite", default="4,8,12", help="окна для составного сигнала, нед")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    cs = CandleStore(args.root)
    btc = closes(cs, "BTC")
    other = closes(cs, "GOLD" if args.rule == "dual" else "NDX")
    start = date.fromisoformat(args.start)
    end = min(max(btc), max(other))
    print(f"правило {args.rule} | {start} … {end} | издержки {args.cost_bps} б.п. за переключение")

    grid = (1, 2, 4, 8, 12, 20, 24) if args.rule == "dual" else (5, 10, 20, 30, 50)
    composite = tuple(int(x) for x in args.composite.split(",") if x.strip())
    unit = "нед" if args.rule == "dual" else "дн"
    print(f"\n{'окно':8}{'CAGR':>9}{'Sharpe':>9}{'просадка':>11}{'в кэше':>9}   по годам")
    for w in grid:
        curve = (
            run_dual(btc, other, w, args.cost_bps, start, end)
            if args.rule == "dual"
            else run_donchian(btc, other, w, args.cost_bps, start, end)
        )
        if len(curve) < 10:
            continue
        st = equity_stats([(d, e) for d, e, _ in curve])
        cash = sum(1 for _, _, h in curve if h == "CASH") / len(curve) * 100
        yrs = by_year(curve)
        yr = " ".join(f"{y % 100:02d}:{v:+.0f}" for y, v in sorted(yrs.items()))
        print(
            f"{w:>3} {unit:4}{st['cagr']:>8.1f}%{st['sharpe']:>9.2f}{st['dd']:>10.1f}%"
            f"{cash:>8.0f}%   {yr}"
        )

    step = 7 if args.rule == "dual" else 1
    print("\nОРИЕНТИРЫ за то же окно")
    for label, series in (("BTC купить и держать", btc), ("второй актив", other)):
        bh = buy_hold(series, start, end, step)
        st = equity_stats(bh)
        print(f"{label:22}{st['cagr']:>8.1f}%{st['sharpe']:>9.2f}{st['dd']:>10.1f}%")
    print(
        "\nЧитать так: авторы отчитались о ЛУЧШЕМ окне из сетки — это отбор по известному\n"
        "исходу. Смотреть надо на всю строку сетки и на годы: если выигрывает одно окно\n"
        "и один-два года, преимущества нет. Издержки у авторов не считались; здесь считаются."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
