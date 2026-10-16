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

**Выход по стопу и цели (`--exit rr`, п.7-доп).** Так учат сами преподаватели: стоп за
разворотной точкой или в ATR от свечи сигнала, цель 1.5–3R. Стоп `swing` — минимум
последних 5 баров − 0.1 ATR (шорт зеркально), `atr` — минимум свечи сигнала − 1 ATR
(как у Rayner Teo). Удержание не больше `--max-hold` баров, дальше выход по закрытию.
Стоп и цель в одной свече — стоп (порядок внутри свечи неизвестен, берём худший); гэп
за стоп — выход по открытию. Контроль — обычный бар той же корзины хода с ТЕМ ЖЕ выходом,
лонги и шорты по корзинам отдельно: у стопа и цели своя асимметрия, и сравнивать сигнал
с баром, закрытым через фиксированное время, было бы нечестно. Блок независимости —
максимальное удержание. `--stop` и `--rr` принимают списки: правила считаются один раз.

    python scripts/youtube_check.py --root /app/data --tf 4h --horizon 6 \
        --exit rr --stop swing,atr --rr 1.5,2,3 --lookahead 60

**Младшие таймфреймы 1–30 минут (п.7-доп, подпункт 2).** Ряды Bybit: перпы 15m (30m
собирается из них), спот BTC/ETH 1m (5m собирается из него). Сборка — в SQL с проверкой,
что в корзине ровно нужное число баров: миллион минутных строк словарями не влезает
в память разового контейнера. По той же причине `--chunk-days` режет ряд на куски
с разогревом `WARMUP` баров: сигналы и сделки считаются только вне разогрева. Правило
`silver_bullet` (ICT) — первый за сутки FVG, образованный и тронутый целиком внутри
10:00–11:00 America/New_York; у него второй контроль — обычный бар В ТОМ ЖЕ окне
(час открытия Нью-Йорка сам по себе волатильнее суток).

    python scripts/youtube_check.py --root /app/data --venue bybit --tf 1m --days 365 \
        --chunk-days 90 --instruments BTC/USDT,ETH/USDT --horizon 8 --exit rr ...

**Проверка кандидата `multi_oversold` (п.7-доп, раздел 9).** `--mo-grid` вместо 16 правил
считает одно `multi_oversold` на сетке порогов перепроданности RSI × MFI × StochRSI
(перекупленность зеркально: 100 − порог), строки `mo_<RSI>_<MFI>_<StochRSI>`. Канон
30/20/20 — одна из клеток сетки, без ключа правило прежнее. В таблице `--exit rr` у сырого
исхода свой шум (`2σс`) и итог после круга мейкером (`−мейк`) и тейкером (`−тейк`): деньги —
это сырой исход сделки минус издержки, а не избыток над контролем.

    python scripts/youtube_check.py --root /app/data --tf 15m --horizon 8 --exit rr \
        --stop atr --rr 1.5,2 --trend-filter --mo-grid 25,30,35:15,20,25:15,20,25
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from math import ceil, sqrt
from pathlib import Path
from statistics import fmean
from zoneinfo import ZoneInfo

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
    "silver_bullet",
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
    "silver_bullet": "ICT Silver Bullet: FVG образован и тронут в 10–11 Нью-Йорка",
}

TF_HOURS = {"1m": 1 / 60, "5m": 5 / 60, "15m": 0.25, "30m": 0.5, "1h": 1, "4h": 4, "1d": 24}
# Внутридневные ряды, которых в хранилище нет: (исходный ТФ, минут в корзине).
SQL_RESAMPLE = {"5m": ("1m", 5), "30m": ("15m", 30)}
WARMUP = 600  # баров разогрева на кусок: EMA200 и SMA200 успевают сойтись
NY = ZoneInfo("America/New_York")
SB_FROM, SB_TO = 10, 11  # окно Silver Bullet, часы Нью-Йорка
MAKER_PCT = 0.04  # круг мейкером на перпах (Binance и Bybit 0.02% за сторону)
# Сетка порогов `multi_oversold` (RSI, MFI, StochRSI); пусто — 16 обычных правил.
MO_GRID: list[tuple[int, int, int]] = []


