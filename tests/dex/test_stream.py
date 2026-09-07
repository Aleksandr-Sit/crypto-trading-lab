"""Поток новых токенов (История 71): оцениваются все, полная история — у прошедших."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.feeds.dex import TokenInfo
from lab.feeds.dex.stream import TokenStream

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def token(i: int, *, liquidity: str = "50000", buys: int = 50) -> TokenInfo:
    return TokenInfo(
        address=f"tok{i}",
        chain="solana",
        symbol=f"T{i}",
        source="pumpportal",
        created_at=NOW - timedelta(minutes=10),
        liquidity_usd=Decimal(liquidity),
        volume_usd=Decimal(liquidity),
        buys=buys,
    )


def test_thin_token_is_evaluated_but_not_stored_in_full():
    stream = TokenStream(now=lambda: NOW)
    verdict = stream.observe(token(1, liquidity="10"))
    assert verdict.passed is False
    assert verdict.reason == "liquidity"
    assert stream.history(token(1).key) == []
    assert stream.aggregates().seen == 1


def test_passing_token_keeps_full_history():
    stream = TokenStream(now=lambda: NOW)
    stream.observe(token(2))
    stream.observe(token(2, liquidity="60000"))
    history = stream.history(token(2).key)
    assert len(history) == 2
    assert [h.liquidity_usd for h in history] == [Decimal(50000), Decimal(60000)]


def test_thousand_tokens_all_evaluated_only_passing_stored():
    stream = TokenStream(now=lambda: NOW)
    for i in range(1000):
        passing = i % 100 == 0  # 10 из 1000 проходят первичный фильтр
        stream.observe(token(i, liquidity="50000" if passing else "10", buys=50 if passing else 0))
    agg = stream.aggregates()
    assert agg.seen == 1000, "оцениваются все"
    assert agg.passed == 10
    assert agg.rejected == 990
    assert agg.by_reason["liquidity"] == 990
    assert len(stream.tracked) == 10, "полная история только у прошедших"
    assert sum(len(h) for h in stream.tracked.values()) == 10


def test_tracked_tokens_are_capped_and_oldest_is_dropped():
    stream = TokenStream(now=lambda: NOW, max_tracked=3)
    for i in range(5):
        stream.observe(token(i))
    assert len(stream.tracked) == 3
    assert token(0).key not in stream.tracked
    assert token(4).key in stream.tracked


def test_aggregates_split_by_chain_and_reason():
    stream = TokenStream(now=lambda: NOW)
    stream.observe(token(1, liquidity="10"))
    stream.observe(token(2, buys=0))
    agg = stream.aggregates()
    assert agg.by_chain["solana"] == 2
    assert set(agg.by_reason) == {"liquidity", "buys"}
