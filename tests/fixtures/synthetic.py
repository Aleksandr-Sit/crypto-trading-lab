"""Синтетические свечи с известными свойствами — для тестов симулятора и порога.

kind:
  trend — геометрический дрейф drift_pct за бар (+ шум noise_pct);
  flat  — среднее 100, шум noise_pct вокруг него (без накопления);
  noise — случайное блуждание с шагом noise_pct, без дрейфа.
Один seed — один и тот же ряд. Деньги — Decimal, чисел с плавающей точкой в выходе нет.
"""

import random
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from lab.contracts import Candle

Kind = Literal["trend", "flat", "noise"]

_TF = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}
_Q = Decimal("0.00000001")


def _d(value: float | Decimal | int) -> Decimal:
    return Decimal(str(value)).quantize(_Q, rounding=ROUND_HALF_UP)


def synthetic_candles(
    n: int,
    kind: Kind = "noise",
    *,
    start_price: Decimal = Decimal(100),
    drift_pct: Decimal | int = Decimal(0),
    noise_pct: Decimal | int = Decimal(1),
    seed: int = 0,
    tf: str = "1h",
    start: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    instrument: str = "SYN/USD",
) -> list[Candle]:
    if n < 1:
        raise ValueError("n должно быть >= 1")
    step = _TF[tf]
    rng = random.Random(seed)
    drift = Decimal(str(drift_pct)) / 100
    noise = Decimal(str(noise_pct)) / 100
    candles: list[Candle] = []
    level = Decimal(start_price)  # текущая цена (trend/noise) или центр (flat)
    prev_close = Decimal(start_price)

    for i in range(n):
        eps = Decimal(str(rng.uniform(-1, 1))) * noise
        if kind == "trend":
            level = level * (1 + drift) if i > 0 else level
            close = level * (1 + eps)
        elif kind == "flat":
            close = level * (1 + eps)
        else:
            level = level * (1 + eps) if i > 0 else level
            close = level
        open_ = prev_close if i > 0 else Decimal(start_price)
        wick = abs(Decimal(str(rng.uniform(0, 1)))) * noise * max(open_, close)
        high = max(open_, close) + wick
        low = min(open_, close) - wick
        volume = Decimal(str(rng.uniform(50, 150)))
        candles.append(
            Candle(
                instrument=instrument,
                tf=tf,
                ts=start + step * i,
                open=_d(open_),
                high=_d(high),
                low=_d(max(low, Decimal("0.00000001"))),
                close=_d(close),
                volume=_d(volume),
            )
        )
        prev_close = close
    return candles
