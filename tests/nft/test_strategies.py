"""Стратегии ветки NFT: ранняя вторичка и варианты минта."""

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Event
from lab.nft import NftPosition
from lab.strategies.nft import (
    MINT_VARIANTS,
    NFT_LISTING_EVENT,
    NFT_MINT_OPEN_EVENT,
    make_mint_strategy,
    make_secondary_strategy,
    mint_strategies,
    nft_strategies,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _listing_event(**payload):
    base = {
        "collection": "hype",
        "chain": "solana",
        "market": "magiceden",
        "token_id": "7",
        "price": "9.5",
        "floor": "10",
        "volume_usd": "50000",
        "sales_per_min": "5",
        "attention": "0.8",
        "listed_at": (NOW - timedelta(minutes=2)).isoformat(),
    }
    base.update({k: str(v) for k, v in payload.items()})
    return Event(kind=NFT_LISTING_EVENT, ts=NOW, payload=base)


# -- ранняя вторичка (Истории 75, 75a) ---------------------------------------------------


def test_secondary_buys_below_floor_in_the_first_minutes():
    strategy = make_secondary_strategy("hype", market="magiceden", capital_usd=1000)
    signals = strategy.on_event(_listing_event())
    assert len(signals) == 1
    signal = signals[0]
    assert signal.side == "buy" and signal.instrument == "hype:7"
    assert signal.price_ref == Decimal("9.5")
    assert signal.meta["reason"] == "secondary_entry"
    assert signal.decided_at == NOW


def test_secondary_skips_a_listing_above_floor_or_a_quiet_collection():
    strategy = make_secondary_strategy("hype", market="magiceden")
    assert strategy.on_event(_listing_event(price="12")) == []  # +20% к флору
    assert strategy.on_event(_listing_event(sales_per_min="0.1")) == []  # никто не покупает
    assert strategy.on_event(_listing_event(volume_usd="10")) == []  # объёма нет
    assert strategy.on_event(_listing_event(attention="0.05")) == []  # интереса нет


def test_secondary_skips_a_listing_older_than_the_window():
    strategy = make_secondary_strategy("hype", market="magiceden")
    old = _listing_event(listed_at=(NOW - timedelta(hours=3)).isoformat())
    assert strategy.on_event(old) == [], "это уже не «первые минуты листинга»"


def test_decision_latency_is_recorded_and_fits_the_budget():
    strategy = make_secondary_strategy("hype", market="magiceden")
    started = time.perf_counter()
    signal = strategy.on_event(_listing_event())[0]
    elapsed_ms = (time.perf_counter() - started) * 1000
    latency = int(signal.meta["decision_latency_ms"])
    assert latency <= 2000, "решение обязано укладываться в 2 секунды (История 75a)"
    assert elapsed_ms <= 2000
    assert signal.meta["decision_budget_ms"] == "2000"


def test_secondary_sells_part_on_target_and_keeps_the_rest():
    strategy = make_secondary_strategy("hype", market="magiceden")
    strategy.note_fill("hype:7", "buy", Decimal(4), Decimal(10), NOW)
    assert strategy.on_price("hype:7", Decimal(11), NOW) == []  # цель не достигнута
    sells = strategy.on_price("hype:7", Decimal(15), NOW)  # +50%
    assert len(sells) == 1 and sells[0].side == "sell" and sells[0].size == Decimal(2)
    strategy.note_fill("hype:7", "sell", Decimal(2), Decimal(15), NOW)
    assert strategy.on_price("hype:7", Decimal(100), NOW)[0].size == Decimal(1)
    strategy.note_fill("hype:7", "sell", Decimal(1), Decimal(100), NOW)
    assert strategy.on_price("hype:7", Decimal(1000), NOW) == [], "остаток держим (G06)"


def test_illiquid_position_gets_a_flag_and_a_markdown():
    strategy = make_secondary_strategy("hype", market="magiceden")
    strategy.note_fill("hype:7", "buy", Decimal(1), Decimal(10), NOW - timedelta(days=20))
    flags = strategy.illiquid(now=NOW, last_sale_at=NOW - timedelta(days=10))
    assert flags and flags[0].suggested_price < Decimal(10)
    assert isinstance(strategy.position("hype:7"), NftPosition)


# -- минт как гипотеза (Истории 76, 77; G09, G09.1) ----------------------------------------


def test_there_are_at_least_three_mint_variants_and_each_is_its_own_strategy():
    strategies = mint_strategies()
    assert len(strategies) >= 3
    ids = [s.strategy_id for s in strategies]
    assert all(i.startswith("nft-mint-") for i in ids)
    assert len(set(ids)) == len(ids), "варианты должны быть разными строками замера"
    assert {"priority-fee", "multi-wallet", "early-send"} <= set(MINT_VARIANTS)


def test_mint_strategy_carries_its_variant_parameters_and_is_forward_only():
    strategy = make_mint_strategy("multi-wallet")
    assert strategy.strategy_id == "nft-mint-multi-wallet-v1"
    assert strategy.manifest.can_backtest is False, "минт бэктесту не поддаётся"
    assert strategy.manifest.branch.value == "nft"
    assert strategy.param("wallets") == 3
    assert strategy.manifest.stop is not None


def test_mint_strategy_emits_a_signal_on_mint_open():
    strategy = make_mint_strategy("priority-fee", capital_usd=1000)
    event = Event(
        kind=NFT_MINT_OPEN_EVENT,
        ts=NOW,
        payload={
            "collection": "hype",
            "chain": "solana",
            "market": "magiceden",
            "price": "1",
            "supply": "1000",
            "attention": "0.9",
            "starts_at": NOW.isoformat(),
        },
    )
    signals = strategy.on_event(event)
    assert len(signals) == 1
    meta = signals[0].meta
    assert meta["reason"] == "mint"
    assert meta["variant"] == "priority-fee"
    assert meta["priority_fee_usd"] == "0.5"
    assert meta["wallets"] == "1"
    assert int(meta["decision_latency_ms"]) <= 2000
    assert strategy.mint_spec(event) is not None


def test_allowlist_variant_only_mints_with_a_seat():
    strategy = make_mint_strategy("allowlist")
    event = Event(
        kind=NFT_MINT_OPEN_EVENT,
        ts=NOW,
        payload={"collection": "hype", "chain": "solana", "market": "magiceden",
                 "price": "1", "attention": "0.9"},
    )
    assert strategy.on_event(event) == [], "места нет — минта нет"
    event.payload["allowlist_seat"] = "true"
    assert len(strategy.on_event(event)) == 1


def test_nft_strategies_include_secondary_and_all_mint_variants():
    ids = [s.strategy_id for s in nft_strategies()]
    assert any(i.startswith("nft-secondary-") for i in ids)
    assert sum(1 for i in ids if i.startswith("nft-mint-")) >= 3
