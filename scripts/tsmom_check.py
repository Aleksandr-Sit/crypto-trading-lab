#!/usr/bin/env python
"""Абсолютный моментум на мировых активах: проверка D7 ДО написания стратегии.

Два классических правила, которые чаще всего и отдают программистам в бота, потому что
они короткие и полностью выписаны:

* `faber` — правило Фабера (Meb Faber, «A Quantitative Approach to Tactical Asset
  Allocation», 2007): раз в месяц — если цена выше своей N-месячной скользящей, держим
  актив, иначе кэш. Заявлено: та же доходность при вдвое меньшей просадке.
* `mom12_1` — моментум Джегадиша–Титмана в НАБОРНОМ виде: доходность за 12 месяцев без
  последнего, верхняя половина набора против нижней. Последний месяц пропускается,
  потому что на нём живёт краткосрочный разворот.

Чего не было в источниках и что проверяется здесь:

* **издержки** — круг за каждое переключение (`--cost-bps`), у мировых активов они
  платятся через CFD или фьючерс и сами по себе съедают часть правила;
* **шум по МЕСЯЦАМ, а не по активам.** Шестнадцать активов в один месяц ходят вместе:
  шестнадцать подтверждений — это одно, повторённое шестнадцать раз. Наблюдение здесь
  месяц ребаланса, их около двухсот сорока, и порог значимости получается честным;
* **по годам** — режим, выданный за закономерность, виден только так;
* **ориентир** — равновзвешенное «купить и держать» по тому же набору. Без него правило,
  которое просто сидит в растущем рынке, выглядит открытием.

    python scripts/tsmom_check.py --rule faber --root /app/data
    python scripts/tsmom_check.py --rule mom12_1 --venue yahoo --cost-bps 10
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

WORLD = (
    "SPX,NDX,DJI,DAX,FTSE,NIKKEI,XAUUSD,XAGUSD,WTI,NATGAS,"
    "EURUSD,GBPUSD,USDJPY,USDCHF,USDCAD,AUDUSD"
)


def month_closes(cs: CandleStore, venue: str, name: str, tf: str) -> dict[tuple[int, int], float]:
    """Закрытия по месяцам: последний бар месяца."""
    rows = cs.query(
        "select ts, close::DOUBLE as c from {candles} order by ts",
        venue,
        name,
        tf,
    )
    out: dict[tuple[int, int], float] = {}
    for r in rows:
        if r["c"] and r["c"] > 0:
            out[(r["ts"].year, r["ts"].month)] = r["c"]
    return out


def sma(series: list[float], n: int) -> float | None:
    return fmean(series[-n:]) if len(series) >= n else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rule", required=True, choices=("faber", "mom12_1"))
    ap.add_argument("--venue", default="yahoo")
    ap.add_argument("--instruments", default=WORLD)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--window", type=int, default=10, help="месяцев в скользящей (faber)")
    ap.add_argument("--cost-bps", type=float, default=10.0, help="круг за переключение")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    cs = CandleStore(args.root)
    names = [s.strip() for s in args.instruments.split(",") if s.strip()]
    closes = {n: month_closes(cs, args.venue, n, args.tf) for n in names}
    closes = {n: c for n, c in closes.items() if len(c) > 36}
    if not closes:
        print("рядов нет")
        return 0
    months = sorted(set().union(*(set(c) for c in closes.values())))
    print(f"активов: {len(closes)}, месяцев: {len(months)} ({months[0]} — {months[-1]})")

    # сигнал по каждому активу на каждый месяц + доходность СЛЕДУЮЩЕГО месяца
    on: dict[tuple[int, int], list[float]] = defaultdict(list)
    off: dict[tuple[int, int], list[float]] = defaultdict(list)
    held: dict[tuple[int, int], list[float]] = defaultdict(list)  # доходность правила
    bench: dict[tuple[int, int], list[float]] = defaultdict(list)  # купить и держать
    prev_on: dict[str, bool] = {}
    top: set[str] = set()
    switches = 0

    for i, m in enumerate(months[:-1]):
        nxt = months[i + 1]
        if args.rule == "mom12_1":
            scored = []
            for n, c in closes.items():
                if m not in c or nxt not in c or i < 13:
                    continue
                a, b = months[i - 12], months[i - 1]
                if a in c and b in c and c[a] > 0:
                    scored.append((c[b] / c[a] - 1, n))
            if len(scored) < 4:
                continue
            scored.sort(reverse=True)
            top = {n for _, n in scored[: len(scored) // 2]}
        for n, c in closes.items():
            if m not in c or nxt not in c:
                continue
            fwd = (c[nxt] / c[m] - 1) * 100
            bench[m].append(fwd)
            if args.rule == "faber":
                hist = [c[k] for k in months[: i + 1] if k in c]
                ma = sma(hist, args.window)
                signal = ma is not None and c[m] > ma
            else:
                signal = n in top
            (on if signal else off)[m].append(fwd)
            # Издержки списываются ЗА ПЕРЕКЛЮЧЕНИЕ, а не за каждый месяц удержания:
            # иначе правило, которое год сидит в активе, платит двенадцать кругов вместо
            # одного, и вердикт выносится издержкам, а не правилу.
            changed = prev_on.get(n) != signal
            if changed:
                switches += 1
                prev_on[n] = signal
            cost = args.cost_bps / 100 if changed else 0.0
            held[m].append((fwd - cost) if signal else -cost)

    def stats(d: dict[tuple[int, int], list[float]]) -> tuple[int, float, float]:
        per_month = [fmean(v) for v in d.values() if v]
        if not per_month:
            return 0, 0.0, 0.0
        se = stdev(per_month) / sqrt(len(per_month)) if len(per_month) > 1 else 0.0
        return len(per_month), fmean(per_month), se

    n_on, m_on, se_on = stats(on)
    n_off, m_off, se_off = stats(off)
    diff = m_on - m_off
    noise = 2 * sqrt(se_on**2 + se_off**2)
    label = "цена выше скользящей" if args.rule == "faber" else "верхняя половина по 12-1"
    other = "цена ниже скользящей" if args.rule == "faber" else "нижняя половина"
    print(f"\n{'группа':40}{'месяцев':>10}{'вперёд':>12}")
    print(f"{label:40}{n_on:>10}{m_on:>11.3f}%")
    print(f"{other:40}{n_off:>10}{m_off:>11.3f}%")
    verdict = "в пределах шума" if abs(diff) < noise else "больше собственного шума"
    print(f"\nразница {diff:+.3f} п.п. в месяц   шум (2σ) ±{noise:.3f}  → {verdict}")

    # итог правила против ориентира, с издержками
    def compound(d: dict[tuple[int, int], list[float]]) -> tuple[float, float]:
        eq, peak, dd = 1.0, 1.0, 0.0
        for m in months[:-1]:
            if m in d and d[m]:
                eq *= 1 + fmean(d[m]) / 100
                peak = max(peak, eq)
                dd = max(dd, (peak - eq) / peak)
        years = len(months) / 12
        return (eq ** (1 / years) - 1) * 100, dd * 100

    r_cagr, r_dd = compound(held)
    b_cagr, b_dd = compound(bench)
    print(f"\n{'':22}{'годовых':>10}{'просадка':>12}")
    print(f"{'правило (с издержками)':22}{r_cagr:>9.2f}%{r_dd:>11.2f}%")
    print(f"{'равновзвешенно держать':22}{b_cagr:>9.2f}%{b_dd:>11.2f}%")
    print(f"переключений: {switches}, круг {args.cost_bps} б.п.")

    print(f"\n{'год':7}{'месяцев':>9}{'правило':>11}{'держать':>11}{'разница':>10}")
    years_r: dict[int, list[float]] = defaultdict(list)
    years_b: dict[int, list[float]] = defaultdict(list)
    for m in months[:-1]:
        if m in held and held[m]:
            years_r[m[0]].append(fmean(held[m]))
            years_b[m[0]].append(fmean(bench[m]))
    signs = []
    for y in sorted(years_r):
        a, b = sum(years_r[y]), sum(years_b[y])
        signs.append(a - b)
        print(f"{y:<7}{len(years_r[y]):>9}{a:>10.2f}%{b:>10.2f}%{a - b:>10.2f}")
    if len(signs) > 1:
        pos = sum(1 for s in signs if s > 0)
        print(f"правило впереди в {pos} годах из {len(signs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