def mo_name(r: int, m: int, s: int) -> str:
    return f"mo_{r}_{m}_{s}"


def set_mo_grid(spec: str) -> None:
    """`--mo-grid 25,30,35:15,20,25:15,20,25` → только `multi_oversold` на этих порогах.

    Список правил подменяется целиком, а не дописывается: 16 правил на сотне монет 15m
    считались бы впустую, а проверка подглядывания и таблицы берут имена из `RULES`.
    """
    global RULES
    parts = spec.split(":")
    if len(parts) != 3:
        raise SystemExit(f"--mo-grid: RSI:MFI:StochRSI через двоеточие, а не {spec}")
    rs, ms, ss = ([int(x) for x in p.split(",")] for p in parts)
    MO_GRID[:] = [(r, m, s) for r in rs for m in ms for s in ss]
    RULES = tuple(mo_name(*t) for t in MO_GRID)
# ТФ, где Silver Bullet вообще имеет смысл: на 30m в часе два бара — FVG из трёх свечей
# в окне не образуется физически.
SB_TFS = ("1m", "5m", "15m")


# -- данные -----------------------------------------------------------------------------


def load(cs: CandleStore, venue: str, inst: str, tf: str,
         since: datetime | None = None, until: datetime | None = None) -> list[dict]:
    """Свечи инструмента. 4h собирается из часовых, 5m и 30m — в SQL из 1m и 15m.

    Неполная корзина выбрасывается целиком: свеча из четырёх минут вместо пяти — другая
    свеча, и FVG на ней появился бы из дыры в данных, а не из рынка.
    """
    where, params = [], []
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if until is not None:
        where.append("ts < ?")
        params.append(until)
    cond = (" where " + " and ".join(where)) if where else ""
    if tf in SQL_RESAMPLE:
        src, mins = SQL_RESAMPLE[tf]
        step = int(src[:-1])
        rows = cs.query(
            f"select time_bucket(interval '{mins} minutes', ts) ts, "
            "arg_min(open, ts)::DOUBLE o, max(high)::DOUBLE h, min(low)::DOUBLE l, "
            "arg_max(close, ts)::DOUBLE c, sum(volume)::DOUBLE v "
            f"from {{candles}}{cond} group by 1 having count(*) = {mins // step} order by 1",
            venue, inst, src, params=params,
        )
    else:
        src = "1h" if tf == "4h" else tf
        rows = cs.query(
            "select ts, open::DOUBLE o, high::DOUBLE h, low::DOUBLE l, close::DOUBLE c, "
            f"volume::DOUBLE v from {{candles}}{cond} order by ts",
            venue, inst, src, params=params,
        )
    bars = [
        {"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"] or 0.0}
        for r in rows
        if r["c"] and r["c"] > 0 and r["o"] and r["o"] > 0 and r["h"] and r["l"]
    ]
    return resample(bars, 4) if tf == "4h" else bars


def span(cs: CandleStore, venue: str, inst: str, tf: str) -> tuple[datetime, datetime] | None:
    src = SQL_RESAMPLE.get(tf, ("1h" if tf == "4h" else tf,))[0]
    r = cs.query("select min(ts) a, max(ts) b from {candles}", venue, inst, src)
    return (r[0]["a"], r[0]["b"]) if r and r[0]["a"] else None


def chunks(cs: CandleStore, args: argparse.Namespace, inst: str):
    """Ряд кусками: (бары с разогревом, первый и за-последний индексы, идущие в счёт).

    Без `--chunk-days` — один кусок, в счёт всё (как раньше). С ним каждый следующий кусок
    начинается на `WARMUP` баров раньше своей границы: индикаторы и фракталы досчитываются
    на перекрытии, а сигналы и сделки берутся только с границы — без двойного счёта.
    """
    sp = span(cs, args.venue, inst, args.tf)
    if sp is None:
        return
    first, last = sp
    start = last - timedelta(days=args.days) if args.days else first
    if not args.chunk_days:
        bars = load(cs, args.venue, inst, args.tf, since=start if args.days else None)
        yield bars, 0, len(bars)
        return
    pad = timedelta(hours=WARMUP * TF_HOURS[args.tf])
    lo_ = start
    while lo_ <= last:
        hi_ = lo_ + timedelta(days=args.chunk_days)
        # Хвост тоже с запасом: сделке у границы нужны бары вперёд для выхода.
        bars = load(cs, args.venue, inst, args.tf, since=lo_ - pad, until=hi_ + pad)
        k0 = next((i for i, b in enumerate(bars) if b["ts"] >= lo_), len(bars))
        k1 = next((i for i, b in enumerate(bars) if b["ts"] >= hi_), len(bars))
        yield bars, (0 if lo_ == start else k0), k1
        lo_ = hi_


