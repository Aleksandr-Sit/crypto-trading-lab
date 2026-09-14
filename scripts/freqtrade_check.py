#!/usr/bin/env python
"""Прямая проверка чужих правил входа (Freqtrade) ДО написания стратегий.

Порядок проверки гипотезы в этой лаборатории требует писать стратегию последней:
правило на вымывании плеча кодировалось, регистрировалось и мерилось — и дало
`insufficient` на восемнадцати сделках, то есть не ответило ничего, тогда как прямая
проверка заняла минуту. Здесь то же самое для шести правил из репозитория
`freqtrade/freqtrade-strategies`, отобранных по отсутствию подгонки параметров.

**Что считается.** Для каждого правила: доходность вперёд после сигнала против
доходности вперёд от ОБЫЧНОГО бара. У рынка свой дрейф, и «+0.3% за сутки» без этого
сравнения не значит ничего.

**Два порога подряд, оба обязательны** (см. CLAUDE.md):

* разница больше собственного шума (2σ), причём шум считается **по независимым
  моментам времени**: восемь монет в один час — это одно наблюдение, а не восемь.
  На часовом шаге группируем по ДАТЕ;
* разница больше круга по издержкам (≈0.10% тейкером на споте Binance).

Индикаторы считаются здесь же, в чистом Python и по Уайлдеру, — чтобы не тянуть
talib ради разовой проверки и чтобы определения были видны глазами.

    python scripts/freqtrade_check.py --root /app/data --horizon 24
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

INSTRUMENTS = (
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "LINK/USDT",
)
COST_PCT = 0.10  # круг тейкером на споте Binance


# -- индикаторы -------------------------------------------------------------------------


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    total = sum(values[:n])
    out[n - 1] = total / n
    for i in range(n, len(values)):
        total += values[i] - values[i - n]
        out[i] = total / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wilder(values: list[float | None], n: int) -> list[float | None]:
    """Сглаживание Уайлдера — то самое, что стоит внутри RSI, ADX и DI."""
    out: list[float | None] = [None] * len(values)
    clean = [v for v in values[:n] if v is not None]
    if len(clean) < n:
        return out
    prev = sum(clean) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        v = values[i] or 0.0
        prev = prev + (v - prev) / n
        out[i] = prev
    return out


def rsi(close: list[float], n: int = 14) -> list[float | None]:
    gains: list[float | None] = [None]
    losses: list[float | None] = [None]
    for a, b in zip(close, close[1:], strict=False):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    g = wilder(gains[1:], n)
    lo = wilder(losses[1:], n)
    out: list[float | None] = [None] * len(close)
    for i in range(len(g)):
        if g[i] is None or lo[i] is None:
            continue
        denom = g[i] + lo[i]
        out[i + 1] = 100.0 if denom == 0 else 100 - 100 / (1 + g[i] / lo[i]) if lo[i] else 100.0
    return out


def directional(
    high: list[float], low: list[float], close: list[float], n: int
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """+DI, −DI и ADX по Уайлдеру. Периоды у talib для DI и ADX задаются отдельно."""
    tr: list[float | None] = [None]
    plus: list[float | None] = [None]
    minus: list[float | None] = [None]
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    str_ = wilder(tr[1:], n)
    sp = wilder(plus[1:], n)
    sm = wilder(minus[1:], n)
    pdi: list[float | None] = [None] * len(close)
    mdi: list[float | None] = [None] * len(close)
    dx: list[float | None] = [None] * len(close)
    for i in range(len(str_)):
        if not str_[i] or sp[i] is None or sm[i] is None:
            continue
        p, m = 100 * sp[i] / str_[i], 100 * sm[i] / str_[i]
        pdi[i + 1], mdi[i + 1] = p, m
        dx[i + 1] = 100 * abs(p - m) / (p + m) if (p + m) else 0.0
    # ADX сглаживается по НЕПУСТОМУ хвосту DX. Если подать весь ряд, первые значения
    # пустые, `wilder` не наберёт полного окна и вернёт пустым ВЕСЬ ряд — а правила,
    # завязанные на ADX, тихо перестанут срабатывать вовсе. Так и вышло в первом прогоне:
    # три правила из шести дали ноль сигналов, и это выглядело как свойство правил.
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    adx: list[float | None] = [None] * len(close)
    if first is not None:
        tail = wilder([v for v in dx[first:]], n)
        for k, v in enumerate(tail):
            adx[first + k] = v
    return pdi, mdi, adx


def stdev_window(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1 : i + 1]
        mean = sum(window) / n
        out[i] = (sum((v - mean) ** 2 for v in window) / n) ** 0.5
    return out


# -- правила ----------------------------------------------------------------------------


def rules(bars: list[dict]) -> dict[str, list[bool]]:
    """Сигналы ВХОДА шести правил. Каждый — ровно то, что написано у авторов."""
    n = len(bars)
    close = [b["c"] for b in bars]
    high = [b["h"] for b in bars]
    low = [b["l"] for b in bars]
    hl2 = [(h + low_) / 2 for h, low_ in zip(high, low, strict=True)]
    typical = [(h + low_ + c) / 3 for h, low_, c in zip(high, low, close, strict=True)]

    _, _, adx14 = directional(high, low, close, 14)
    pdi25, mdi25, _ = directional(high, low, close, 25)
    mom14 = [None if i < 14 else close[i] - close[i - 14] for i in range(n)]
    sma3, sma6 = sma(close, 3), sma(close, 6)
    ema8, ema21 = ema(close, 8), ema(close, 21)
    ema12, ema26 = ema(close, 12), ema(close, 26)
    macd = [
        None if ema12[i] is None or ema26[i] is None else ema12[i] - ema26[i] for i in range(n)
    ]
    ao5, ao34 = sma(hl2, 5), sma(hl2, 34)
    ao = [None if ao5[i] is None or ao34[i] is None else ao5[i] - ao34[i] for i in range(n)]
    rsi14 = rsi(close, 14)
    bb_mid, bb_sd = sma(typical, 20), stdev_window(typical, 20)
    bb_low = [
        None if bb_mid[i] is None or bb_sd[i] is None else bb_mid[i] - 2 * bb_sd[i]
        for i in range(n)
    ]
    # «Старший» тренд ReinforcedAverage: SMA50 по 12-барному ресемплу часового ряда.
    step = 12
    coarse = [close[i] for i in range(0, n, step)]
    coarse_sma = sma(coarse, 50)
    sma_long: list[float | None] = [None] * n
    for i in range(n):
        idx = i // step
        sma_long[i] = coarse_sma[idx - 1] if idx >= 1 else None  # только прошлые блоки

    # TD Sequential: счёт баров, закрывшихся ниже закрытия четыре бара назад.
    seq_buy = [0] * n
    for i in range(4, n):
        seq_buy[i] = seq_buy[i - 1] + 1 if close[i] < close[i - 4] else 0

    def crossed_above(a: list, b: list, i: int) -> bool:
        return (
            i > 0
            and None not in (a[i], b[i], a[i - 1], b[i - 1])
            and a[i] > b[i]
            and a[i - 1] <= b[i - 1]
        )

    out = {k: [False] * n for k in (
        "ADXMomentum", "AdxSmas", "AwesomeMacd", "BbandRsi", "ReinforcedAverage", "TDSequential"
    )}
    for i in range(n):
        if adx14[i] is not None and mom14[i] is not None and pdi25[i] is not None:
            out["ADXMomentum"][i] = (
                adx14[i] > 25 and mom14[i] > 0 and pdi25[i] > 25 and pdi25[i] > (mdi25[i] or 0)
            )
        if adx14[i] is not None:
            out["AdxSmas"][i] = adx14[i] > 25 and crossed_above(sma3, sma6, i)
        # AwesomeMacd не смотрит на ADX вовсе — его условие не должно зависеть от того,
        # посчитался ли ADX. В первом прогоне оно стояло внутри ветки ADX и молчало.
        out["AwesomeMacd"][i] = (
            i > 0
            and macd[i] is not None
            and ao[i] is not None
            and ao[i - 1] is not None
            and macd[i] > 0
            and ao[i] > 0
            and ao[i - 1] < 0
        )
        if rsi14[i] is not None and bb_low[i] is not None:
            out["BbandRsi"][i] = rsi14[i] < 30 and close[i] < bb_low[i]
        if sma_long[i] is not None:
            out["ReinforcedAverage"][i] = crossed_above(ema8, ema21, i) and close[i] > sma_long[i]
        if i >= 2:
            out["TDSequential"][i] = seq_buy[i] > 8 and low[i] < low[i - 2]
    return out


# -- замер ------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data")
    ap.add_argument("--venue", default="binance")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--horizon", type=int, default=24, help="баров вперёд")
    args = ap.parse_args()

    cs = CandleStore(args.root)
    hit: dict[str, dict[object, list[float]]] = defaultdict(lambda: defaultdict(list))
    base: dict[object, list[float]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    covered = []
    # Контроль «покупай просадку»: бары раскладываются по корзинам недавнего хода цены,
    # и правило сравнивается с ОБЫЧНЫМ баром той же корзины. Без этого правило, которое
    # входит после падения, всегда выглядит хорошо — не потому, что оно умное, а потому,
    # что отскок после падения сильнее среднего бара. Эту проверку в лаборатории
    # не прошло «вымывание плеча», и прошёл поток тейкеров.
    by_bucket: dict[int, list[float]] = defaultdict(list)
    hit_bucket: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

    for inst in INSTRUMENTS:
        rows = cs.query(
            "select ts, open::DOUBLE o, high::DOUBLE h, low::DOUBLE l, close::DOUBLE c "
            "from {candles} order by ts",
            args.venue, inst, args.tf,
        )
        bars = [
            {"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
            for r in rows
            if r["c"] and r["c"] > 0 and r["o"] and r["o"] > 0
        ]
        if len(bars) < 500:
            continue
        covered.append(f"{inst}({len(bars)})")
        signals = rules(bars)
        horizon = args.horizon
        for i in range(horizon, len(bars) - horizon - 1):
            # Вход по СЛЕДУЮЩЕМУ открытию, а не по закрытию бара сигнала: иначе
            # получаем цену, которой в момент решения ещё не существовало.
            entry = bars[i + 1]["o"]
            exit_ = bars[i + 1 + horizon]["o"]
            if entry <= 0:
                continue
            ret = (exit_ / entry - 1) * 100
            day = bars[i]["ts"].date()
            base[day].append(ret)
            # Корзина недавнего хода: тот же горизонт НАЗАД, шаг 2 процентных пункта.
            moved = (bars[i]["c"] / bars[i - horizon]["c"] - 1) * 100
            bucket = max(-10, min(10, int(moved // 2)))
            by_bucket[bucket].append(ret)
            for name, flags in signals.items():
                if flags[i]:
                    hit[name][day].append(ret)
                    hit_bucket[name][bucket].append(ret)
                    counts[name] += 1

    print(f"Ряды: {', '.join(covered)}")
    print(f"Горизонт: {args.horizon} баров {args.tf}; вход по следующему открытию")
    print(f"Круг по издержкам: {COST_PCT:.2f}%\n")

    base_daily = [fmean(v) for v in base.values() if v]
    base_mean = fmean(base_daily) if base_daily else 0.0
    print(f"обычный бар: {base_mean:+.3f}% за горизонт, независимых дней {len(base_daily)}\n")
    print(f"{'правило':20}{'сигналов':>9}{'дней':>7}{'среднее':>10}{'против обычного':>17}"
          f"{'2σ шума':>10}  вердикт")

    for name in sorted(hit, key=lambda k: -counts[k]):
        daily = [fmean(v) for v in hit[name].values() if v]
        if len(daily) < 10:
            print(f"{name:20}{counts[name]:>9}{len(daily):>7}      наблюдений слишком мало")
            continue
        mean = fmean(daily)
        diff = mean - base_mean
        noise = 2 * stdev(daily) / (len(daily) ** 0.5) if len(daily) > 1 else 0.0
        if abs(diff) < noise:
            verdict = "нет: внутри шума"
        elif diff < COST_PCT:
            verdict = "нет: меньше издержек"
        else:
            verdict = "ЕСТЬ СИГНАЛ"
        print(
            f"{name:20}{counts[name]:>9}{len(daily):>7}{mean:>+10.3f}{diff:>+17.3f}"
            f"{noise:>10.3f}  {verdict}"
        )

    print()
    print("Контроль «покупай просадку»: сравнение с обычным баром ПРИ ТОМ ЖЕ ходе цены")
    print(f"{'правило':20}{'взвешенно':>11}{'обычный':>10}{'разница':>10}  вердикт")
    for name in sorted(hit_bucket, key=lambda k: -counts[k]):
        buckets = hit_bucket[name]
        total = sum(len(v) for v in buckets.values())
        if total < 200:
            print(f"{name:20}   наблюдений мало ({total})")
            continue
        # Средний исход правила и средний исход ОБЫЧНОГО бара, взвешенные одинаково —
        # по тому, как часто правило попадает в каждую корзину хода цены.
        rule_mean = sum(fmean(v) * len(v) for v in buckets.values() if v) / total
        peer_mean = sum(
            fmean(by_bucket[b]) * len(v) for b, v in buckets.items() if v and by_bucket[b]
        ) / total
        diff = rule_mean - peer_mean
        verdict = (
            "СВОДИТСЯ к просадке" if diff < COST_PCT else "остаётся после контроля"
        )
        print(f"{name:20}{rule_mean:>+11.3f}{peer_mean:>+10.3f}{diff:>+10.3f}  {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
