#!/usr/bin/env python
"""Hash Ribbons (Capriole): даёт ли сигнал «майнеры вышли из капитуляции» что-то сверх цены.

П.8 плана трейдеров, второй пункт после CryptosMX (`teaching-traders-2026-10-03.md`).
Правило публичное и целиком на открытых данных, поэтому проверяется само правило, а не автор.

Сигнал (как описан автором; объявлено до прогона, 11.10.2026):
* капитуляция — SMA(30) хешрейта ниже SMA(60);
* восстановление — SMA(30) снова выше SMA(60);
* покупка — первый день от восстановления, когда SMA(10) цены выше SMA(20), и не позже
  начала следующей капитуляции; одна покупка на капитуляцию;
* вход — цена следующего дня (сигнал известен после закрытия суток).

Сигналов мало (≈ раз в год), поэтому не хвосты распределения, как в `signal_check.py`, а
разбор по событиям. Контроль ЛОКАЛЬНЫЙ: у BTC сменился режим (+1622% в год в 2011–2013 против
единиц процентов сейчас), и сравнение с «обычным днём за всю историю» награждало бы ранние годы.
* контроль 1 — любой день в пределах ±WINDOW дней от сигнала (вопрос DCA: лучше ли купить в день
  сигнала, чем в случайный день того же года);
* контроль 2 — дни того же окна, когда SMA(10) цены только что пересекла SMA(20) снизу вверх
  (не работает ли один ценовой фильтр без майнеров — урок Freqtrade и ютуб-техник).
Доходность — логарифмическая, в % (ln × 100). Избыток события = доходность сигнала минус
среднее его контроля; шум — по событиям (они в среднем через год, на горизонтах до 180 дней
независимы; на 365 соседние могут перекрываться — печатается число перекрытий).
Плюс перестановка: из `--draws` наборов случайных дней (по одному из окна каждого сигнала)
сколько дали средний итог хуже сигнала.

    python scripts/hash_ribbons_check.py --data ../lab-data/hash-ribbons/cm_btc.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from math import log, sqrt
from pathlib import Path
from statistics import mean, stdev

HORIZONS = (30, 90, 180, 365)
WINDOW = 365


def load(path: Path) -> tuple[list[date], list[float | None], list[float | None]]:
    rows = json.loads(path.read_text(encoding="utf-8"))["data"]
    days, hr, px = [], [], []
    for r in rows:
        days.append(date.fromisoformat(r["time"][:10]))
        hr.append(float(r["HashRate"]) if r.get("HashRate") else None)
        px.append(float(r["PriceUSD"]) if r.get("PriceUSD") else None)
    for a, b in zip(days, days[1:], strict=False):
        if (b - a).days != 1:
            raise SystemExit(f"дыра в ряду: {a} → {b}")
    return days, hr, px


def sma(xs: list[float | None], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1 : i + 1]
        if all(v is not None for v in w):
            out[i] = sum(w) / n  # type: ignore[arg-type]
    return out


def signals(hr: list[float | None], px: list[float | None]) -> tuple[list[int], list[int], int]:
    """Индексы дней сигнала, дни пересечения цены (контроль 2) и число капитуляций."""
    h30, h60 = sma(hr, 30), sma(hr, 60)
    p10, p20 = sma(px, 10), sma(px, 20)
    out: list[int] = []
    crosses: list[int] = []
    in_cap = False
    armed = False  # восстановление было, покупки ещё не было
    caps = 0
    for i in range(1, len(hr)):
        if None not in (p10[i], p20[i], p10[i - 1], p20[i - 1]):
            if p10[i - 1] <= p20[i - 1] and p10[i] > p20[i]:  # type: ignore[operator]
                crosses.append(i)
        if h30[i] is None or h60[i] is None:
            continue
        if h30[i] < h60[i]:  # type: ignore[operator]
            if not in_cap:
                caps += 1
            in_cap, armed = True, False
            continue
        if in_cap:
            in_cap, armed = False, True
        if armed and p10[i] is not None and p20[i] is not None and p10[i] > p20[i]:
            out.append(i)
            armed = False
    return out, crosses, caps


def fwd(px: list[float | None], i: int, h: int) -> float | None:
    """Лог-доходность в % от цены дня i+1 (вход) до дня i+1+h."""
    a, b = i + 1, i + 1 + h
    if b >= len(px) or px[a] is None or px[b] is None:
        return None
    return log(px[b] / px[a]) * 100  # type: ignore[operator]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="JSON Coin Metrics: HashRate, PriceUSD, 1d")
    ap.add_argument("--draws", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    days, hr, px = load(Path(args.data))
    sig, crosses, caps = signals(hr, px)
    print(f"ряд {days[0]} … {days[-1]}, капитуляций {caps}, сигналов {len(sig)}")
    for i in sig:
        r = {h: fwd(px, i, h) for h in HORIZONS}
        cells = "  ".join(f"{h}д {r[h]:+7.1f}" if r[h] is not None else f"{h}д    —   " for h in HORIZONS)
        print(f"  {days[i]}  цена {px[i]:>9.2f}  {cells}")

    rng = random.Random(args.seed)
    cross_set = set(crosses)
    print("\nизбыток = сигнал − среднее локального контроля (±365 д), лог-%, шум — по событиям")
    print(f"{'гор.':>5} {'n':>3} {'сигнал':>8} | {'к1 любой день':>14} {'±шум':>6} {'хуже/1000':>9} | {'к2 крест цены':>14} {'±шум':>6} {'хуже/1000':>9}")
    for h in HORIZONS:
        ev = [i for i in sig if fwd(px, i, h) is not None]
        if len(ev) < 3:
            continue
        pools: dict[str, list[list[float]]] = {"any": [], "cross": []}
        sig_r = []
        for i in ev:
            lo, hi = max(0, i - WINDOW), min(len(px) - 1, i + WINDOW)
            any_r = [v for j in range(lo, hi + 1) if j != i and (v := fwd(px, j, h)) is not None]
            cr_r = [v for j in range(lo, hi + 1) if j in cross_set and j != i and (v := fwd(px, j, h)) is not None]
            pools["any"].append(any_r)
            pools["cross"].append(cr_r or any_r)
            sig_r.append(fwd(px, i, h))
        out = []
        for key in ("any", "cross"):
            ex = [s - mean(p) for s, p in zip(sig_r, pools[key], strict=True)]  # type: ignore[operator]
            se = stdev(ex) / sqrt(len(ex))
            target = mean(sig_r)  # type: ignore[arg-type]
            worse = sum(mean(rng.choice(p) for p in pools[key]) < target for _ in range(args.draws))
            out.append(f"{mean(ex):>+14.1f} {se:>6.1f} {worse * 1000 // args.draws:>9}")
        overlap = sum(1 for a, b in zip(ev, ev[1:], strict=False) if b - a < h)
        tag = f"  (перекрытий {overlap})" if overlap else ""
        print(f"{h:>4}д {len(ev):>3} {mean(sig_r):>+8.1f} | {out[0]} | {out[1]}{tag}")  # type: ignore[arg-type]

    print("\nпо эпохам (избыток к1, горизонт 180 д):")
    for a, b in ((2011, 2016), (2017, 2021), (2022, 2026)):
        ex = []
        for i in sig:
            if a <= days[i].year <= b and (s := fwd(px, i, 180)) is not None:
                lo, hi = max(0, i - WINDOW), min(len(px) - 1, i + WINDOW)
                pool = [v for j in range(lo, hi + 1) if j != i and (v := fwd(px, j, 180)) is not None]
                ex.append(s - mean(pool))
        if ex:
            print(f"  {a}–{b}: n {len(ex)}, избыток {mean(ex):+.1f}, плюсовых {sum(e > 0 for e in ex)} из {len(ex)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
