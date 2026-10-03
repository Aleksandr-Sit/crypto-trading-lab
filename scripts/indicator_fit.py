"""Обратный вывод формулы закрытого индикатора по его значениям со скриншотов и постов.

Кандидаты (дневной и недельный Bitstamp BTC/USD):
  * «линия-множитель»: level = k · MA(n)          — MA из {SMA, EMA, RMA, WMA} по закрытиям;
  * «среднее минус ATR»: level = MA(n) − k · ATR(m) — m из {14, n};
  * «экстремум-множитель»: level = k · max(high, n) и k · min(low, n).
Для каждого кандидата k подбирается по данным (медиана для множителя, МНК для ATR),
качество — средняя абсолютная относительная ошибка по всем точкам. Хорошая формула
укладывается в погрешность чтения со скриншота (<0.5–1%); подгонка к шуму даёт ошибку
в единицы процентов.

    python scripts/indicator_fit.py --daily <1d.csv> --points <points.json> [--top 8]

points.json: {"<имя индикатора>": [["2025-10-30", 100485.1], ...], ...}
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd


def rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def mas(close: pd.Series, n: int, with_wma: bool) -> dict[str, pd.Series]:
    out = {"SMA": close.rolling(n).mean(), "EMA": close.ewm(span=n, adjust=False).mean(),
           "RMA": rma(close, n)}
    if with_wma:
        out["WMA"] = wma(close, n)
    return out


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
                   axis=1).max(axis=1)
    return rma(tr, n)


def at(series: pd.Series, dates: list[pd.Timestamp]) -> np.ndarray:
    # значение на последнем закрытом баре не позже даты
    return np.array([series.asof(d) for d in dates], dtype=float)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--daily", required=True)
    ap.add_argument("--points", required=True)
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    d1 = pd.read_csv(args.daily, index_col=0, parse_dates=True)
    w1 = d1.resample("W-MON", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    frames = {"1D": (d1, range(5, 401, 1)), "1W": (w1, range(3, 121, 1))}
    points = json.load(open(args.points, encoding="utf-8"))

    for name, pts in points.items():
        dates = [pd.Timestamp(p[0]) for p in pts]
        y = np.array([p[1] for p in pts], dtype=float)
        res = []
        for tf, (df, ns) in frames.items():
            close = df["close"]
            atr14 = atr(df, 14)
            for n in ns:
                for kind, s in mas(close, n, with_wma=n <= 120).items():
                    x = at(s, dates)
                    if np.isnan(x).any():
                        continue
                    k = float(np.median(y / x))
                    err = float(np.mean(np.abs(k * x / y - 1)))
                    res.append((err, f"{tf} {k:.4f}·{kind}({n})"))
                    for m_name, a in (("ATR14", atr14), (f"ATR{n}", atr(df, n))):
                        av = at(a, dates)
                        if np.isnan(av).any() or not av.any():
                            continue
                        kk = float(np.dot(x - y, av) / np.dot(av, av))
                        e2 = float(np.mean(np.abs((x - kk * av) / y - 1)))
                        res.append((e2, f"{tf} {kind}({n}) − {kk:.3f}·{m_name}"))
                hh = at(df["high"].rolling(n).max(), dates)
                ll = at(df["low"].rolling(n).min(), dates)
                for lab, x in (("maxHigh", hh), ("minLow", ll)):
                    if np.isnan(x).any():
                        continue
                    k = float(np.median(y / x))
                    res.append((float(np.mean(np.abs(k * x / y - 1))), f"{tf} {k:.4f}·{lab}({n})"))
        res.sort()
        print(f"\n== {name}: {len(pts)} точек, {pts[0][0]} .. {pts[-1][0]}")
        for err, lab in res[: args.top]:
            print(f"  ошибка {err:6.2%}   {lab}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
