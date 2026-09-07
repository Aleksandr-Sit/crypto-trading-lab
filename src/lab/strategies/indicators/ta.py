"""Стандартные индикаторы на numpy — кирпичи для портов v0 (формулы по учебнику, без авторских
доработок). NaN — пока окно не набралось."""

from __future__ import annotations

import numpy as np

NAN = float("nan")


def sma(x: np.ndarray, period: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NAN)
    if period < 1 or len(x) < period:
        return out
    csum = np.cumsum(np.insert(np.nan_to_num(x), 0, 0.0))
    cnt = np.cumsum(np.insert((~np.isnan(x)).astype(float), 0, 0.0))
    window_sum = csum[period:] - csum[:-period]
    window_cnt = cnt[period:] - cnt[:-period]
    out[period - 1 :] = np.where(window_cnt == period, window_sum / period, NAN)
    return out


def ema(x: np.ndarray, period: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NAN)
    alpha = 2.0 / (period + 1)
    prev = NAN
    for i, v in enumerate(x):
        if np.isnan(v):
            continue
        prev = v if np.isnan(prev) else prev + alpha * (v - prev)
        out[i] = prev
    return out


def _wilder(x: np.ndarray, period: int) -> np.ndarray:
    """Сглаживание Уайлдера: первое значение — простое среднее, дальше (prev*(n-1)+x)/n."""
    out = np.full(len(x), NAN)
    if len(x) < period:
        return out
    prev = float(np.mean(x[:period]))
    out[period - 1] = prev
    for i in range(period, len(x)):
        prev = (prev * (period - 1) + x[i]) / period
        out[i] = prev
    return out


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    close = np.asarray(close, dtype=float)
    out = np.full(len(close), NAN)
    if len(close) <= period:
        return out
    delta = np.diff(close)
    gain = _wilder(np.where(delta > 0, delta, 0.0), period)
    loss = _wilder(np.where(delta < 0, -delta, 0.0), period)
    for i in range(period - 1, len(delta)):
        g, lo = gain[i], loss[i]
        if np.isnan(g):
            continue
        out[i + 1] = 100.0 if lo == 0 else 100.0 - 100.0 / (1.0 + g / lo)
    return out


def mfi(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, period: int = 14
) -> np.ndarray:
    tp = (np.asarray(high, float) + np.asarray(low, float) + np.asarray(close, float)) / 3.0
    flow = tp * np.asarray(volume, float)
    out = np.full(len(tp), NAN)
    if len(tp) <= period:
        return out
    pos = np.where(tp[1:] > tp[:-1], flow[1:], 0.0)
    neg = np.where(tp[1:] < tp[:-1], flow[1:], 0.0)
    for i in range(period - 1, len(pos)):
        p, n = pos[i - period + 1 : i + 1].sum(), neg[i - period + 1 : i + 1].sum()
        out[i + 1] = 100.0 if n == 0 else 100.0 - 100.0 / (1.0 + p / n)
    return out


def stoch(x: np.ndarray, period: int) -> np.ndarray:
    """Стохастик ряда: (x − min) / (max − min) · 100 по окну."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NAN)
    for i in range(period - 1, len(x)):
        w = x[i - period + 1 : i + 1]
        if np.isnan(w).any():
            continue
        lo, hi = w.min(), w.max()
        out[i] = 50.0 if hi == lo else (x[i] - lo) / (hi - lo) * 100.0
    return out


def stoch_rsi(
    close: np.ndarray, rsi_period: int = 14, stoch_period: int = 14, k: int = 3, d: int = 3
) -> tuple[np.ndarray, np.ndarray]:
    """Stochastic RSI (TradingView): K = SMA(stoch(RSI), k), D = SMA(K, d); шкала 0–100."""
    kk = sma(stoch(rsi(close, rsi_period), stoch_period), k)
    return kk, sma(kk, d)


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high, low, close = (np.asarray(a, float) for a in (high, low, close))
    tr = high - low
    if len(close) > 1:
        prev = close[:-1]
        tr[1:] = np.maximum(tr[1:], np.maximum(np.abs(high[1:] - prev), np.abs(low[1:] - prev)))
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    return _wilder(true_range(high, low, close), period)


def supertrend(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 10, mult: float = 3.0
) -> tuple[np.ndarray, np.ndarray]:
    """Supertrend: (линия, направление +1 вверх / −1 вниз), NaN пока нет ATR."""
    high, low, close = (np.asarray(a, float) for a in (high, low, close))
    a = atr(high, low, close, period)
    hl2 = (high + low) / 2.0
    upper, lower = hl2 + mult * a, hl2 - mult * a
    line = np.full(len(close), NAN)
    direction = np.full(len(close), NAN)
    fu, fl, d = NAN, NAN, 1.0
    for i in range(len(close)):
        if np.isnan(a[i]):
            continue
        if np.isnan(fu):
            fu, fl = upper[i], lower[i]
            d = 1.0 if close[i] > hl2[i] else -1.0
        else:
            fu = upper[i] if upper[i] < fu or close[i - 1] > fu else fu
            fl = lower[i] if lower[i] > fl or close[i - 1] < fl else fl
            if d < 0 and close[i] > fu:
                d = 1.0
            elif d > 0 and close[i] < fl:
                d = -1.0
        direction[i] = d
        line[i] = fl if d > 0 else fu
    return line, direction


def cross_up(x: np.ndarray, level: float) -> np.ndarray:
    x = np.asarray(x, float)
    out = np.zeros(len(x))
    out[1:] = (x[:-1] < level) & (x[1:] >= level)
    return out


def cross_down(x: np.ndarray, level: float) -> np.ndarray:
    x = np.asarray(x, float)
    out = np.zeros(len(x))
    out[1:] = (x[:-1] > level) & (x[1:] <= level)
    return out


__all__ = [
    "atr",
    "cross_down",
    "cross_up",
    "ema",
    "mfi",
    "rsi",
    "sma",
    "stoch",
    "stoch_rsi",
    "supertrend",
    "true_range",
]
