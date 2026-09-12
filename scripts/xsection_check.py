#!/usr/bin/env python
"""Кросс-секционный сигнал: даёт ли ранжирование вселенной хоть что-нибудь.

Проверка ДО написания стратегии — та же дисциплина, что в `signal_check.py`, но для правил
вида «купить лучшую десятую часть рынка, продать худшую». Сюда ложатся классические
факторы, перенос которых в крипту и проверяется:

* `mom12_1` — моментум Джегадиша–Титмана: доходность за 12 месяцев БЕЗ последнего
  (последний пропускается, потому что на нём живёт краткосрочный разворот), держать месяц;
* `mom12` — то же без пропуска, для сравнения: пропуск помогает или мешает;
* `vol` — реализованная волатильность за три месяца. Аномалия низкой волатильности в акциях
  объясняется запретом на плечо у институтов: кто не может занять, покупает рискованное
  и переплачивает. В крипте плечо доступно всем и дёшево, поэтому причина не переносится —
  замер это либо подтвердит, либо нет;
* `dollar_vol` — средний оборот: контроль, а не гипотеза. Если «сигнал» есть только у него,
  значит мы измеряем ликвидность, а не идею.

**Наблюдение — это МЕСЯЦ РЕБАЛАНСА, а не монета в нём.** Шестьсот монет в один месяц ходят
вместе; шумом по монетам можно доказать что угодно. Поэтому разброс считается по месяцам,
их около семидесяти, и порог значимости получается честно высоким.

Вселенная берётся из архива листингов и содержит УМЕРШИЕ пары. Монета, пропавшая после
даты формирования, не выбрасывается: её последняя цена в следующем месяце и есть выход.

    python scripts/xsection_check.py --signal mom12_1 --root /app/data
    python scripts/xsection_check.py --signal vol --min-dollar-vol 1000000
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

SIGNALS = ("mom12_1", "mom12", "vol", "dollar_vol")
MONTHLY_COST = 0.20  # круг по тейкеру на полной смене состава, % за ребаланс

SQL = """
select date_trunc('month', ts) as m,
       arg_max(close::DOUBLE, ts) as close,
       stddev_samp(ret) as vol,
       avg(close::DOUBLE * volume::DOUBLE) as dollar_vol,
       count(*) as n
