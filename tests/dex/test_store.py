"""Хранение потока (История 71): полная история — у прошедших, у остальных — агрегаты."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.executors.dex.swap import TxAttempt
from lab.feeds.dex import TokenInfo
from lab.feeds.dex.store import (
    flow_windows,
    load_token,
    save_attempts,
    save_flow,
    save_token,
    token_history,
    tracked_tokens,
    tx_attempts,
)
from lab.feeds.dex.stream import TokenStream

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def token(address: str, *, liquidity: str, ts: datetime = NOW) -> TokenInfo:
    return TokenInfo(
        address=address,
        chain="solana",
        symbol="DOG",
        source="pumpportal",
        venue="jupiter",
        created_at=ts - timedelta(minutes=10),
        price_usd=Decimal("0.01"),
        liquidity_usd=Decimal(liquidity),
        volume_usd=Decimal(liquidity),
        buys=50,
    )


def test_passing_token_keeps_snapshots_rejected_keeps_none(session):
    stream = TokenStream(now=lambda: NOW)
    good, bad = token("good", liquidity="50000"), token("bad", liquidity="10")
    for snapshot in (good, token("good", liquidity="60000")):
        save_token(session, snapshot, stream.observe(snapshot))
    save_token(session, bad, stream.observe(bad))
    session.flush()

    assert len(token_history(session, "solana", "good")) == 2
    assert token_history(session, "solana", "bad") == []
    row = load_token(session, "solana", "bad")
    assert row is not None and row.passed_filter is False
    assert row.reason == "liquidity", "по отсеянному остаётся только запись с причиной"
    assert [r.address for r in tracked_tokens(session)] == ["good"]


def test_flow_aggregates_are_stored_by_window(session):
    stream = TokenStream(now=lambda: NOW)
    for i in range(50):
        stream.observe(token(f"t{i}", liquidity="50000" if i % 10 == 0 else "10"))
    save_flow(session, stream.reset_window())
    session.flush()

    (window,) = flow_windows(session)
    assert window.seen == 50
    assert window.passed == 5
    assert window.rejected == 45
    assert window.reasons["liquidity"] == 45


def test_failed_transaction_attempts_are_stored_with_cost(session):
    attempts = [
        TxAttempt("ord-1", 1, "stuck", "tx1", "застряла", Decimal("0.01"), Decimal("0.05"), NOW),
        TxAttempt("ord-1", 2, "confirmed", "tx2", "", Decimal("0.01"), Decimal("0.10"), NOW),
    ]
    save_attempts(session, attempts, strategy_id="meme-sol-pumpfun-early-v1", venue="jupiter")
    session.flush()

    rows = tx_attempts(session, order_id="ord-1")
    assert [r.status for r in rows] == ["stuck", "confirmed"]
    assert rows[0].reason == "застряла"
    assert sum(r.gas_usd for r in rows) == Decimal("0.02")
    assert rows[1].priority_fee_usd == Decimal("0.10")