def ny_window(bars: list[dict], tf: str) -> list[bool]:
    """Бар целиком внутри окна Silver Bullet по часам Нью-Йорка (летнее время учтено).

    Смещение берётся на 15:00 UTC даты бара — внутри окна, а не в полночь UTC: часы
    в Нью-Йорке переводятся в 02:00 местного, и полночь UTC ещё живёт по старому времени.
    """
    step = timedelta(hours=TF_HOURS[tf])
    offs: dict = {}
    out = []
    for b in bars:
        ts = b["ts"]
        d = ts.date()
        if d not in offs:
            offs[d] = ts.replace(hour=15, minute=0, second=0, microsecond=0).astimezone(
                NY).utcoffset()
        loc = ts + offs[d]
        edge = loc.replace(hour=SB_TO, minute=0, second=0, microsecond=0)
        out.append(SB_FROM <= loc.hour < SB_TO and loc + step <= edge)
    return out


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


def rules(bars: list[dict], win: list[bool] | None = None) -> dict[str, list[int]]:
    """Направление сигнала по каждому правилу на каждом баре: +1 лонг, −1 шорт, 0 нет.

    Сигнал на баре i использует данные только до закрытия i включительно. `win` — бары
    внутри окна Silver Bullet (`ny_window`); без него `silver_bullet` молчит.
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
    if MO_GRID:
        # Тот же сигнал, что у канона ниже, — первый бар зоны, — только на своих порогах.
        for r_, m_, s_ in MO_GRID:
            sig = out[mo_name(r_, m_, s_)]
            prev = 0
            for k in range(n):
                z = 0
                if None not in (r14[k], mfi14[k], srsi[k]):
                    if r14[k] < r_ and mfi14[k] < m_ and srsi[k] < s_:
                        z = 1
                    elif r14[k] > 100 - r_ and mfi14[k] > 100 - m_ and srsi[k] > 100 - s_:
                        z = -1
                if z and prev != z:
                    sig[k] = z
                prev = z
        return out
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
    sb_bull: list[tuple[float, float]] = []  # FVG окна Silver Bullet: (низ, верх)
    sb_bear: list[tuple[float, float]] = []
    sb_days: set = set()  # сутки, в которые сигнал уже был: один сетап за окно

    for i in range(n):
        # --- Silver Bullet: первое касание FVG, образованного внутри окна 10–11 Нью-Йорка ---
        if win is None or not win[i]:
            sb_bull, sb_bear = [], []
        else:
            day = bars[i]["ts"].date()
            if day not in sb_days:
                side = 0
                keep_b = []
                for bot, top in sb_bull:
                    if c[i] < bot:
                        continue
                    if lo[i] <= top:
                        side += 1
                        continue
                    keep_b.append((bot, top))
                keep_s = []
                for bot, top in sb_bear:
                    if c[i] > top:
                        continue
                    if h[i] >= bot:
                        side -= 1
                        continue
                    keep_s.append((bot, top))
                sb_bull, sb_bear = keep_b, keep_s
                if side:
                    out["silver_bullet"][i] = (side > 0) - (side < 0)
                    sb_days.add(day)
                    sb_bull, sb_bear = [], []
            # Имбаланс из трёх свечей окна; минимального размера у ICT нет — любой разрыв.
            if i >= 2 and win[i - 1] and win[i - 2] and day not in sb_days:
                if lo[i] > h[i - 2]:
                    sb_bull.append((h[i - 2], lo[i]))
                if h[i] < lo[i - 2]:
                    sb_bear.append((h[i], lo[i - 2]))

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


# -- выход по стопу и цели --------------------------------------------------------------

SWING_BARS = 5  # стоп `swing`: экстремум последних 5 баров, включая бар сигнала


def stop_level(h, lo, a, i: int, side: int, kind: str) -> float | None:
    """Уровень стопа по данным до закрытия бара сигнала i включительно."""
    if a[i] is None:
        return None
    if kind == "swing":
        if side > 0:
            return min(lo[i - SWING_BARS + 1 : i + 1]) - 0.1 * a[i]
        return max(h[i - SWING_BARS + 1 : i + 1]) + 0.1 * a[i]
    return lo[i] - a[i] if side > 0 else h[i] + a[i]


def rr_exits(o, h, lo, c, i: int, side: int, stop: float, rrs: list[float],
             max_hold: int) -> list[tuple[float, int]] | None:
    """Исход (% со знаком позиции, баров в сделке) для каждой цели из `rrs` за один проход.

    Вход — открытие бара i+1. Цены переведены в «пространство позиции» (умножены на знак):
    так лонг и шорт проходят одной веткой кода, и «против позиции» всегда значит «меньше».
    None — открытие входа уже за стопом: такой сделки нет ни у сигнала, ни у контроля.
    """
    entry = o[i + 1]
    s_entry, s_stop = side * entry, side * stop
    risk = s_entry - s_stop
    if risk <= 0:
        return None
    s_tgt = [s_entry + r * risk for r in rrs]
    px: list[float | None] = [None] * len(rrs)
    held = [max_hold] * len(rrs)
    left = len(rrs)
    last = i + max_hold
    for k in range(i + 1, last + 1):
        s_open = side * o[k]
        fav, adv = (h[k], lo[k]) if side > 0 else (-lo[k], -h[k])
        if s_open <= s_stop:  # гэп за стоп — выход по открытию, хуже стопа
            hit, targets = s_open, False
        else:
            for j, t in enumerate(s_tgt):  # гэп за цель — по открытию, лучше цели
                if px[j] is None and s_open >= t:
                    px[j], held[j], left = s_open, k - i, left - 1
            hit, targets = (s_stop, False) if adv <= s_stop else (None, True)
        if hit is not None:  # стоп и цель в одной свече — стоп
            for j in range(len(rrs)):
                if px[j] is None:
                    px[j], held[j] = hit, k - i
            left = 0
        elif targets:
            for j, t in enumerate(s_tgt):
                if px[j] is None and fav >= t:
                    px[j], held[j], left = t, k - i, left - 1
        if not left:
            break
    s_last = side * c[last]
    return [
        (((s_last if p is None else p) - s_entry) / entry * 100, hb)
        for p, hb in zip(px, held, strict=True)
    ]


def lookahead(bars: list[dict], sig: dict[str, list[int]], a14, stops: list[str],
              n_points: int, seed: int = 7, win: list[bool] | None = None) -> str:
    """Подглядывает ли правило в будущее: сигнал и стоп на баре i по полной истории против
    истории, обрезанной на i. Точки — бары, где правило сработало (у редкого правила
    случайная точка почти всегда «нет сигнала», и проверка вышла бы пустой), плюс случайные.
    """
    rng = random.Random(seed)
    start = 250
    pts: set[int] = set(rng.sample(range(start, len(bars)), n_points // 2))
    per_rule = max(2, n_points // (2 * len(RULES)))
    for name in RULES:
        fired = [i for i in range(start, len(bars)) if sig[name][i]]
        pts.update(rng.sample(fired, min(per_rule, len(fired))))
    h = [b["h"] for b in bars]
    lo = [b["l"] for b in bars]
    bad: list[str] = []
    for i in sorted(pts):
        cut = bars[: i + 1]
        st = rules(cut, None if win is None else win[: i + 1])
        hc, lc, cc = h[: i + 1], lo[: i + 1], [b["c"] for b in cut]
        ac = atr(hc, lc, cc, 14)
        for name in RULES:
            if st[name][i] != sig[name][i]:
                bad.append(f"{name}@{i}: {sig[name][i]} против {st[name][i]}")
        for kind in stops:
            for side in (1, -1):
                if stop_level(hc, lc, ac, i, side, kind) != stop_level(h, lo, a14, i, side, kind):
                    bad.append(f"стоп {kind}{side:+d}@{i}")
    head = f"{len(pts)} точек × {len(RULES)} правил + стопы"
    return f"{head}: совпало" if not bad else f"{head}: РАСХОЖДЕНИЯ {len(bad)}: {bad[:8]}"


def verdict_of(nblk: int, exc: float, two_se: float, yrs_pos: int, n_yrs: int) -> str:
    if nblk < 20:
        return "мало наблюдений"
    if abs(exc) < two_se:
        return "шум"
    if exc < 0:
        return "ОБРАТНЫЙ знак"
    if exc < COST_PCT:
        return "меньше издержек"
    if n_yrs and yrs_pos / n_yrs < 2 / 3:
        return "проходит, но НЕ по годам"
    return "ПЕРЕЖИВАЕТ — кандидат"


def main_rr(args: argparse.Namespace) -> int:
    """Режим `--exit rr`: вход по сигналу, выход по стопу, цели или через `max_hold` баров."""
    tf_hours = TF_HOURS[args.tf]
    hz, mh = args.horizon, args.max_hold
    move_days = hz * tf_hours / 24
    # Нижний предел — минута, а не час: на 1m ход за 8 минут в корзине «за час» шириной
    # 0.4 п.п. почти весь попал бы в одну корзину, и контроля по ходу не было бы вовсе.
    width = 2.0 * sqrt(max(move_days, 1 / 1440))
    block_days = max(1, ceil(mh * tf_hours / 24))
    stops = args.stop.split(",")
    if not set(stops) <= {"swing", "atr"}:
        raise SystemExit(f"--stop: swing и/или atr, а не {args.stop}")
    rrs = [float(x) for x in args.rr.split(",")]

    cs = CandleStore(args.root)
    # (стоп, цель, корзина, сторона) -> [сумма исходов, сделок, прибыльных]
    peer: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0, 0])
    # (стоп, цель, правило) -> [(блок, год, знак, корзина, исход, баров)]
    events: dict[tuple, list[tuple]] = defaultdict(list)
    covered = []
    la = ""
    # Тот же контроль, но только из баров окна Silver Bullet — главный для `silver_bullet`.
    peer_w: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0, 0])
    for inst in args.instruments.split(","):
        n_bars, since_ = 0, None
        for bars, k0, k1 in chunks(cs, args, inst):
            if len(bars) < 400:
                continue
            since_ = since_ or bars[k0]["ts"]
            n_bars += k1 - k0
            win = ny_window(bars, args.tf) if args.tf in SB_TFS else None
            sig = rules(bars, win)
            o = [b["o"] for b in bars]
            h = [b["h"] for b in bars]
            lo = [b["l"] for b in bars]
            c = [b["c"] for b in bars]
            a14 = atr(h, lo, c, 14)
            e200 = ema(c, 200)
            if args.lookahead and not la:
                la = f"{inst}: " + lookahead(bars, sig, a14, stops, args.lookahead, win=win)
            for i in range(max(hz, 200, k0), min(k1, len(bars) - max(hz, mh) - 1)):
                moved = (c[i] / c[i - hz] - 1) * 100
                trend = (1 if c[i] > e200[i] else -1) if args.trend_filter else 0
                bucket = (max(-10, min(10, int(moved // width))), trend)
                day = bars[i]["ts"].date()
                blk = day.toordinal() // block_days
                fired = [(name, sig[name][i]) for name in RULES if sig[name][i]]
                in_win = win is not None and win[i]
                for side in (1, -1):
                    mine = [name for name, s in fired if s == side and (not trend or s == trend)]
                    for kind in stops:
                        st = stop_level(h, lo, a14, i, side, kind)
                        res = None if st is None else rr_exits(o, h, lo, c, i, side, st, rrs, mh)
                        if res is None:
                            continue
                        for r, (ret, held) in zip(rrs, res, strict=True):
                            for book, use in ((peer, True), (peer_w, in_win)):
                                if use:
                                    cell = book[(kind, r, bucket, side)]
                                    cell[0] += ret
                                    cell[1] += 1
                                    cell[2] += ret > 0
                            for name in mine:
                                events[(kind, r, name)].append(
                                    (blk, day.year, side, bucket, ret, held))
        if n_bars:
            covered.append(f"{inst}({n_bars}, с {since_:%Y-%m-%d})")
        else:
            print(f"{inst}: данных нет — пропуск")

    print(f"Ряды: {', '.join(covered)}")
    print(f"ТФ {args.tf}, выход по стопу/цели, удержание не больше {mh} баров, вход по следующему "
          "открытию" + ("; ФИЛЬТР EMA200" if args.trend_filter else ""))
    print(f"Корзина хода за {hz} баров шириной {width:.2f} п.п.; блок независимости "
          f"{block_days} сут; круг издержек {COST_PCT:.2f}%")
    if la:
        print(f"Подглядывание в будущее — {la}")
    for kind in stops:
        for side, label in ((1, "лонг"), (-1, "шорт")):
            cells = [v for key, v in peer_w.items()
                     if key[0] == kind and key[1] == rrs[0] and key[3] == side]
            n = sum(v[1] for v in cells)
            if n:
                print(f"  окно 10–11 NY, стоп {kind}, цель {rrs[0]:g}R, {label}: {n} сделок, "
                      f"в среднем {sum(v[0] for v in cells) / n:+.3f}%, прибыльных "
                      f"{100 * sum(v[2] for v in cells) / n:.1f}%")

    for kind in stops:
        for r in rrs:
            print(f"\n== стоп {kind}, цель {r:g}R ==")
            for side, label in ((1, "лонг"), (-1, "шорт")):
                cells = [v for key, v in peer.items() if key[:2] == (kind, r) and key[3] == side]
                n = sum(v[1] for v in cells)
                if n:
                    print(f"  обычный бар, {label}: {n} сделок, в среднем "
                          f"{sum(v[0] for v in cells) / n:+.3f}%, прибыльных "
                          f"{100 * sum(v[2] for v in cells) / n:.1f}%")
            print(f"{'правило':16}{'сигн':>7}{'L/S':>13}{'блоков':>7}{'сырой':>8}{'2σс':>7}"
                  f"{'−мейк':>8}{'−тейк':>8}{'корзина':>9}{'избыток':>9}{'2σ':>7}{'приб%':>7}"
                  f"{'контр%':>7}{'баров':>6}{'годы+':>7}  вердикт")
            # silver_bullet — против баров своего окна; строка `sb_vs_day` — против суток.
            rows = [(nm, nm, peer_w if nm == "silver_bullet" else peer) for nm in RULES]
            if not MO_GRID:
                rows.append(("sb_vs_day", "silver_bullet", peer))
            for label_, name, book in rows:
                ev = events[(kind, r, name)]
                if not ev:
                    print(f"{label_:16}{0:>7}   сигналов нет")
                    continue
                cells = [book[(kind, r, e[3], e[2])] for e in ev]
                n_long = sum(1 for e in ev if e[2] > 0)
                raw, raw_2se, _ = block_stats([(e[0], e[4]) for e in ev])
                pmean = fmean(v[0] / v[1] for v in cells)
                exc_pairs = [(e[0], e[4] - v[0] / v[1]) for e, v in zip(ev, cells, strict=True)]
                exc, two_se, nblk = block_stats(exc_pairs)
                win = 100 * fmean(1.0 if e[4] > 0 else 0.0 for e in ev)
                cwin = 100 * fmean(v[2] / v[1] for v in cells)
                held = fmean(e[5] for e in ev)
                per_year: dict[int, list[tuple[int, float]]] = defaultdict(list)
                for (blk, x), e in zip(exc_pairs, ev, strict=True):
                    per_year[e[1]].append((blk, x))
                yrs = [block_stats(p)[0] for p in per_year.values() if len({b for b, _ in p}) >= 5]
                yrs_pos = sum(1 for y in yrs if y > 0)
                verdict = verdict_of(nblk, exc, two_se, yrs_pos, len(yrs))
                print(
                    f"{label_:16}{len(ev):>7}{f'{n_long}/{len(ev) - n_long}':>13}{nblk:>7}"
                    f"{raw:>+8.3f}{raw_2se:>7.3f}{raw - MAKER_PCT:>+8.3f}{raw - COST_PCT:>+8.3f}"
                    f"{pmean:>+9.3f}{exc:>+9.3f}{two_se:>7.3f}{win:>7.1f}{cwin:>7.1f}"
                    f"{held:>6.1f}{f'{yrs_pos}/{len(yrs)}':>7}  {verdict}"
                )
    print("\nсырой — средний исход сделки сигнала, % со знаком позиции; корзина — обычный бар")
    print("того же хода, та же сторона, тот же выход; избыток = сырой − корзина, шум и вердикт —")
    print("по независимым блокам. приб% / контр% — доля прибыльных у сигнала и у контроля;")
    print("баров — среднее удержание сигнала. Безубыточная доля без издержек: 1/(1+R).")
    print(f"2σс — шум сырого исхода по тем же блокам; −мейк / −тейк — сырой минус круг "
          f"{MAKER_PCT:.2f}% / {COST_PCT:.2f}%.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data")
    ap.add_argument("--venue", default="binance")
    ap.add_argument("--tf", default="4h", choices=tuple(TF_HOURS))
    ap.add_argument("--horizon", type=int, default=6, help="баров вперёд")
    ap.add_argument("--days", type=int, default=0,
                    help="только последние N суток ряда (1m — год: миллион строк)")
    ap.add_argument("--chunk-days", type=int, default=0,
                    help="считать кусками по N суток с разогревом — ради памяти контейнера")
    ap.add_argument("--instruments", default=",".join(INSTRUMENTS))
    ap.add_argument(
        "--trend-filter", action="store_true",
        help="сигналы только по стороне EMA200 (так учат Rayner Teo и Trading Rush); "
        "корзина контроля тогда делится и по стороне EMA200",
    )
    ap.add_argument("--exit", default="hold", choices=("hold", "rr"),
                    help="hold — выход через --horizon баров; rr — по стопу и цели")
    ap.add_argument("--stop", default="swing", help="rr: swing и/или atr через запятую")
    ap.add_argument("--rr", default="2", help="rr: цели в R через запятую, например 1.5,2,3")
    ap.add_argument("--max-hold", type=int, default=50, help="rr: предел удержания, баров")
    ap.add_argument("--lookahead", type=int, default=0,
                    help="rr: проверить подглядывание в N точках первого инструмента")
    ap.add_argument("--mo-grid", default="",
                    help="только multi_oversold на сетке порогов RSI:MFI:StochRSI, "
                    "например 25,30,35:15,20,25:15,20,25 (канон 30:20:20)")
    args = ap.parse_args()
    if args.mo_grid:
        set_mo_grid(args.mo_grid)
    if args.exit == "rr":
        return main_rr(args)

    tf_hours = TF_HOURS[args.tf]
    hz = args.horizon
    hold_days = hz * tf_hours / 24
    block_days = max(1, ceil(hold_days))
    # Ширина корзины хода растёт как корень из горизонта: 2 п.п. на сутки (предел — минута).
    width = 2.0 * sqrt(max(hold_days, 1 / 1440))

    cs = CandleStore(args.root)
    # Суммы, а не списки исходов: на 1m за год списки — миллион чисел на корзину.
    by_bucket: dict[tuple[int, int], list[float]] = defaultdict(lambda: [0.0, 0])
    by_bucket_w: dict[tuple[int, int], list[float]] = defaultdict(lambda: [0.0, 0])
    events: dict[str, list[tuple]] = defaultdict(list)  # (блок, год, знак, корзина, исход)
    covered = []
    for inst in args.instruments.split(","):
        n_bars, since_ = 0, None
        for bars, k0, k1 in chunks(cs, args, inst):
            if len(bars) < 400:
                continue
            since_ = since_ or bars[k0]["ts"]
            n_bars += k1 - k0
            win = ny_window(bars, args.tf) if args.tf in SB_TFS else None
            sig = rules(bars, win)
            e200 = ema([b["c"] for b in bars], 200)
            for i in range(max(hz, 200, k0), min(k1, len(bars) - hz - 1)):
                entry, exit_ = bars[i + 1]["o"], bars[i + 1 + hz]["o"]
                ret = (exit_ / entry - 1) * 100
                moved = (bars[i]["c"] / bars[i - hz]["c"] - 1) * 100
                trend = (1 if bars[i]["c"] > e200[i] else -1) if args.trend_filter else 0
                bucket = (max(-10, min(10, int(moved // width))), trend)
                for book, use in ((by_bucket, True), (by_bucket_w, win is not None and win[i])):
                    if use:
                        book[bucket][0] += ret
                        book[bucket][1] += 1
                day = bars[i]["ts"].date()
                blk = day.toordinal() // block_days
                for name in RULES:
                    s = sig[name][i]
                    if s and (not trend or s == trend):
                        events[name].append((blk, day.year, s, bucket, ret))
        if n_bars:
            covered.append(f"{inst}({n_bars}, с {since_:%Y-%m-%d})")
        else:
            print(f"{inst}: данных нет — пропуск")

    peer = {b: s / n for b, (s, n) in by_bucket.items() if n}
    peer_w = {b: s / n for b, (s, n) in by_bucket_w.items() if n}
    print(f"Ряды: {', '.join(covered)}")
    print(f"ТФ {args.tf}, удержание {hz} баров (~{hold_days:g} сут), вход по следующему открытию"
          + ("; ФИЛЬТР EMA200" if args.trend_filter else ""))
    print(f"Блок независимости {block_days} сут; корзина хода {width:.2f} п.п.; "
          f"круг издержек {COST_PCT:.2f}%\n")
    print(f"{'правило':16}{'сигн':>7}{'L/S':>13}{'блоков':>7}{'сырой':>8}{'корзина':>9}"
          f"{'избыток':>9}{'2σ':>7}{'лонг':>8}{'шорт':>8}{'годы+':>7}  вердикт")

    # silver_bullet — против баров своего окна; строка `sb_vs_day` — против суток.
    rows = [(nm, nm, peer_w if nm == "silver_bullet" else peer) for nm in RULES]
    rows.append(("sb_vs_day", "silver_bullet", peer))
    for label_, name, book in rows:
        ev = events[name]
        if not ev:
            print(f"{label_:16}{0:>7}   сигналов нет")
            continue
        n_long = sum(1 for e in ev if e[2] > 0)
        raw = fmean(e[2] * e[4] for e in ev)
        pmean = fmean(e[2] * book[e[3]] for e in ev)
        exc_pairs = [(e[0], e[2] * (e[4] - book[e[3]])) for e in ev]
        exc, two_se, nblk = block_stats(exc_pairs)
        longs = [x for (blk, x), e in zip(exc_pairs, ev, strict=True) if e[2] > 0]
        shorts = [x for (blk, x), e in zip(exc_pairs, ev, strict=True) if e[2] < 0]
        per_year: dict[int, list[tuple[int, float]]] = defaultdict(list)
        for (blk, x), e in zip(exc_pairs, ev, strict=True):
            per_year[e[1]].append((blk, x))
        yrs = [block_stats(p)[0] for p in per_year.values() if len({b for b, _ in p}) >= 5]
        yrs_pos = sum(1 for y in yrs if y > 0)
        verdict = verdict_of(nblk, exc, two_se, yrs_pos, len(yrs))
        fl = f"{fmean(longs):+.2f}" if longs else "—"
        fs = f"{fmean(shorts):+.2f}" if shorts else "—"
        print(
            f"{label_:16}{len(ev):>7}{f'{n_long}/{len(ev) - n_long}':>13}{nblk:>7}{raw:>+8.3f}"
            f"{pmean:>+9.3f}{exc:>+9.3f}{two_se:>7.3f}{fl:>8}{fs:>8}"
            f"{f'{yrs_pos}/{len(yrs)}':>7}  {verdict}"
        )
    print("\nсырой — средний исход сигнала со знаком; корзина — обычный бар того же хода в ту же")
    print("сторону; избыток = сырой − корзина, шум и вердикт — по независимым блокам.")
    print("\n" + "\n".join(f"  {k:16}{v}" for k, v in LABELS.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
