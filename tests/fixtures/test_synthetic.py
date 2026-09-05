"""Генератор синтетических свечей: известные свойства по виду ряда и воспроизводимость."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle
from tests.fixtures.synthetic import synthetic_candles

START = datetime(2026, 1, 1, tzinfo=UTC)


def test_trend_up_ends_near_start_times_drift():
    candles = synthetic_candles(n=200, kind="trend", drift_pct=Decimal("0.5"), noise_pct=0, seed=1)
    assert len(candles) == 200
    assert all(isinstance(c, Candle) for c in candles)
    expected = Decimal(100) * (Decimal("1.005") ** 199)
    assert abs(candles[-1].close - expected) / expected < Decimal("0.001")


def test_trend_down_when_drift_negative():
    candles = synthetic_candles(n=50, kind="trend", drift_pct=Decimal("-1"), noise_pct=0, seed=1)
    assert candles[-1].close < candles[0].close
    assert all(b.close < a.close for a, b in zip(candles[:-1], candles[1:], strict=True))


def test_flat_stays_within_noise_band():
    candles = synthetic_candles(n=500, kind="flat", noise_pct=Decimal("0.2"), seed=7)
    closes = [c.close for c in candles]
    assert min(closes) > Decimal(90) and max(closes) < Decimal(110)
    mean = sum(closes) / len(closes)
    assert abs(mean - Decimal(100)) < Decimal(2)


def test_ohlc_consistent_and_timestamps_spaced_by_tf():
    candles = synthetic_candles(n=100, kind="noise", noise_pct=Decimal(2), seed=3, tf="1h")
    for c in candles:
        assert c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high
        assert c.volume > 0
    assert candles[0].ts == START
    assert candles[1].ts - candles[0].ts == timedelta(hours=1)
    assert candles[0].tf == "1h"


def test_seed_makes_series_reproducible():
    a = synthetic_candles(n=30, kind="noise", seed=42)
    b = synthetic_candles(n=30, kind="noise", seed=42)
    c = synthetic_candles(n=30, kind="noise", seed=43)
    assert a == b
    assert a != c
