"""Robinhood Crypto API: подпись Ed25519, фид, исполнитель, режим сигналов (Истории 85, 85a, 86).

Ответы фейкового транспорта — по документации (`research-sources.md` §8): котировка приходит
в `results[0]` c ценами, включающими спред; заказ — объект с `id` и `state`.
Подпись проверяется независимо: публичным ключом той же пары по строке
`api_key + timestamp + path + method + body` из документации.
"""

import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab.contracts import Executor, OrderIntent, OrderState, Signal
from lab.executors import registry
from lab.executors.access import branch_mode, status_lines
from lab.executors.robinhood import (
    RobinhoodError,
    RobinhoodExecutor,
    SignalOnly,
    check_trading_access,
    stock_signal_card,
)
from lab.feeds.robinhood import Ed25519Signer, FakeRhTransport, RobinhoodFeed

BEST_BID_ASK = {
    "results": [
        {
            "symbol": "BTC-USD",
            "price": "60000.00",
            "bid_inclusive_of_sell_spread": "59940.00",
            "sell_spread": "0.001",
            "ask_inclusive_of_buy_spread": "60060.00",
            "buy_spread": "0.001",
            "timestamp": "2026-03-01T12:00:00Z",
        }
    ]
}
ACCOUNT = {
    "account_number": "1234",
    "status": "active",
    "buying_power": "5000.00",
    "buying_power_currency": "USD",
}
HOLDINGS = {
    "results": [
        {
            "account_number": "1234",
            "asset_code": "BTC",
            "total_quantity": "0.5",
            "quantity_available_for_trading": "0.5",
        }
    ]
}
ORDER = {
    "id": "ord-1",
    "client_order_id": "rh-1",
    "state": "filled",
    "side": "buy",
    "symbol": "BTC-USD",
    "filled_asset_quantity": "0.01",
    "average_price": "60060.00",
    "created_at": "2026-03-01T12:00:01Z",
}

SEED = bytes(range(32))
PRIVATE_KEY_B64 = base64.b64encode(SEED).decode()


def _transport(*, blocked: bool = False) -> FakeRhTransport:
    t = FakeRhTransport()
    if blocked:
        t.fail("marketdata/best_bid_ask", status=403, detail="Not available in your region")
        t.fail("trading/accounts", status=403, detail="Not available in your region")
        return t
    t.route("GET", "marketdata/best_bid_ask", BEST_BID_ASK)
    t.route("GET", "trading/accounts", ACCOUNT)
    t.route("GET", "trading/holdings", HOLDINGS)
    t.route("POST", "trading/orders", ORDER)
    return t


def make_feed(**kw) -> RobinhoodFeed:
    transport = kw.pop("transport", None) or _transport()
    return RobinhoodFeed(transport, api_key="key-1", private_key=PRIVATE_KEY_B64, **kw)


def _intent(instrument="BTC-USD", price=Decimal("60060")) -> OrderIntent:
    return OrderIntent(
        strategy_id="rh-preset-sma-v1",
        venue="robinhood",
        instrument=instrument,
        side="buy",
        qty=Decimal("0.01"),
        price=price,
        order_type="limit",
        mode="paper",
        signal_id="sig-rh-1",
        client_order_id="rh-1",
    )


def test_signature_is_verifiable_by_public_key():
    signer = Ed25519Signer("key-1", PRIVATE_KEY_B64)
    headers = signer.headers("GET", "/api/v1/crypto/trading/accounts/", ts=1772000000)
    message = f"key-1{1772000000}/api/v1/crypto/trading/accounts/GET".encode()
    public = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
    public.verify(base64.b64decode(headers["x-signature"]), message)  # бросит при несовпадении
    assert headers["x-api-key"] == "key-1"
    assert headers["x-timestamp"] == "1772000000"


def test_signature_covers_body():
    signer = Ed25519Signer("key-1", PRIVATE_KEY_B64)
    a = signer.headers(
        "POST", "/api/v1/crypto/trading/orders/", body='{"side":"buy"}', ts=1772000000
    )
    b = signer.headers(
        "POST", "/api/v1/crypto/trading/orders/", body='{"side":"sell"}', ts=1772000000
    )
    assert a["x-signature"] != b["x-signature"]


def test_feed_book_from_best_bid_ask():
    feed = make_feed()
    book = feed.book("BTC-USD")
    assert book.bids[0].price == Decimal("59940.00")
    assert book.asks[0].price == Decimal("60060.00")
    assert feed.calls_signed()


def test_feed_health_down_when_account_unavailable():
    feed = make_feed(transport=_transport(blocked=True))
    health = feed.health()
    assert health.status == "down"
    assert "403" in health.detail or "регион" in health.detail.lower()


def test_executor_registered_and_paper_fills():
    assert "robinhood" in registry.all()
    assert isinstance(registry.get("robinhood")(), Executor)
    ex = RobinhoodExecutor(make_feed(), mode="paper")
    order = ex.place(_intent(), mode="paper")
    assert order.state == OrderState.FILLED
    fills = ex.fills(since=datetime.now(UTC) - timedelta(minutes=1))
    assert fills[0].price == Decimal("60060.00")
    assert ex.positions()[0].instrument == "BTC-USD"
    assert any(b.asset == "USD" for b in ex.balance())


def test_live_without_key_is_refused():
    ex = RobinhoodExecutor(RobinhoodFeed(_transport()), mode="live")
    assert ex.rights().trade is False
    with pytest.raises(RobinhoodError):
        ex.place(_intent().model_copy(update={"mode": "live"}), mode="live")


def test_stocks_are_signal_only():
    ex = RobinhoodExecutor(make_feed(), mode="paper")
    with pytest.raises(SignalOnly):
        ex.place(_intent(instrument="AAPL", price=Decimal("190")), mode="paper")


def test_stock_signal_card_is_ready_for_bot():
    signal = Signal(
        strategy_id="rh-preset-sma-cross-v1",
        decided_at=datetime(2026, 3, 1, tzinfo=UTC),
        instrument="AAPL",
        side="buy",
        size=Decimal("10"),
        price_ref=Decimal("190.5"),
        inputs_hash="abc",
        ttl_s=86400,
        meta={"asset_class": "stock"},
    )
    kind, payload = stock_signal_card(signal, signal_id="sig-42")
    assert kind == "signal"
    assert payload["signal_id"] == "sig-42"
    assert payload["instrument"] == "AAPL"
    assert payload["venue"] == "stocks"
    assert payload["rung"] == "signal"
    assert payload["price_ref"] == Decimal("190.5")


def test_unavailable_account_puts_rh_branch_into_signals_mode(session):
    ex = RobinhoodExecutor(
        RobinhoodFeed(_transport(blocked=True), api_key="k", private_key=PRIVATE_KEY_B64),
        mode="live",
    )
    mode = check_trading_access(ex, session=session)
    assert mode.branch == "rh"
    assert mode.read_only is True
    assert "сигнал" in mode.reason.lower()
    assert branch_mode(session, "rh").read_only is True
    assert any("rh" in line for line in status_lines(session))
