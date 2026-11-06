"""Conservative OHLC execution: liquidity is shared and touches prove no fill."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.contracts import Candle, Signal
from lab.core.measure import PaperEngine


def bar(volume="10", low="99", high="101"):
    return Candle(instrument="SYN/USD", tf="1h", ts=datetime(2026, 1, 1, tzinfo=UTC),
                  open=Decimal(100), close=Decimal(100), low=Decimal(low), high=Decimal(high),
                  volume=Decimal(volume))


def signal(side="buy", ttl=3600, order_type="limit"):
    return Signal(strategy_id="x", decided_at=bar().ts, instrument="SYN/USD", side=side,
                  size=Decimal(2), price_ref=Decimal(100), inputs_hash="h", ttl_s=ttl,
                  meta={"order_type": order_type, "limit_price": "100"})


def engine():
    return PaperEngine(venue="bybit", instrument="SYN/USD", tf="1h")


@pytest.mark.parametrize("side,low,high", [("buy", "100", "101"), ("sell", "99", "100")])
def test_touch_does_not_fill(side, low, high):
    e = engine()
    e.submit(signal(side))
    assert e.on_bar(bar(low=low, high=high)) == []
    assert len(e.pending) == 1


@pytest.mark.parametrize("volume", ["0", "-1"])
@pytest.mark.parametrize("order_type", ["market", "limit"])
def test_no_execution_without_positive_volume(volume, order_type):
    e = engine()
    e.submit(signal(order_type=order_type))
    assert e.on_bar(bar(volume=volume)) == []
    assert e.position == 0


def test_limit_budget_shared_by_both_sides():
    e = engine()
    e.submit(signal())
    e.submit(signal("sell"))
    fills = e.on_bar(bar())
    assert sum(f.qty for f in fills) == Decimal(1)
    assert len(fills) == 1
    assert [p.remaining for p in e.pending] == [Decimal(1), Decimal(2)]


@pytest.mark.parametrize("ttl", [0, 60, 3599])
def test_partial_bar_lifetime_cannot_use_later_extreme(ttl):
    e = engine()
    e.submit(signal(ttl=ttl))
    assert e.on_bar(bar()) == []


def test_full_bar_lifetime_can_fill():
    e = engine()
    e.submit(signal())
    assert e.on_bar(bar())[0].qty == Decimal(1)


def test_zero_ttl_market_order_expires():
    e = engine()
    e.submit(signal(ttl=0, order_type="market"))
    assert e.on_bar(bar()) == []
    assert e.expired == 1


@pytest.mark.parametrize("value", ["0", "-0.1", "1.1"])
def test_invalid_participation_rejected(value):
    with pytest.raises(ValueError, match="max_participation"):
        PaperEngine(venue="bybit", instrument="SYN/USD", tf="1h",
                    max_participation=Decimal(value))


def test_unknown_order_type_rejected():
    with pytest.raises(ValueError, match="order_type"):
        engine().submit(signal(order_type="stop"))
