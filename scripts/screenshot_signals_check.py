"""Проверка индикатора по датам сигналов, снятым со скриншотов автора.

Кода индикатора нет (invite-only на TradingView), есть только картинки с историей.
Скрипт отвечает на два вопроса, и оба — без формулы индикатора:

1. Что было ПОСЛЕ каждого сигнала: доходность от открытия следующего дня на 10/30/60/90
   дней и худшая просадка от входа за 90 дней.
2. Лучше ли это обычного дня с таким же падением (контроль по цене, см. CLAUDE.md,
   «Порядок проверки гипотезы», п.2): доля дней истории с тем же ходом за 30 дней,
   у которых доходность вперёд была ниже, чем у сигнала (перцентиль).

Эпизоды задаются как `ДАТА[:ДАТА_КОНЦА][@уровень]`; вход считается от первого дня
эпизода. Данные — публичный API Bitstamp (BTC/USD, дневные), кэш в --cache.

    python scripts/screenshot_signals_check.py \
        --episodes 2025-11-14:2025-11-24@9 2026-01-31:2026-02-08@6

Чего скрипт НЕ говорит: шума при 5-10 эпизодах не посчитать, это описание, а не вердикт.
Скриншот может показывать ПЕРЕРИСОВАННУЮ историю — это проверяется только постами
автора в реальном времени.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

API = "https://www.bitstamp.net/api/v2/ohlc/{pair}/?step=86400&limit=1000&start={start}"
HORIZONS = (10, 30, 60, 90)
MOVE_DAYS = 30
MOVE_BAND = 0.05  # контроль: ход за 30 дней в пределах ±5 п. от хода сигнала


def fetch(pair: str, since: str, cache: Path) -> pd.DataFrame:
    """Дневные свечи Bitstamp с `since` по сегодня; кэш обновляется раз в сутки."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"bitstamp_{pair}_1d.csv"
    if path.exists() and time.time() - path.stat().st_mtime < 86400:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    rows: list[dict] = []
    start = int(datetime.fromisoformat(since).replace(tzinfo=UTC).timestamp())
    now = time.time()
    while start < now:
        with urllib.request.urlopen(API.format(pair=pair, start=start), timeout=30) as r:
            chunk = json.load(r)["data"]["ohlc"]
        if not chunk:
            break
        rows += chunk
        start = int(chunk[-1]["timestamp"]) + 86400
        time.sleep(0.3)
    df = pd.DataFrame(rows).drop_duplicates("timestamp")
    df.index = pd.to_datetime(df.pop("timestamp").astype(int), unit="s")
    df = df[["open", "high", "low", "close"]].astype(float).sort_index()
    df.to_csv(path)
    return df


def parse_episode(s: str) -> tuple[pd.Timestamp, pd.Timestamp, str]:
    body, _, level = s.partition("@")
    a, _, b = body.partition(":")
    return pd.Timestamp(a), pd.Timestamp(b or a), level or "?"


def forward(df: pd.DataFrame, day: pd.Timestamp) -> dict:
    """Вход по открытию следующего дня после `day`."""
    i = df.index.get_indexer([day])[0]
    if i < 0 or i + 1 >= len(df):
        return {}
    entry = df["open"].iloc[i + 1]
    out = {"entry": entry, "move30": df["close"].iloc[i] / df["close"].iloc[i - MOVE_DAYS] - 1}
    for h in HORIZONS:
        j = i + 1 + h
        out[f"r{h}"] = df["open"].iloc[j] / entry - 1 if j < len(df) else None
    window = df["low"].iloc[i + 1 : i + 1 + max(HORIZONS)]
    out["dd90"] = window.min() / entry - 1
    out["dd90_day"] = window.idxmin().date()
    return out


def control_pct(
    df: pd.DataFrame, move: float, h: int, value: float, until: pd.Timestamp
) -> tuple[float, int]:
    """Перцентиль `value` среди дней ДО `until` с тем же ходом за 30 дней."""
    c = df["close"]
    move30 = c / c.shift(MOVE_DAYS) - 1
    fwd = df["open"].shift(-(1 + h)) / df["open"].shift(-1) - 1
    base = pd.DataFrame({"m": move30, "f": fwd}).dropna()
    base = base[(base.index < until) & ((base["m"] - move).abs() <= MOVE_BAND)]
    if base.empty:
        return float("nan"), 0
    return float((base["f"] < value).mean()), len(base)


def pct(x: float | None) -> str:
    return "   —  " if x is None or x != x else f"{x * 100:+6.1f}"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--episodes", nargs="+", required=True,
                    help="ДАТА[:КОНЕЦ][@уровень]")
    ap.add_argument("--pair", default="btcusd")
    ap.add_argument("--since", default="2014-01-01", help="начало истории для контроля")
    ap.add_argument("--cache", default="data/screenshot_cache")
    args = ap.parse_args()

    df = fetch(args.pair, args.since, Path(args.cache))
    last = df.index[-1].date()
    print(f"Bitstamp {args.pair} 1d: {df.index[0].date()} .. {last}, "
          f"закрытие {df['close'].iloc[-1]:,.0f}\n")

    head = ("эпизод                  ур.  вход     ход30д "
            + " ".join(f"  {h:>3}д " for h in HORIZONS))
    print(head + "  просадка90  (дно)")
    results = []
    for raw in args.episodes:
        a, b, level = parse_episode(raw)
        f = forward(df, a)
        if not f:
            print(f"{raw}: нет данных")
            continue
        results.append((raw, a, f))
        span = f"{a.date()}..{b.date()}" if b != a else f"{a.date()}"
        cols = " ".join(pct(f[f"r{h}"]) for h in HORIZONS)
        print(f"{span:<23} {level:>3} {f['entry']:>8,.0f} {pct(f['move30'])}  {cols}   "
              f"{pct(f['dd90'])}  ({f['dd90_day']})")

    print(f"\nКонтроль: дни с 2014 г. до сигнала с тем же ходом за {MOVE_DAYS} дней "
          f"(±{MOVE_BAND * 100:.0f} п.).")
    print("Перцентиль = доля таких дней, у которых вперёд вышло ХУЖЕ, чем у сигнала "
          "(50% = как обычно).\n")
    print("эпизод        " + " ".join(f"  {h:>3}д      " for h in HORIZONS))
    for _raw, a, f in results:
        cells = []
        for h in HORIZONS:
            v = f[f"r{h}"]
            if v is None:
                cells.append("     —      ")
                continue
            p, n = control_pct(df, f["move30"], h, v, a)
            cells.append(f"{p * 100:4.0f}% (n={n:<4})")
        print(f"{a.date()}    " + " ".join(cells))

    full = [f for _, _, f in results if f["r60"] is not None]
    if full:
        avg = sum(f["r60"] for f in full) / len(full)
        print(f"\nСредняя доходность за 60 дней по {len(full)} эпизодам: {pct(avg)}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
