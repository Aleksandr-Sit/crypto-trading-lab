"""Шов feeds.cex: контракт Feed поверх фейкового ccxt-транспорта. Сети нет."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.feeds import CountingQuota
from lab.feeds.cex import BybitFeed, FakeTransport
from lab.feeds.cex.transport import VENUE_SPECS, VENUES, credentials_from_env

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _fake(n: int = 48) -> FakeTransport:
    t = FakeTransport("bybit")
    t.seed_ohlcv("BTC/USDT:USDT", "1h", T0, n, start_price=Decimal("50000"))
    return t


def test_candles_paginate_and_count_quota():
    quota = CountingQuota()
    feed = BybitFeed(transport=_fake(48), quota=quota, page_limit=20)
    out = feed.candles("BTC/USDT:USDT", "1h", T0, T0 + timedelta(hours=48))
    assert len(out) == 48
    assert out[0].ts == T0 and out[-1].ts == T0 + timedelta(hours=47)
    assert out[0].open == Decimal("50000") and isinstance(out[0].volume, Decimal)
    assert out[0].tf == "1h" and out[0].instrument == "BTC/USDT:USDT"
    # 48 свечей по 20 на страницу = 3 запроса, каждый через квоту
    assert quota.used["bybit"] == 3


def test_health_down_on_geo_block_and_on_network_loss():
    import ccxt

    from lab.contracts import Feed

    t = _fake()
    feed = BybitFeed(transport=t)
    assert isinstance(feed, Feed)
    assert feed.health().status == "ok"
    t.fail_next = ccxt.PermissionDenied('bybit {"retCode":10024,"retMsg":"restricted location"}')
    h = feed.health()
    assert h.status == "down" and "гео" in h.detail
    t.offline = True
    h = feed.health()
    assert h.status == "down" and "связи" in h.detail


def test_funding_and_book_come_from_venue():
    t = _fake()
    t.set_funding("BTC/USDT:USDT", Decimal("0.0001"), next_at=T0 + timedelta(hours=8))
    feed = BybitFeed(transport=t)
    f = feed.funding("BTC/USDT:USDT")
    assert f.rate == Decimal("0.0001") and f.next_at == T0 + timedelta(hours=8)
    book = feed.book("BTC/USDT:USDT")
    assert book.bids[0].price < book.asks[0].price
    assert all(isinstance(level.qty, Decimal) for level in book.bids + book.asks)


def test_data_only_venue_has_no_credentials_and_does_not_crash():
    """Площадка только для истории (bitstamp) не имеет имён ключей в `.env`.

    Проверка «все ключи заполнены» на пустом наборе имён истинна (`all({}.values())`),
    поэтому раньше код шёл дальше и падал на `values[names[0]]` с IndexError — бэкфилл
    Bitstamp не запускался вовсе.
    """
    assert credentials_from_env("bitstamp", {}) == {}
    assert credentials_from_env("bitstamp", {"BYBIT_API_KEY": "x"}) == {}
    assert "bitstamp" in VENUE_SPECS
    assert "bitstamp" not in VENUES, "торговых исполнителей на bitstamp быть не должно"
