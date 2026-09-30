#!/usr/bin/env python
"""«Дно по ATR» Pifagor: есть ли у зоны дна преимущество сверх обычного отскока.

Вопрос владельца 30.09.2026. Индикатор закрытый; на сайте автора сказано только
«поиск потенциального дна в средне- и долгосрочной перспективе» и что база — ATR
(`docs/research/indicators/pifagor.md`, строка 4). Наша версия v0 та же, что в
`lab.strategies.indicators.pifagor.AtrBottom`: зона дна = закрытие ниже SMA(n) − k·ATR(n).
Формулы автора мы не знаем, поэтому n и k — сетка, и ищется ПЛАТО, а не лучшая клетка.

Только лонг, дневные свечи, вход по открытию следующего дня, выход через H дней по открытию.
Два режима входа:

* `вход` — первый день, когда закрытие ушло в зону (так читается сигнал «зелёная волна»);
* `зона` — каждый день в зоне (так им пользуется накопитель: докупает, пока зона горит).

Три сравнения, каждое строже предыдущего:

* `обыч` — против обычного дня того же набора монет: есть ли эффект вообще (шаг 1);
* `изб n` — против дня с ТАКИМ ЖЕ ходом цены за n дней, то есть в масштабе самого правила
  (шаг 2). Индикатор, который «знает» лишь то, что цена упала, здесь обнуляется;
* `изб n+10` — ход за n дней И ход за последние 10 дней: зона дна почти всегда наступает
  после обвала, и свежий обвал даёт свой отскок, которого правило не заслуживает.

Вердикт — по самому строгому сравнению. Шум — по независимым блокам длиной в горизонт
(восемь монет в один день и соседние дни с общим удержанием — одно свидетельство).
Колонка `корз` — медианный размер корзины контроля: у редких глубоких зон корзина
состоит почти из самих сигналов, и избыток тогда прижимается к нулю (ошибка в сторону
«шума», не в сторону находки).

    python scripts/atr_bottom_check.py --root /app/data
    python scripts/atr_bottom_check.py --root /app/data --venue bitstamp --instruments BTC/USD
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from math import sqrt
from pathlib import Path
from statistics import fmean, median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freqtrade_check import COST_PCT, INSTRUMENTS, sma  # noqa: E402
from youtube_check import atr, block_stats, load, verdict_of  # noqa: E402

from lab.data.store import CandleStore  # noqa: E402

SHORT = 10  # второй масштаб контроля, дней: свежий обвал


def bucket(moved: float, days: int) -> int:
    """Номер корзины хода: ширина 2 п.п. на корень из дня, края обрезаны."""
    width = 2.0 * sqrt(days)
    return max(-10, min(10, int(moved // width)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data")
    ap.add_argument("--venue", default="binance")
    ap.add_argument("--instruments", default=",".join(INSTRUMENTS))
    ap.add_argument("--n", default="20,50,100,200", help="периоды SMA/ATR через запятую")
    ap.add_argument("--k", default="1,1.5,2,2.5,3", help="множители ATR через запятую")
    ap.add_argument("--horizons", default="10,30,60", help="удержание, дней")
    args = ap.parse_args()
    ns = [int(x) for x in args.n.split(",")]
    ks = [float(x) for x in args.k.split(",")]
    hs = [int(x) for x in args.horizons.split(",")]
    warm = max(max(ns) + 1, 200)

    cs = CandleStore(args.root)
    series = []
    covered = []
    for inst in args.instruments.split(","):
        bars = load(cs, args.venue, inst, "1d")
        if len(bars) < warm + max(hs) + 50:
            print(f"{inst}: баров {len(bars)} — пропуск")
            continue
        covered.append(f"{inst}({len(bars)}, с {bars[0]['ts']:%Y-%m})")
        series.append(bars)

    # key -> [(блок, год, исход, ключ корзины n, ключ корзины n+10)]
    events: dict[tuple, list[tuple]] = defaultdict(list)
    # (H, n, корзина) -> [сумма, штук]; (H, n, корзина, корзина10) -> то же; H -> все дни
    peer_n: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0])
    peer_2: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0])
    plain: dict[int, list[float]] = defaultdict(list)

    for bars in series:
        o = [b["o"] for b in bars]
        h = [b["h"] for b in bars]
        lo = [b["l"] for b in bars]
        c = [b["c"] for b in bars]
        lines = {}
        for n in ns:
            s, a = sma(c, n), atr(h, lo, c, n)
            for k in ks:
                lines[(n, k)] = [
                    None if si is None or ai is None else si - k * ai
                    for si, ai in zip(s, a, strict=True)
                ]
        for hz in hs:
            for i in range(warm, len(bars) - hz - 1):
                ret = (o[i + 1 + hz] / o[i + 1] - 1) * 100
                day = bars[i]["ts"].date()
                blk = day.toordinal() // hz
                plain[hz].append(ret)
                b10 = bucket((c[i] / c[i - SHORT] - 1) * 100, SHORT)
                for n in ns:
                    bn = bucket((c[i] / c[i - n] - 1) * 100, n)
                    for store, cell in ((peer_n, (hz, n, bn)), (peer_2, (hz, n, bn, b10))):
                        store[cell][0] += ret
                        store[cell][1] += 1
                    for k in ks:
                        line = lines[(n, k)]
                        if line[i] is None or line[i - 1] is None:
                            continue
                        now, before = c[i] < line[i], c[i - 1] < line[i - 1]
                        if not now:
                            continue
                        ev = (blk, day.year, ret, (hz, n, bn), (hz, n, bn, b10))
                        events[("зона", hz, n, k)].append(ev)
                        if not before:
                            events[("вход", hz, n, k)].append(ev)

    print(f"Ряды {args.venue}: {', '.join(covered)}")
    print(f"Зона дна = закрытие < SMA(n) − k·ATR(n); лонг, вход по следующему открытию, "
          f"круг издержек {COST_PCT:.2f}%")
    for mode in ("вход", "зона"):
        for hz in hs:
            base = fmean(plain[hz])
            print(f"\n== {mode}, удержание {hz} дн. (обычный день {base:+.2f}%, "
                  f"блок {hz} дн.) ==")
            print(f"{'n':>4}{'k':>5}{'сигн':>7}{'блоков':>7}{'сырой':>8}{'обыч':>8}"
                  f"{'изб n':>8}{'изб n+10':>9}{'2σ':>7}{'корз':>6}{'годы+':>7}  вердикт")
            for n in ns:
                for k in ks:
                    ev = events[(mode, hz, n, k)]
                    if not ev:
                        print(f"{n:>4}{k:>5g}{0:>7}   сигналов нет")
                        continue
                    raw = fmean(e[2] for e in ev)
                    vs_plain = raw - base
                    exc_n, _, _ = block_stats(
                        [(e[0], e[2] - peer_n[e[3]][0] / peer_n[e[3]][1]) for e in ev]
                    )
                    pairs = [(e[0], e[2] - peer_2[e[4]][0] / peer_2[e[4]][1]) for e in ev]
                    exc2, two_se, nblk = block_stats(pairs)
                    size = median(peer_2[e[4]][1] for e in ev)
                    per_year: dict[int, list[tuple[int, float]]] = defaultdict(list)
                    for (blk, x), e in zip(pairs, ev, strict=True):
                        per_year[e[1]].append((blk, x))
                    yrs = [block_stats(p)[0] for p in per_year.values()
                           if len({b for b, _ in p}) >= 3]
                    yrs_pos = sum(1 for y in yrs if y > 0)
                    verdict = verdict_of(nblk, exc2, two_se, yrs_pos, len(yrs))
                    print(f"{n:>4}{k:>5g}{len(ev):>7}{nblk:>7}{raw:>+8.2f}{vs_plain:>+8.2f}"
                          f"{exc_n:>+8.2f}{exc2:>+9.2f}{two_se:>7.2f}{size:>6.0f}"
                          f"{f'{yrs_pos}/{len(yrs)}':>7}  {verdict}")
    print("\nсырой — средний исход сигнала, %; обыч — сырой минус обычный день; изб n — минус")
    print("обычный день с тем же ходом за n дней; изб n+10 — ещё и с тем же ходом за 10 дней.")
    print("2σ, блоки, годы и вердикт — по изб n+10. корз — медиана дней в корзине контроля.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
