"""`Frame` — таблица колонок numpy для индикаторов (pandas в проекте не зависимость).

Колонки `ts` (datetime, UTC), `open/high/low/close/volume` (float64) + колонки индикаторов.
`Indicator.compute(frame) -> frame` возвращает новый Frame с добавленными колонками той же длины.
Сигналы индикатора — булевы колонки `signal_<kind>` (`signal_buy`, `signal_dot_green`, …),
по ним `signal_dates(kind)` даёт даты для теста на эталонных значениях.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from lab.contracts import Candle

OHLCV = ("open", "high", "low", "close", "volume")


class Frame:
    def __init__(self, ts: Sequence[datetime], columns: dict[str, np.ndarray]) -> None:
        self.ts: list[datetime] = list(ts)
        n = len(self.ts)
        self.columns: dict[str, np.ndarray] = {}
        for name, arr in columns.items():
            a = np.asarray(arr, dtype=float)
            if a.shape != (n,):
                raise ValueError(f"колонка {name}: длина {a.shape} ≠ {n}")
            self.columns[name] = a

    # -- конструкторы -------------------------------------------------------------------

    @classmethod
    def from_candles(cls, candles: Sequence[Candle]) -> Frame:
        return cls(
            [c.ts for c in candles],
            {name: np.array([float(getattr(c, name)) for c in candles]) for name in OHLCV},
        )

    @classmethod
    def from_csv(cls, path: Path | str) -> Frame:
        """CSV с колонками ts,open,high,low,close,volume (ts — ISO-дата или дата-время)."""
        ts: list[datetime] = []
        cols: dict[str, list[float]] = {name: [] for name in OHLCV}
        with Path(path).open(encoding="utf-8") as fh:
            for row in csv.DictReader(line for line in fh if not line.startswith("#")):
                t = datetime.fromisoformat(row["ts"])
                ts.append(t if t.tzinfo else t.replace(tzinfo=UTC))
                for name in OHLCV:
                    cols[name].append(float(row.get(name) or 0))
        return cls(ts, {k: np.array(v) for k, v in cols.items()})

    # -- доступ -------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.ts)

    def __getitem__(self, name: str) -> np.ndarray:
        return self.columns[name]

    def __contains__(self, name: str) -> bool:
        return name in self.columns

    def with_columns(self, **cols: np.ndarray) -> Frame:
        merged = dict(self.columns)
        merged.update(cols)
        return Frame(self.ts, merged)

    def last(self, name: str) -> float:
        return float(self.columns[name][-1]) if len(self) else float("nan")

    def value_at(self, name: str, at: datetime) -> float:
        """Значение на последней свече с ts <= at."""
        idx = [i for i, t in enumerate(self.ts) if t <= at]
        if not idx:
            raise KeyError(f"нет свечей до {at.isoformat()}")
        return float(self.columns[name][idx[-1]])

    def signal_dates(self, kind: str) -> list[datetime]:
        col = self.columns.get(f"signal_{kind}")
        if col is None:
            return []
        return [t for t, v in zip(self.ts, col, strict=True) if v and not np.isnan(v)]

    # -- агрегация в старшие периоды -----------------------------------------------------------

    def resample(self, rule: str) -> Frame:
        """Свечи периода `1w` (ISO-неделя) или `1M` (календарный месяц) из дневных.
        Последняя группа может быть неполной — вызывающий отбрасывает её через `period_key`."""
        if not len(self):
            return Frame([], {name: np.array([]) for name in OHLCV})
        groups: list[list[int]] = []
        keys: list[tuple] = []
        for i, t in enumerate(self.ts):
            key = period_key(t, rule)
            if not keys or keys[-1] != key:
                keys.append(key)
                groups.append([])
            groups[-1].append(i)
        o, h, lo, c, v, ts = [], [], [], [], [], []
        for g in groups:
            ts.append(self.ts[g[0]])
            o.append(self.columns["open"][g[0]])
            h.append(self.columns["high"][g].max())
            lo.append(self.columns["low"][g].min())
            c.append(self.columns["close"][g[-1]])
            v.append(self.columns["volume"][g].sum())
        return Frame(
            ts,
            {
                "open": np.array(o),
                "high": np.array(h),
                "low": np.array(lo),
                "close": np.array(c),
                "volume": np.array(v),
            },
        )

    def expand_from(
        self, higher: Frame, name: str, rule: str, *, closed_only: bool = True
    ) -> np.ndarray:
        """Колонку старшего периода вернуть на дневную сетку: каждой дневной свече — значение
        последнего **закрытого** периода (без заглядывания в будущее)."""
        out = np.full(len(self), np.nan)
        hkeys = [period_key(t, rule) for t in higher.ts]
        col = higher.columns[name]
        j = -1
        for i, t in enumerate(self.ts):
            key = period_key(t, rule)
            while j + 1 < len(hkeys) and hkeys[j + 1] < key:
                j += 1
            if not closed_only and j + 1 < len(hkeys) and hkeys[j + 1] == key:
                out[i] = col[j + 1]
            elif j >= 0:
                out[i] = col[j]
        return out


def period_key(t: datetime, rule: str) -> tuple:
    if rule == "1w":
        iso = t.isocalendar()
        return (iso.year, iso.week)
    if rule == "1M":
        return (t.year, t.month)
    if rule == "1d":
        return (t.year, t.month, t.day)
    raise ValueError(f"период {rule!r}: ожидается 1d | 1w | 1M")


__all__ = ["OHLCV", "Frame", "period_key"]
