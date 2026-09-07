"""Фиды ранней стадии (История 65): PumpPortal WS с реконнектом, DexScreener, STON.fi."""

import json
from decimal import Decimal

import pytest

from lab.contracts import Feed
from lab.feeds import MemoryFeedsRegistry
from lab.feeds.chains.fake import FakeHttpTransport
from lab.feeds.dex import MIGRATION_EVENT, NEW_TOKEN_EVENT
from lab.feeds.dex.dexscreener import DexScreenerFeed
from lab.feeds.dex.pumpportal import PumpPortalFeed
from lab.feeds.dex.stonfi import StonFiFeed
from lab.feeds.dex.ws import FakeWebSocket, FakeWsFactory


async def take(stream, n: int) -> list:
    out = []
    async for event in stream:
        out.append(event)
        if len(out) == n:
            break
    return out


def new_token_msg(mint: str) -> str:
    return json.dumps(
        {
            "txType": "create",
            "mint": mint,
            "name": "Dog",
            "symbol": "DOG",
            "traderPublicKey": "creator1",
            "marketCapSol": 30,
            "vSolInBondingCurve": 31.5,
            "solAmount": 1.5,
        }
    )


def migration_msg(mint: str) -> str:
    return json.dumps({"txType": "migrate", "mint": mint, "pool": "raydium"})


@pytest.mark.asyncio
async def test_pumpportal_yields_new_tokens_and_subscribes():
    ws = FakeWebSocket([new_token_msg("mint1"), new_token_msg("mint2")])
    feed = PumpPortalFeed(FakeWsFactory([ws]), sol_price_usd=Decimal(200))
    events = await take(feed.events(NEW_TOKEN_EVENT), 2)
    assert [e.kind for e in events] == [NEW_TOKEN_EVENT, NEW_TOKEN_EVENT]
    assert events[0].payload["address"] == "mint1"
    assert json.loads(ws.sent[0])["method"] == "subscribeNewToken"


@pytest.mark.asyncio
async def test_pumpportal_reconnects_after_drop_and_resubscribes():
    first = FakeWebSocket([new_token_msg("mint1")], drop_after=1)
    second = FakeWebSocket([new_token_msg("mint2")])
    feed = PumpPortalFeed(FakeWsFactory([first, second]), reconnect_delay_s=0)
    events = await take(feed.events(NEW_TOKEN_EVENT), 2)
    assert [e.payload["address"] for e in events] == ["mint1", "mint2"]
    assert feed.reconnects == 1
    assert json.loads(second.sent[0])["method"] == "subscribeNewToken"


@pytest.mark.asyncio
async def test_pumpportal_migration_stream_marks_token_migrated():
    ws = FakeWebSocket([migration_msg("mint9")])
    feed = PumpPortalFeed(FakeWsFactory([ws]))
    (event,) = await take(feed.events(MIGRATION_EVENT), 1)
    assert event.kind == MIGRATION_EVENT
    assert event.payload["token"]["migrated"] is True
    assert json.loads(ws.sent[0])["method"] == "subscribeMigration"


def test_dexscreener_maps_pair_to_token_and_counts_quota():
    quota = MemoryFeedsRegistry()
    transport = FakeHttpTransport().route(
        "GET",
        "/latest/dex/tokens/",
        {
            "pairs": [
                {
                    "chainId": "bsc",
                    "dexId": "pancakeswap",
                    "pairAddress": "0xpair",
                    "baseToken": {"address": "0xtok", "name": "Doge", "symbol": "DOGE"},
                    "priceUsd": "0.5",
                    "liquidity": {"usd": 42000},
                    "volume": {"h24": 12000},
                    "txns": {"h24": {"buys": 90, "sells": 30}},
                    "pairCreatedAt": 1757246400000,
                    "fdv": 900000,
                }
            ]
        },
    )
    feed = DexScreenerFeed(transport, quota=quota)
    token = feed.token("bnb", "0xtok")
    assert token.symbol == "DOGE"
    assert token.liquidity_usd == Decimal(42000)
    assert token.buys == 90
    assert token.venue == "pancakeswap"
    assert token.chain == "bnb"
    assert quota.calls["dexscreener"] == 1
    assert feed.rate_limit_rpm == 300


def test_dexscreener_new_pairs_reports_profiles():
    transport = FakeHttpTransport().route(
        "GET",
        "/token-profiles/latest",
        [{"chainId": "base", "tokenAddress": "0xnew"}, {"chainId": "solana", "tokenAddress": "s1"}],
    ).route(
        "GET",
        "/latest/dex/tokens/",
        lambda url, params, json_: {
            "pairs": [
                {
                    "chainId": "base",
                    "dexId": "aerodrome",
                    "pairAddress": "0xp",
                    "baseToken": {"address": url.rsplit("/", 1)[-1], "symbol": "NEW"},
                    "priceUsd": "1",
                    "liquidity": {"usd": 9000},
                }
            ]
        },
    )
    feed = DexScreenerFeed(transport)
    tokens = feed.new_tokens(chains=("base",))
    assert [t.address for t in tokens] == ["0xnew"]
    assert tokens[0].source == "dexscreener"


def test_stonfi_lists_new_ton_tokens():
    transport = FakeHttpTransport().route(
        "GET",
        "/v1/assets",
        {
            "asset_list": [
                {
                    "contract_address": "EQtoken",
                    "symbol": "TONK",
                    "display_name": "Tonk",
                    "dex_price_usd": "0.02",
                    "dex_usd_price": "0.02",
                    "kind": "JETTON",
                    "deprecated": False,
                }
            ]
        },
    )
    feed = StonFiFeed(transport)
    tokens = feed.new_tokens()
    assert tokens[0].address == "EQtoken"
    assert tokens[0].chain == "ton"
    assert tokens[0].venue == "stonfi"


def test_dex_feeds_satisfy_feed_protocol():
    assert isinstance(DexScreenerFeed(FakeHttpTransport()), Feed)
    assert isinstance(StonFiFeed(FakeHttpTransport()), Feed)
    assert isinstance(PumpPortalFeed(FakeWsFactory([])), Feed)
