"""Живые проверки площадок — только за LAB_LIVE_TESTS=1 (правило: тесты не ходят в сеть)."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from lab.feeds.cex import VENUES, make_feed

pytestmark = pytest.mark.skipif(
    os.environ.get("LAB_LIVE_TESTS") != "1", reason="LAB_LIVE_TESTS != 1"
)


@pytest.mark.parametrize("venue", VENUES)
def test_live_health_and_candles(venue):
    feed = make_feed(venue)
    health = feed.health()
    assert health.status in ("ok", "degraded", "down") and health.detail
    if health.status == "down":
        pytest.skip(f"{venue}: {health.detail}")
    symbol = "BTC/USDC:USDC" if venue == "hyperliquid" else "BTC/USDT:USDT"
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    candles = feed.candles(symbol, "1h", now - timedelta(hours=6), now)
    assert 4 <= len(candles) <= 6 and all(c.close > 0 for c in candles)