from (
    select ts, close, volume,
           close::DOUBLE / lag(close::DOUBLE) over (order by ts) - 1 as ret
    from {candles}
)
group by 1
order by 1
"""


def monthly(cs: CandleStore, name: str) -> dict[date, dict[str, float]]:
    """Месячные сводки одного инструмента: закрытие, волатильность, оборот."""
    rows = cs.query(SQL, "binance", name, "1d")
    out: dict[date, dict[str, float]] = {}
    for r in rows:
        if r["close"] is None or r["n"] < 10:  # огрызок месяца — не наблюдение
            continue
        out[r["m"].date().replace(day=1)] = {
            "close": float(r["close"]),
            "vol": float(r["vol"] or 0),
            "dollar_vol": float(r["dollar_vol"] or 0),
        }
    return out


def signal_of(kind: str, hist: dict[date, dict[str, float]], months: list[date], i: int):
    """Значение сигнала на конец месяца `months[i]` — только по прошлым данным."""
    here = hist.get(months[i])
    if here is None:
        return None
    if kind == "dollar_vol":
        return here["dollar_vol"]
    if kind == "vol":
        past = [hist[m]["vol"] for m in months[max(0, i - 2) : i + 1] if m in hist]
        return fmean(past) if len(past) == 3 else None
    lag = 1 if kind == "mom12_1" else 0
    if i - 12 - lag < 0:
        return None
    start, end = hist.get(months[i - 12 - lag]), hist.get(months[i - lag])
    if start is None or end is None or start["close"] <= 0:
        return None
    return end["close"] / start["close"] - 1


def forward(hist: dict[date, dict[str, float]], months: list[date], i: int) -> float | None:
    """Доходность следующего месяца. Пропажа инструмента — не ноль и не пропуск сделки."""
    here, nxt = hist.get(months[i]), hist.get(months[i + 1])
    if here is None or here["close"] <= 0:
        return None
    if nxt is None:
        return None  # ряд оборвался целиком: выхода не было, месяц не засчитываем
    return (nxt["close"] / here["close"] - 1) * 100


def by_year(rows: list[tuple[date, float, float, float]]) -> None:
    years: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    for m, lo, hi, allm in rows:
        years[m.year].append((lo, hi, allm))
    cols = f"{'год':7}{'мес':>5}{'низ':>10}{'верх':>10}{'вся вселенная':>15}{'верх−низ':>11}"
    print("\nПО ГОДАМ\n" + cols)
    signs: list[float] = []
    for year in sorted(years):
        vals = years[year]
        lo, hi, allm = (fmean(v[k] for v in vals) for k in range(3))
        signs.append(hi - lo)
        print(f"{year:<7}{len(vals):>5}{lo:>9.2f}%{hi:>9.2f}%{allm:>14.2f}%{hi - lo:>11.2f}")
    if len(signs) > 1:
        pos = sum(1 for s in signs if s > 0)
        print(f"знак совпадает в {max(pos, len(signs) - pos)} годах из {len(signs)}")


def risk_table(
    lo: list[float], hi: list[float], allm: list[float], bench: list[float], bench_name: str
) -> None:
    """Доходность на единицу риска — единственная честная проверка аномалии низкой волы.

    Портфель с низкой бетой в падающем рынке обгоняет вселенную просто потому, что меньше
    в ней участвует; то же даёт половина позиции в деньгах. Аномалия утверждает большее —
    что спокойные активы дают больше НА ЕДИНИЦУ РИСКА. Здесь это и сравнивается.

    Ориентир обязателен. Спокойная десятая часть крипторынка — это почти наверняка
    BTC и ETH, и тогда «аномалия» сводится к «держи биткойн», то есть к нашему обычному
    ориентиру, а не к преимуществу над ним.
    """
    print(f"\n{'портфель':24}{'за месяц':>10}{'риск (σ)':>11}{'доход/риск, год':>18}")
    rows = [
        ("нижняя десятая", lo),
        ("верхняя десятая", hi),
        ("вся вселенная", allm),
        (f"ориентир {bench_name}", bench),
    ]
    for label, s in rows:
        if not s:
            continue
        m = fmean(s)
        sd = stdev(s) if len(s) > 1 else 0.0
        ratio = (m / sd * sqrt(12)) if sd else 0.0
        print(f"{label:24}{m:>9.2f}%{sd:>10.2f}%{ratio:>18.2f}")
    print("  (безрисковой ставки здесь нет: сравниваются портфели между собой)")


def report(name: str, series: list[float], base: list[float], label: str) -> None:
    """Средняя за месяц, её шум и оба порога — значимость и издержки."""
    diff = [a - b for a, b in zip(series, base, strict=True)]
    mean = fmean(diff)
    se = stdev(diff) / sqrt(len(diff)) if len(diff) > 1 else 0.0
    if abs(mean) < 2 * se:
        verdict = "в пределах шума"
    elif abs(mean) < MONTHLY_COST:
        verdict = "меньше издержек"
    else:
        verdict = "ПЕРЕЖИВАЕТ ОБА ПОРОГА"
    print(f"{name:24}{mean:>9.2f}%{2 * se:>11.2f}{mean * 12:>12.1f}%   {verdict}   ({label})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--signal", default="mom12_1", choices=SIGNALS)
    ap.add_argument("--universe", default="universe-1d.txt")
    ap.add_argument("--decile", type=float, default=10.0, help="размер края, %%")
    ap.add_argument("--min-names", type=int, default=20, help="сколько монет нужно для дециля")
    ap.add_argument("--min-dollar-vol", type=float, default=0.0, help="фильтр оборота, $/день")
    ap.add_argument("--from-year", type=int, default=2019)
    ap.add_argument("--benchmark", default="BTC/USDT", help="ориентир для сравнения")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    names = [ln.strip() for ln in (root / args.universe).read_text().splitlines() if ln.strip()]
    cs = CandleStore(root)
    print(f"вселенная: {len(names)} рядов | сигнал: {args.signal}")

    hists: dict[str, dict[date, dict[str, float]]] = {}
    for i, name in enumerate(names, 1):
        try:
            h = monthly(cs, name)
        except Exception as err:  # noqa: BLE001 — битый ряд не должен ронять весь разбор
            print(f"  {name}: {type(err).__name__}")
            continue
        if h:
            hists[name] = h
        if i % 100 == 0:
            print(f"  прочитано {i}/{len(names)}")

    # Список месяцев НЕ обрезается по --from-year: сигналу нужна предыстория, иначе первый
    # год замера окажется пустым не потому, что данных нет, а потому что мы их отрезали.
    all_months = sorted({m for h in hists.values() for m in h})
    if len(all_months) < 24:
        print("месяцев слишком мало")
        return 0

    rows: list[tuple[date, float, float, float]] = []
    membership: list[tuple[date, list[str], list[str]]] = []
    for i in range(len(all_months) - 1):
        if all_months[i].year < args.from_year:
            continue
        picks: list[tuple[float, float, str]] = []
        for name, h in hists.items():
            sig = signal_of(args.signal, h, all_months, i)
            fwd = forward(h, all_months, i)
            if sig is None or fwd is None:
                continue
            if args.min_dollar_vol and h[all_months[i]]["dollar_vol"] < args.min_dollar_vol:
                continue
            picks.append((sig, fwd, name))
        if len(picks) < args.min_names:
            continue
        picks.sort(key=lambda p: p[0])
        k = max(1, int(len(picks) * args.decile / 100))
        membership.append((all_months[i], [p[2] for p in picks[:k]], [p[2] for p in picks[-k:]]))
        rows.append(
            (
                all_months[i],
                fmean(p[1] for p in picks[:k]),
                fmean(p[1] for p in picks[-k:]),
                fmean(p[1] for p in picks),
            )
        )

    if len(rows) < 12:
        print("ребалансов слишком мало")
        return 0
    lo = [r[1] for r in rows]
    hi = [r[2] for r in rows]
    allm = [r[3] for r in rows]
    print(f"\nребалансов: {len(rows)} ({rows[0][0]} … {rows[-1][0]})")
    print(f"вся вселенная, равные веса: {fmean(allm):.2f}% за месяц\n")
    print(f"{'портфель':24}{'за месяц':>9}{'шум (2σ)':>11}{'за год':>12}   вердикт")
    report("верхняя десятая", hi, allm, "лонг лучших")
    report("нижняя десятая", lo, allm, "лонг худших")
    report("верх минус низ", hi, lo, "рыночно-нейтральный")
    # Ориентир считается по ТЕМ ЖЕ месяцам, иначе сравниваются разные куски истории.
    bench_hist = hists.get(args.benchmark, {})
    bench = [
        (bench_hist[m2]["close"] / bench_hist[m1]["close"] - 1) * 100
        for m1, m2 in zip(all_months, all_months[1:], strict=False)
        if m1 in {r[0] for r in rows} and m1 in bench_hist and m2 in bench_hist
    ]
    risk_table(lo, hi, allm, bench, args.benchmark)
    if membership:
        last = membership[-1]
        print(f"\nсостав на {last[0]} (низ): {', '.join(sorted(last[1]))}")
        print(f"состав на {last[0]} (верх): {', '.join(sorted(last[2]))}")
        stayed = sorted(set.intersection(*(set(m[1]) for m in membership[-12:])))
        print(f"в нижней десятой все последние 12 ребалансов: {', '.join(stayed) or '—'}")
    by_year(rows)
    print(
        f"\nДва порога подряд: больше собственного шума (2σ) и больше издержек ребаланса\n"
        f"(~{MONTHLY_COST}% за смену состава, то есть ~{MONTHLY_COST * 12:.1f}% в год).\n"
        "Шум считается по МЕСЯЦАМ: монеты внутри месяца — одно наблюдение, а не шестьсот."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
