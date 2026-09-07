"""Стратегии `meme-*` (Истории 66–69): только форвард, честность до покупки, лестница продаж."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Event
from lab.feeds.dex import MIGRATION_EVENT, NEW_PAIR_EVENT, TokenInfo, token_payload
from lab.strategies.meme import (
    make_meme_early_strategy,
    make_meme_volume_strategy,
    meme_strategies,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
TOKEN = "So1111"


def token(**kw) -> TokenInfo:
    base = dict(
        address=TOKEN,
        chain="solana",
        symbol="DOG",
        source="pumpportal",
        venue="jupiter",
        created_at=NOW - timedelta(minutes=30),
        price_usd=Decimal("0.001"),
        liquidity_usd=Decimal(40000),
        volume_usd=Decimal(50000),
        buys=200,
        holders=500,
        top_holders=[Decimal(5), Decimal(4)],
        migrated=True,
    )
    base.update(kw)
    return TokenInfo(**base)


def event(kind: str = MIGRATION_EVENT, **kw) -> Event:
    return Event(kind=kind, ts=NOW, payload=token_payload(token(**kw)))


def test_meme_strategies_are_forward_only():
    for strategy in meme_strategies():
        assert strategy.manifest.can_backtest is False
        assert strategy.manifest.branch == "meme"


def test_early_strategy_buys_after_migration():
    strategy = make_meme_early_strategy(capital_usd="1000", max_trade_usd="100")
    (signal,) = strategy.on_event(event())
    assert signal.side == "buy"
    assert signal.instrument == TOKEN
    assert signal.decided_at == NOW, "решение записано моментом события — до исхода"
    assert signal.inputs_hash
    assert signal.meta["reason"] == "migration_entry"
    assert Decimal(signal.meta["size_usd"]) == Decimal(100)
    assert signal.meta["honesty"] == "passed"


def test_failed_honesty_check_blocks_purchase():
    strategy = make_meme_early_strategy()
    assert strategy.on_event(event(mint_authority="Owner1")) == []
    checklist = strategy.last_checklist(TOKEN)
    assert checklist is not None and checklist.passed is False
    assert "mint_authority" in checklist.failed_names()


def test_early_strategy_ignores_token_that_has_not_migrated():
    strategy = make_meme_early_strategy()
    assert strategy.on_event(event(migrated=False)) == []


def test_volume_strategy_requires_volume_and_honesty():
    strategy = make_meme_volume_strategy(min_volume_usd="10000", min_buys=100)
    assert strategy.on_event(event(NEW_PAIR_EVENT, volume_usd=Decimal(500), buys=5)) == []
    (signal,) = strategy.on_event(event(NEW_PAIR_EVENT))
    assert signal.meta["reason"] == "honest_volume_entry"
    assert strategy.on_event(event(NEW_PAIR_EVENT, liquidity_usd=Decimal(10))) == []


def test_ladder_sells_partially_at_targets_then_trails():
    strategy = make_meme_early_strategy(capital_usd="1000", max_trade_usd="100")
    strategy.on_event(event())
    strategy.note_fill(TOKEN, "buy", Decimal(1000), Decimal("0.1"), NOW)

    (first,) = strategy.on_price(TOKEN, Decimal("0.16"), NOW + timedelta(minutes=1))
    assert first.side == "sell"
    assert first.size == Decimal(400), "40% позиции на первой цели"
    assert first.meta["reason"] == "ladder_target_1"
    strategy.note_fill(TOKEN, "sell", first.size, Decimal("0.16"), NOW + timedelta(minutes=1))

    (second,) = strategy.on_price(TOKEN, Decimal("0.25"), NOW + timedelta(minutes=2))
    assert second.size == Decimal(300)
    assert second.meta["reason"] == "ladder_target_2"
    strategy.note_fill(TOKEN, "sell", second.size, Decimal("0.25"), NOW + timedelta(minutes=2))

    assert strategy.on_price(TOKEN, Decimal("0.30"), NOW + timedelta(minutes=3)) == []
    (trail,) = strategy.on_price(TOKEN, Decimal("0.22"), NOW + timedelta(minutes=4))
    assert trail.size == Decimal(300), "остаток по трейлингу"
    assert trail.meta["reason"] == "trailing_stop"
    assert Decimal(trail.meta["peak_price"]) == Decimal("0.30")


def test_stop_loss_closes_position():
    strategy = make_meme_early_strategy()
    strategy.on_event(event())
    strategy.note_fill(TOKEN, "buy", Decimal(1000), Decimal("0.1"), NOW)
    (signal,) = strategy.on_price(TOKEN, Decimal("0.04"), NOW + timedelta(minutes=5))
    assert signal.size == Decimal(1000)
    assert signal.meta["reason"] == "stop_loss"


def test_partial_sells_are_recorded_in_meta_for_journal():
    strategy = make_meme_early_strategy()
    strategy.on_event(event())
    strategy.note_fill(TOKEN, "buy", Decimal(1000), Decimal("0.1"), NOW)
    (signal,) = strategy.on_price(TOKEN, Decimal("0.16"), NOW + timedelta(minutes=1))
    assert signal.meta["entry_price"] == "0.1"
    assert signal.meta["gain_pct"] == "60"
    assert signal.meta["position_qty"] == "1000"
