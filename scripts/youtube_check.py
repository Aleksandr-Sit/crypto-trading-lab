#!/usr/bin/env python
"""Прямая проверка техник, которым учат ютуб-преподаватели трейдинга (п.7 плана трейдеров).

Вопрос владельца 29.09.2026: на ютубе много трейдеров, которые учат торговать крипту;
разобрать их технику по очереди и тестом понять, что работает. Часть школьной программы
уже измерена раньше и здесь НЕ повторяется: RSI-2, Боллинджер+RSI, MACD, ADX, TD Sequential
(`freqtrade-2026-09-14.md` — сводятся к просадке), снятие ликвидности SMC/ICT, пробой
диапазона открытия, коридор шума (`bots-2026-09-20.md` — обратный знак), черепахи, пробой
с трейлингом, сетки, мартингейл, DCA-бот (`measurements-2026-09-07.md`). Здесь — то, что
осталось: дивергенции, FVG, фибо, структура, пробой с ретестом, откат к EMA, свечные
паттерны у экстремума, Supertrend, Ишимоку, золотой крест, недельный VWAP.

**Каждое правило — канонический вариант из обучающих роликов, без подбора параметров.**
Лонг и шорт зеркальны. Сигнал знает только прошлое: разворотная точка (фрактал) признаётся
через `K` баров после самой точки, вход — по открытию СЛЕДУЮЩЕГО бара после сигнала.

**Контроль — обычный бар при ТОМ ЖЕ недавнем ходе цены**, в ту же сторону, что сигнал.
Почти все ютуб-входы — «после падения купить» или «после роста продать»; без этого контроля
правило получает в подарок обычный отскок (так исчезли BbandRsi и TD Sequential), а в бычьи
годы лонги — дрейф рынка. Избыток сигнала = знак × (исход − средний исход корзины).

**Шум — по независимым блокам времени**, длиной в горизонт удержания: восемь монет в один
день и соседние дни с перекрывающимся удержанием — одно свидетельство, а не десять.

Два порога подряд, оба обязательны: избыток больше 2σ шума И больше круга по издержкам.
Прошедшее — ещё не стратегия: дальше устойчивость по годам (печатается здесь же) и только
потом карточка, издержки, стоп и замер.

    python scripts/youtube_check.py --root /app/data --tf 4h --horizon 6
    python scripts/youtube_check.py --root /app/data --tf 1d --horizon 20
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from math import ceil, sqrt
from pathlib import Path
from statistics import fmean

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freqtrade_check import COST_PCT, INSTRUMENTS, ema, rsi, sma, wilder  # noqa: E402

from lab.data.store import CandleStore  # noqa: E402

K = 3  # фрактал: разворотная точка — экстремум среди K баров слева и K справа
RULES = (
    "rsi_div",
    "choch",
    "fib_golden",
    "fvg_tap",
    "break_retest",
    "ema_pullback",
    "engulf_extreme",
    "pinbar_extreme",
    "supertrend",
    "ichimoku",
    "golden_cross",
    "vwap_week",
    "macd_trend",
    "ema_cross_trend",
    "multi_oversold",
)
LABELS = {
    "rsi_div": "дивергенция RSI(14) на фракталах",
    "choch": "смена характера: пробой последней вершины после нисходящих",
    "fib_golden": "откат к 0.618 фибо импульса (зона 0.618–0.786)",
    "fvg_tap": "касание FVG (имбаланса) в течение 30 баров",
    "break_retest": "пробой 20-барного максимума и ретест уровня",
    "ema_pullback": "откат к EMA21 при EMA21>55>200",
    "engulf_extreme": "поглощение у 20-барного экстремума",
    "pinbar_extreme": "пин-бар у 20-барного экстремума",
    "supertrend": "смена цвета Supertrend(10, 3)",
    "ichimoku": "выход из облака Ишимоку при Tenkan>Kijun",
    "golden_cross": "золотой/мёртвый крест SMA50/200",
    "vwap_week": "возврат выше/ниже недельного VWAP",
    "macd_trend": "MACD пересекает сигнальную ниже нуля, по EMA200 (Trading Rush)",
    "ema_cross_trend": "EMA9 пересекает EMA21, по EMA200 (Trading Rush)",
    "multi_oversold": "RSI<30, MFI<20, StochRSI<20 разом (Crypto Crew University)",
}


# -- данные -----------------------------------------------------------------------------


def load(cs: CandleStore, venue: str, inst: str, tf: str) -> list[dict]:
    """Свечи инструмента. 4h собирается из часовых: 4h в хранилище не лежит."""
    src = "1h" if tf == "4h" else tf
    rows = cs.query(
        "select ts, open::DOUBLE o, high::DOUBLE h, low::DOUBLE l, close::DOUBLE c, "
        "volume::DOUBLE v from {candles} order by ts",
        venue, inst, src,
    )
    bars = [
        {"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"] or 0.0}
        for r in rows
        if r["c"] and r["c"] > 0 and r["o"] and r["o"] > 0 and r["h"] and r["l"]
    ]
    return resample(bars, 4) if tf == "4h" else bars


def resample(bars: list[dict], hours: int) -> list[dict]:
    """Часовые → `hours`-часовые по границам UTC; неполный блок выбрасывается целиком."""
    groups: dict[datetime, list[dict]] = defaultdict(list)
    for b in bars:
        ts = b["ts"]
        key = ts.replace(hour=ts.hour // hours * hours, minute=0, second=0, microsecond=0)
        groups[key].append(b)
    out = []
    for key in sorted(groups):
        g = groups[key]
        if len(g) != hours:
            continue
        out.append({
            "ts": key, "o": g[0]["o"], "h": max(x["h"] for x in g), "l": min(x["l"] for x in g),
            "c": g[-1]["c"], "v": sum(x["v"] for x in g),
        })
    return out


# -- индикаторы, которых нет в freqtrade_check -----------------------------------------


def atr(high: list[float], low: list[float], close: list[float], n: int) -> list[float | None]:
    tr = [high[0] - low[0]] + [
        max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        for i in range(1, len(close))
    ]
    return wilder(tr, n)


def midpoint(high: list[float], low: list[float], n: int) -> list[float | None]:
    """(максимум + минимум) / 2 за n баров — линии Ишимоку."""
    return [
        None if i < n - 1 else (max(high[i - n + 1 : i + 1]) + min(low[i - n + 1 : i + 1])) / 2
        for i in range(len(high))
    ]


def supertrend(high, low, close, n: int = 10, mult: float = 3.0) -> list[int]:
    """Направление Supertrend: +1 зелёный, −1 красный, 0 пока ATR не набрался."""
    a = atr(high, low, close, n)
    out = [0] * len(close)
    ub = lb = None
    d = 0
    for i in range(len(close)):
        if a[i] is None:
            continue
        mid = (high[i] + low[i]) / 2
        bu, bl = mid + mult * a[i], mid - mult * a[i]
        prev_ub, prev_lb = ub, lb
        ub = bu if prev_ub is None or bu < prev_ub or close[i - 1] > prev_ub else prev_ub
        lb = bl if prev_lb is None or bl > prev_lb or close[i - 1] < prev_lb else prev_lb
        if d == 0:
            d = 1 if close[i] > mid else -1
        elif d == -1 and prev_ub is not None and close[i] > prev_ub:
            d = 1
        elif d == 1 and prev_lb is not None and close[i] < prev_lb:
            d = -1
        out[i] = d
    return out


def mfi(high, low, close, vol, n: int = 14) -> list[float | None]:
    """Money Flow Index: RSI по денежному потоку типичной цены, простые суммы за n."""
    tp = [(a + b + x) / 3 for a, b, x in zip(high, low, close, strict=True)]
    pos = [0.0] + [tp[i] * vol[i] if tp[i] > tp[i - 1] else 0.0 for i in range(1, len(tp))]
    neg = [0.0] + [tp[i] * vol[i] if tp[i] < tp[i - 1] else 0.0 for i in range(1, len(tp))]
    out: list[float | None] = [None] * len(tp)
    for i in range(n, len(tp)):
        p, m = sum(pos[i - n + 1 : i + 1]), sum(neg[i - n + 1 : i + 1])
        out[i] = 100.0 if m == 0 else 100 - 100 / (1 + p / m)
    return out


def stoch_rsi(r: list[float | None], n: int = 14, smooth: int = 3) -> list[float | None]:
    """StochRSI %K: положение RSI в его диапазоне за n, сглаженное SMA(smooth), 0–100."""
    raw: list[float | None] = [None] * len(r)
    for i in range(n - 1, len(r)):
        win = r[i - n + 1 : i + 1]
        if None in win:
            continue
        lo_, hi_ = min(win), max(win)
        raw[i] = 50.0 if hi_ == lo_ else (r[i] - lo_) / (hi_ - lo_) * 100
    out: list[float | None] = [None] * len(r)
    for i in range(smooth - 1, len(r)):
        win = raw[i - smooth + 1 : i + 1]
        if None not in win:
            out[i] = sum(win) / smooth
    return out


def fractals(high: list[float], low: list[float], k: int) -> tuple[list[bool], list[bool]]:
    """Вершины и впадины: экстремум окна ±k, строго выше/ниже левых соседей."""
    n = len(high)
    ph, pl = [False] * n, [False] * n
    for j in range(k, n - k):
        if high[j] == max(high[j - k : j + k + 1]) and high[j] > max(high[j - k : j]):
            ph[j] = True
        if low[j] == min(low[j - k : j + k + 1]) and low[j] < min(low[j - k : j]):
            pl[j] = True
    return ph, pl


# -- правила ----------------------------------------------------------------------------


def rules(bars: list[dict]) -> dict[str, list[int]]:
    """Направление сигнала по каждому правилу на каждом баре: +1 лонг, −1 шорт, 0 нет.

    Сигнал на баре i использует данные только до закрытия i включительно.
    """
    n = len(bars)
    o = [b["o"] for b in bars]
    h = [b["h"] for b in bars]
    lo = [b["l"] for b in bars]
    c = [b["c"] for b in bars]
    v = [b["v"] for b in bars]
    out = {name: [0] * n for name in RULES}

    r14 = rsi(c, 14)
    a14 = atr(h, lo, c, 14)
    e21, e55, e200 = ema(c, 21), ema(c, 55), ema(c, 200)
    s50, s200 = sma(c, 50), sma(c, 200)
    e9, e12, e26 = ema(c, 9), ema(c, 12), ema(c, 26)
    macd = [None if None in (x, y) else x - y for x, y in zip(e12, e26, strict=True)]
    first = next((i for i, x in enumerate(macd) if x is not None), n)
    macd_sig: list[float | None] = [None] * first + ema([x or 0.0 for x in macd[first:]], 9)
    mfi14 = mfi(h, lo, c, v, 14)
    srsi = stoch_rsi(r14, 14, 3)
    st = supertrend(h, lo, c)
    tenkan, kijun, span_b_raw = midpoint(h, lo, 9), midpoint(h, lo, 26), midpoint(h, lo, 52)
    ph, pl = fractals(h, lo, K)

    # Недельный VWAP с якорем в понедельник 00:00 UTC.
    vwap: list[float | None] = [None] * n
    week, pv, vv = None, 0.0, 0.0
    for i, b in enumerate(bars):
        wk = b["ts"].isocalendar()[:2]
        if wk != week:
            week, pv, vv = wk, 0.0, 0.0
        pv += (h[i] + lo[i] + c[i]) / 3 * v[i]
        vv += v[i]
        vwap[i] = pv / vv if vv > 0 else None

    def zone(k: int) -> int:
        """+1 — все три осциллятора в перепроданности, −1 — все в перекупленности."""
        if None in (r14[k], mfi14[k], srsi[k]):
            return 0
        if r14[k] < 30 and mfi14[k] < 20 and srsi[k] < 20:
            return 1
        if r14[k] > 70 and mfi14[k] > 80 and srsi[k] > 80:
            return -1
        return 0

    highs: list[int] = []  # признанные вершины (индексы), по порядку признания
    lows: list[int] = []
    choch_used: set[tuple[int, int]] = set()
    fib_long = fib_short = None  # [H, L, использован]
    bull_gaps: list[list[float]] = []  # [низ, верх, истекает]
    bear_gaps: list[list[float]] = []
    bo_long = bo_short = None  # [уровень, истекает]

    for i in range(n):
        # --- фракталы, признанные на этом баре ---
        j = i - K
        new_high = j >= K and ph[j]
        new_low = j >= K and pl[j]

        if new_low:
            if lows and 5 <= j - lows[-1] <= 60:
                j0 = lows[-1]
                if lo[j] < lo[j0] and None not in (r14[j], r14[j0]) and r14[j] > r14[j0]:
                    out["rsi_div"][i] = 1
            lows.append(j)
            # Шорт-сетап фибо: вершина раньше впадины, импульс вниз.
            if highs and highs[-1] < j and a14[j]:
                hh, ll = h[highs[-1]], lo[j]
                fib_short = [hh, ll, False] if hh - ll >= 3 * a14[j] else None
        if new_high:
            if highs and 5 <= j - highs[-1] <= 60:
                j0 = highs[-1]
                if h[j] > h[j0] and None not in (r14[j], r14[j0]) and r14[j] < r14[j0]:
                    out["rsi_div"][i] = -1 if out["rsi_div"][i] == 0 else 0
            highs.append(j)
            if lows and lows[-1] < j and a14[j]:
                hh, ll = h[j], lo[lows[-1]]
                fib_long = [hh, ll, False] if hh - ll >= 3 * a14[j] else None

        # --- смена характера (CHoCH): после двух снижающихся вершин закрытие выше последней ---
        side = 0
        if len(highs) >= 2 and h[highs[-1]] < h[highs[-2]]:
            lvl = h[highs[-1]]
            key = (1, highs[-1])
            if c[i] > lvl and i > 0 and c[i - 1] <= lvl and key not in choch_used:
                side += 1
                choch_used.add(key)
        if len(lows) >= 2 and lo[lows[-1]] > lo[lows[-2]]:
            lvl = lo[lows[-1]]
            key = (-1, lows[-1])
            if c[i] < lvl and i > 0 and c[i - 1] >= lvl and key not in choch_used:
                side -= 1
                choch_used.add(key)
        out["choch"][i] = side

        # --- фибо: первое касание 0.618 после импульса, пока 0.786 не пробит закрытием ---
        side = 0
        if fib_long and not new_high:
            hh, ll, used = fib_long
            rng = hh - ll
            if c[i] < ll or h[i] > hh:
                fib_long = None
            elif not used and lo[i] <= hh - 0.618 * rng and c[i] >= hh - 0.786 * rng:
                side += 1
                fib_long[2] = True
        if fib_short and not new_low:
            hh, ll, used = fib_short
            rng = hh - ll
            if c[i] > hh or lo[i] < ll:
                fib_short = None
            elif not used and h[i] >= ll + 0.618 * rng and c[i] <= ll + 0.786 * rng:
                side -= 1
                fib_short[2] = True
        out["fib_golden"][i] = side

        # --- FVG: касание имбаланса, образованного раньше ---
        side = 0
        keep = []
        for g in bull_gaps:
            bot, top, exp = g
            if i > exp or c[i] < bot:
                continue
            if lo[i] <= top:
                side += 1
                continue
            keep.append(g)
        bull_gaps = keep
        keep = []
        for g in bear_gaps:
            bot, top, exp = g
            if i > exp or c[i] > top:
                continue
            if h[i] >= bot:
                side -= 1
                continue
            keep.append(g)
        bear_gaps = keep
        out["fvg_tap"][i] = (side > 0) - (side < 0)
        if i >= 2:
            if lo[i] > h[i - 2] and (lo[i] - h[i - 2]) / c[i] >= 0.001:
                bull_gaps.append([h[i - 2], lo[i], i + 30])
            if h[i] < lo[i - 2] and (lo[i - 2] - h[i]) / c[i] >= 0.001:
                bear_gaps.append([h[i], lo[i - 2], i + 30])

        # --- пробой 20-барного экстремума и ретест уровня в течение 10 баров ---
        side = 0
        if bo_long:
            lvl, exp = bo_long
            if i > exp or c[i] < lvl:
                bo_long = None
            elif lo[i] <= lvl:
                side += 1
                bo_long = None
        if bo_short:
            lvl, exp = bo_short
            if i > exp or c[i] > lvl:
                bo_short = None
            elif h[i] >= lvl:
                side -= 1
                bo_short = None
        out["break_retest"][i] = side
        if i >= 21:
            top20, prev_top = max(h[i - 20 : i]), max(h[i - 21 : i - 1])
            bot20, prev_bot = min(lo[i - 20 : i]), min(lo[i - 21 : i - 1])
            if c[i] > top20 and c[i - 1] <= prev_top:
                bo_long = [top20, i + 10]
            if c[i] < bot20 and c[i - 1] >= prev_bot:
                bo_short = [bot20, i + 10]

        if i == 0:
            continue

        # --- откат к EMA21 в выстроенном тренде (первое касание) ---
        if None not in (e21[i], e55[i], e200[i], e21[i - 1]):
            if e21[i] > e55[i] > e200[i] and lo[i] <= e21[i] < c[i] and lo[i - 1] > e21[i - 1]:
                out["ema_pullback"][i] = 1
            elif e21[i] < e55[i] < e200[i] and h[i] >= e21[i] > c[i] and h[i - 1] < e21[i - 1]:
                out["ema_pullback"][i] = -1

        # --- свечные паттерны у 20-барного экстремума ---
        if i >= 20:
            at_low = min(lo[i - 1], lo[i]) <= min(lo[i - 19 : i + 1])
            at_high = max(h[i - 1], h[i]) >= max(h[i - 19 : i + 1])
            red_prev, green_now = c[i - 1] < o[i - 1], c[i] > o[i]
            green_prev, red_now = c[i - 1] > o[i - 1], c[i] < o[i]
            if at_low and red_prev and green_now and o[i] <= c[i - 1] and c[i] >= o[i - 1]:
                out["engulf_extreme"][i] = 1
            elif at_high and green_prev and red_now and o[i] >= c[i - 1] and c[i] <= o[i - 1]:
                out["engulf_extreme"][i] = -1
            rng = h[i] - lo[i]
            body = abs(c[i] - o[i])
            if rng > 0:
                lower, upper = min(o[i], c[i]) - lo[i], h[i] - max(o[i], c[i])
                if lo[i] <= min(lo[i - 19 : i + 1]) and lower >= 2 * body and lower >= 0.6 * rng:
                    out["pinbar_extreme"][i] = 1
                elif h[i] >= max(h[i - 19 : i + 1]) and upper >= 2 * body and upper >= 0.6 * rng:
                    out["pinbar_extreme"][i] = -1

        # --- Supertrend: смена цвета ---
        if st[i - 1] and st[i] != st[i - 1]:
            out["supertrend"][i] = st[i]

        # --- Ишимоку: закрытие выходит из облака, Tenkan по ту же сторону от Kijun ---
        if i >= 27 and None not in (tenkan[i], kijun[i], tenkan[i - 26], kijun[i - 26],
                                    span_b_raw[i - 26], tenkan[i - 27], kijun[i - 27],
                                    span_b_raw[i - 27]):
            a_now = (tenkan[i - 26] + kijun[i - 26]) / 2
            a_prev = (tenkan[i - 27] + kijun[i - 27]) / 2
            top_now, bot_now = max(a_now, span_b_raw[i - 26]), min(a_now, span_b_raw[i - 26])
            top_prev = max(a_prev, span_b_raw[i - 27])
            bot_prev = min(a_prev, span_b_raw[i - 27])
            if c[i] > top_now and c[i - 1] <= top_prev and tenkan[i] > kijun[i]:
                out["ichimoku"][i] = 1
            elif c[i] < bot_now and c[i - 1] >= bot_prev and tenkan[i] < kijun[i]:
                out["ichimoku"][i] = -1

        # --- золотой / мёртвый крест ---
        if None not in (s50[i], s200[i], s50[i - 1], s200[i - 1]):
            if s50[i] > s200[i] and s50[i - 1] <= s200[i - 1]:
                out["golden_cross"][i] = 1
            elif s50[i] < s200[i] and s50[i - 1] >= s200[i - 1]:
                out["golden_cross"][i] = -1

        # --- Trading Rush: MACD и EMA 9/21, оба только по стороне EMA200 ---
        if None not in (macd[i], macd[i - 1], macd_sig[i], macd_sig[i - 1], e200[i]):
            up = macd[i] > macd_sig[i] and macd[i - 1] <= macd_sig[i - 1]
            down = macd[i] < macd_sig[i] and macd[i - 1] >= macd_sig[i - 1]
            if up and macd[i] < 0 and c[i] > e200[i]:
                out["macd_trend"][i] = 1
            elif down and macd[i] > 0 and c[i] < e200[i]:
                out["macd_trend"][i] = -1
        if None not in (e9[i], e9[i - 1], e21[i], e21[i - 1], e200[i]):
            if e9[i] > e21[i] and e9[i - 1] <= e21[i - 1] and c[i] > e200[i]:
                out["ema_cross_trend"][i] = 1
            elif e9[i] < e21[i] and e9[i - 1] >= e21[i - 1] and c[i] < e200[i]:
                out["ema_cross_trend"][i] = -1

        # --- Crypto Crew: перепроданность сразу по трём осцилляторам (первый бар) ---
        z = zone(i)
        if z and zone(i - 1) != z:
            out["multi_oversold"][i] = z

        # --- недельный VWAP: закрытие пересекает его внутри недели ---
        same_week = bars[i]["ts"].isocalendar()[:2] == bars[i - 1]["ts"].isocalendar()[:2]
        if same_week and vwap[i] is not None and vwap[i - 1] is not None:
            if c[i] > vwap[i] and c[i - 1] <= vwap[i - 1]:
                out["vwap_week"][i] = 1
            elif c[i] < vwap[i] and c[i - 1] >= vwap[i - 1]:
                out["vwap_week"][i] = -1
    return out


# -- замер ------------------------------------------------------------------------------


def block_stats(pairs: list[tuple[int, float]]) -> tuple[float, float, int]:
    """Среднее ПО СИГНАЛАМ, 2σ по независимым блокам и число блоков.

    Среднее — по сигналам, потому что бот торгует каждый сигнал, а не «один день».
    Первая версия усредняла внутри блока и потом по блокам: день с одним сигналом весил
    как день с двадцатью, и у недельного VWAP избыток вышел +0.25 при +0.02 по сигналам —
    редкие дни перетянули вывод. Шум — кластерная ошибка отношения: суммы по блоку против
    среднего, так соседние сигналы одного блока не выдают себя за независимые.
    """
    sums: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for blk, x in pairs:
        cell = sums[blk]
        cell[0] += x
        cell[1] += 1
    nb = len(sums)
    total_n = sum(cnt for _, cnt in sums.values())
    if nb < 2 or not total_n:
        return (fmean(x for _, x in pairs) if pairs else 0.0), float("inf"), nb
    m = sum(s for s, _ in sums.values()) / total_n
    resid = sum((s - m * cnt) ** 2 for s, cnt in sums.values())
    se = sqrt(resid * nb / (nb - 1)) / total_n
    return m, 2 * se, nb


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data")
    ap.add_argument("--venue", default="binance")
    ap.add_argument("--tf", default="4h", choices=("1h", "4h", "1d"))
    ap.add_argument("--horizon", type=int, default=6, help="баров вперёд")
    ap.add_argument("--instruments", default=",".join(INSTRUMENTS))
    ap.add_argument(
        "--trend-filter", action="store_true",
        help="сигналы только по стороне EMA200 (так учат Rayner Teo и Trading Rush); "
        "корзина контроля тогда делится и по стороне EMA200",
    )
    args = ap.parse_args()

    tf_hours = {"1h": 1, "4h": 4, "1d": 24}[args.tf]
    hz = args.horizon
    hold_days = hz * tf_hours / 24
    block_days = max(1, ceil(hold_days))
    # Ширина корзины хода растёт как корень из горизонта: 2 п.п. на сутки.
    width = 2.0 * sqrt(max(hold_days, 1 / 24))

    cs = CandleStore(args.root)
    by_bucket: dict[tuple[int, int], list[float]] = defaultdict(list)
    events: dict[str, list[tuple]] = defaultdict(list)  # (блок, год, знак, корзина, исход)
    covered = []
    for inst in args.instruments.split(","):
        bars = load(cs, args.venue, inst, args.tf)
        if len(bars) < 400:
            print(f"{inst}: баров {len(bars)} — пропуск")
            continue
        covered.append(f"{inst}({len(bars)}, с {bars[0]['ts']:%Y-%m})")
        sig = rules(bars)
        e200 = ema([b["c"] for b in bars], 200)
        for i in range(max(hz, 200), len(bars) - hz - 1):
            entry, exit_ = bars[i + 1]["o"], bars[i + 1 + hz]["o"]
            ret = (exit_ / entry - 1) * 100
            moved = (bars[i]["c"] / bars[i - hz]["c"] - 1) * 100
            trend = (1 if bars[i]["c"] > e200[i] else -1) if args.trend_filter else 0
            bucket = (max(-10, min(10, int(moved // width))), trend)
            by_bucket[bucket].append(ret)
            day = bars[i]["ts"].date()
            blk = day.toordinal() // block_days
            for name in RULES:
                s = sig[name][i]
                if s and (not trend or s == trend):
                    events[name].append((blk, day.year, s, bucket, ret))

    peer = {b: fmean(v) for b, v in by_bucket.items() if v}
    print(f"Ряды: {', '.join(covered)}")
    print(f"ТФ {args.tf}, удержание {hz} баров (~{hold_days:g} сут), вход по следующему открытию"
          + ("; ФИЛЬТР EMA200" if args.trend_filter else ""))
    print(f"Блок независимости {block_days} сут; корзина хода {width:.1f} п.п.; "
          f"круг издержек {COST_PCT:.2f}%\n")
    print(f"{'правило':16}{'сигн':>6}{'L/S':>11}{'блоков':>7}{'сырой':>8}{'корзина':>9}"
          f"{'избыток':>9}{'2σ':>7}{'лонг':>8}{'шорт':>8}{'годы+':>7}  вердикт")

    for name in RULES:
        ev = events[name]
        if not ev:
            print(f"{name:16}{0:>6}   сигналов нет")
            continue
        n_long = sum(1 for e in ev if e[2] > 0)
        raw = fmean(e[2] * e[4] for e in ev)
        pmean = fmean(e[2] * peer[e[3]] for e in ev)
        exc_pairs = [(e[0], e[2] * (e[4] - peer[e[3]])) for e in ev]
        exc, two_se, nblk = block_stats(exc_pairs)
        longs = [x for (blk, x), e in zip(exc_pairs, ev, strict=True) if e[2] > 0]
        shorts = [x for (blk, x), e in zip(exc_pairs, ev, strict=True) if e[2] < 0]
        per_year: dict[int, list[tuple[int, float]]] = defaultdict(list)
        for (blk, x), e in zip(exc_pairs, ev, strict=True):
            per_year[e[1]].append((blk, x))
        yrs = [block_stats(p)[0] for p in per_year.values() if len({b for b, _ in p}) >= 5]
        yrs_pos = sum(1 for y in yrs if y > 0)
        if nblk < 20:
            verdict = "мало наблюдений"
        elif abs(exc) < two_se:
            verdict = "шум"
        elif exc < 0:
            verdict = "ОБРАТНЫЙ знак"
        elif exc < COST_PCT:
            verdict = "меньше издержек"
        elif yrs and yrs_pos / len(yrs) < 2 / 3:
            verdict = "проходит, но НЕ по годам"
        else:
            verdict = "ПЕРЕЖИВАЕТ — кандидат"
        fl = f"{fmean(longs):+.2f}" if longs else "—"
        fs = f"{fmean(shorts):+.2f}" if shorts else "—"
        print(
            f"{name:16}{len(ev):>6}{f'{n_long}/{len(ev) - n_long}':>11}{nblk:>7}{raw:>+8.2f}"
            f"{pmean:>+9.2f}{exc:>+9.3f}{two_se:>7.3f}{fl:>8}{fs:>8}"
            f"{f'{yrs_pos}/{len(yrs)}':>7}  {verdict}"
        )
    print("\nсырой — средний исход сигнала со знаком; корзина — обычный бар того же хода в ту же")
    print("сторону; избыток = сырой − корзина, шум и вердикт — по независимым блокам.")
    print("\n" + "\n".join(f"  {k:16}{v}" for k, v in LABELS.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
