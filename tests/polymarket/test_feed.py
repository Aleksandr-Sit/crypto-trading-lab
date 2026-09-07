"""Фид Polymarket: Gamma (рынки), CLOB (история цен, стакан), Data API (лидерборд, позиции).

Ответы фейкового транспорта собраны по документации (`research-sources.md` §7):
Gamma отдаёт `clobTokenIds`/`outcomePrices` строками JSON, CLOB `/prices-history` —
`{"history": [{"t": unix, "p": 0.53}]}`, Data API `/positions` — список позиций кошелька.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.contracts import Feed
from lab.feeds.chains.fake import FakeHttpTransport
from lab.feeds.polymarket import DATA_FEED_ID, PolymarketError, PolymarketFeed
from lab.feeds.quota import MemoryFeedsRegistry

MARKETS = [
    {
        "id": "512",
        "question": "Will BTC close above $100k in 2026?",
        "conditionId": "0xcond1",
        "slug": "btc-100k-2026",
        "clobTokenIds": (
            '["71321045679252212594626385532706912750332728571942532289631379312455583992563", '
            '"52114319501245915516055106046884209969926127482827954674443846427813813222426"]'
        ),
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.53", "0.47"]',
        "endDate": "2026-12-31T12:00:00Z",
        "closed": False,
        "active": True,
        "volume": "1250000.5",
        "liquidity": "42000",
    }
]

HISTORY = {
    "history": [
        {"t": 1767225600, "p": 0.51},
        {"t": 1767229200, "p": 0.53},
        {"t": 1767232800, "p": 0.55},
    ]
}

BOOK = {
    "market": "0xcond1",
    "asset_id": "71321045679252212594626385532706912750332728571942532289631379312455583992563",
    "bids": [{"price": "0.52", "size": "1200"}, {"price": "0.51", "size": "3000"}],
    "asks": [{"price": "0.54", "size": "800"}, {"price": "0.55", "size": "2500"}],
}

LEADERBOARD = {
    "data": [
        {"proxyWallet": "0xAAA", "name": "whale", "amount": "125000.25", "rank": 1},
        {"proxyWallet": "0xBBB", "name": "second", "amount": "80000", "rank": 2},
    ]
}

POSITIONS = [
    {
        "proxyWallet": "0xAAA",
        "conditionId": "0xcond1",
        "asset": "71321045679252212594626385532706912750332728571942532289631379312455583992563",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "size": "1000",
        "avgPrice": "0.42",
        "curPrice": "0.53",
        "initialValue": "420",
        "currentValue": "530",
        "title": "Will BTC close above $100k in 2026?",
        "slug": "btc-100k-2026",
        "redeemable": False,
    }
]

TOKEN = MARKETS[0]["clobTokenIds"].split('"')[1]


def make_feed(quota=None, *, offline: bool = False) -> tuple[PolymarketFeed, FakeHttpTransport]:
    transport = FakeHttpTransport(offline=offline)
    transport.route("GET", "gamma-api.polymarket.com/markets", MARKETS)
    transport.route("GET", "clob.polymarket.com/prices-history", HISTORY)
    transport.route("GET", "clob.polymarket.com/book", BOOK)
    transport.route("GET", "clob.polymarket.com/ok", "OK")
    transport.route("GET", "data-api.polymarket.com/v1/leaderboard", LEADERBOARD)
    transport.route("GET", "data-api.polymarket.com/positions", POSITIONS)
    transport.route("GET", "data-api.polymarket.com/trades", [])
    return PolymarketFeed(transport, quota=quota), transport


def test_feed_implements_contract():
    feed, _ = make_feed()
    assert isinstance(feed, Feed)


def test_markets_parse_tokens_and_prices():
    feed, _ = make_feed()
    markets = feed.markets(limit=10)
    m = markets[0]
    assert m.condition_id == "0xcond1"
    assert m.question.startswith("Will BTC")
    assert m.tokens[0].token_id == TOKEN
    assert m.tokens[0].outcome == "Yes"
    assert m.tokens[0].price == Decimal("0.53")
    assert m.tokens[1].outcome == "No"
    assert m.closed is False


def test_prices_history_becomes_candles():
    feed, transport = make_feed()
    candles = feed.candles(
        TOKEN, "1h", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert [c.close for c in candles] == [Decimal("0.51"), Decimal("0.53"), Decimal("0.55")]
    assert candles[0].instrument == TOKEN
    assert candles[0].ts == datetime(2026, 1, 1, tzinfo=UTC)
    # вероятность — единственная цена точки: OHLC совпадают
    assert candles[1].open == candles[1].high == candles[1].low == Decimal("0.53")
    params = transport.calls[-1].params
    assert params["market"] == TOKEN
    assert params["fidelity"] == 60


def test_book_has_sorted_sides():
    feed, _ = make_feed()
    book = feed.book(TOKEN)
    assert book.bids[0].price == Decimal("0.52")
    assert book.asks[0].price == Decimal("0.54")
    assert book.bids[0].qty == Decimal("1200")


def test_leaderboard_returns_ranked_wallets():
    feed, _ = make_feed()
    top = feed.leaderboard(limit=2)
    assert [e.address for e in top] == ["0xAAA", "0xBBB"]
    assert top[0].pnl_usd == Decimal("125000.25")
    assert top[0].rank == 1


def test_positions_of_wallet():
    feed, _ = make_feed()
    positions = feed.positions("0xAAA")
    p = positions[0]
    assert p.wallet == "0xAAA"
    assert p.token_id == TOKEN
    assert p.outcome == "Yes"
    assert p.size == Decimal("1000")
    assert p.avg_price == Decimal("0.42")
    assert p.current_price == Decimal("0.53")


def test_data_api_endpoints_cost_more_quota_than_gamma():
    """Лимиты Data API (1000 req/10 с, /positions 150, /trades 200) — весами в feeds_registry."""
    quota = MemoryFeedsRegistry()
    feed, _ = make_feed(quota)
    feed.markets(limit=1)
    feed.positions("0xAAA")
    assert quota.used[DATA_FEED_ID] > quota.used[feed.id]


def test_health_down_when_offline():
    feed, _ = make_feed(offline=True)
    health = feed.health()
    assert health.status == "down"
    assert "polymarket" in health.detail.lower()


def test_transport_error_is_polymarket_error():
    feed, _ = make_feed(offline=True)
    with pytest.raises(PolymarketError):
        feed.markets()
