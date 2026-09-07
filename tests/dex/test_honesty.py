"""Чек-лист честности токена (История 67): провал любого пункта запрещает покупку."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.feeds.dex import TokenInfo, honesty_check

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def token(**kw) -> TokenInfo:
    base = dict(
        address="So1111",
        chain="solana",
        symbol="DOG",
        created_at=NOW - timedelta(hours=2),
        liquidity_usd=Decimal(20000),
        mint_authority=None,
        freeze_authority=None,
        top_holders=[Decimal(5), Decimal(4), Decimal(3)],
        holders=300,
    )
    base.update(kw)
    return TokenInfo(**base)


def test_clean_token_passes_every_check():
    checklist = honesty_check(token(), now=NOW)
    assert checklist.passed is True
    assert checklist.failed_names() == []
    assert {c.name for c in checklist.checks} == {
        "mint_authority",
        "freeze_authority",
        "top_holders",
        "liquidity",
        "age",
        "blocklist",
    }


def test_live_mint_authority_blocks_purchase():
    checklist = honesty_check(token(mint_authority="Owner11"), now=NOW)
    assert checklist.passed is False
    assert "mint_authority" in checklist.failed_names()


def test_live_freeze_authority_blocks_purchase():
    checklist = honesty_check(token(freeze_authority="Owner11"), now=NOW)
    assert checklist.failed_names() == ["freeze_authority"]


def test_top_holder_share_above_limit_blocks_purchase():
    checklist = honesty_check(token(top_holders=[Decimal(40), Decimal(5)]), now=NOW)
    assert "top_holders" in checklist.failed_names()


def test_thin_liquidity_blocks_purchase():
    checklist = honesty_check(token(liquidity_usd=Decimal(100)), now=NOW)
    assert "liquidity" in checklist.failed_names()


def test_too_young_token_blocks_purchase():
    checklist = honesty_check(token(created_at=NOW - timedelta(seconds=5)), now=NOW)
    assert "age" in checklist.failed_names()


def test_blocklisted_creator_blocks_purchase():
    checklist = honesty_check(token(creator="Scammer1"), blocklist=["scammer1"], now=NOW)
    assert "blocklist" in checklist.failed_names()


def test_checklist_reports_value_and_limit_of_failed_check():
    checklist = honesty_check(token(liquidity_usd=Decimal(100)), now=NOW)
    check = next(c for c in checklist.checks if c.name == "liquidity")
    assert check.value == Decimal(100)
    assert check.limit == Decimal(5000)
    assert check.detail
