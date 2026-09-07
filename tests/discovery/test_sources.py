"""Шов R15: каждая лента превращает сырой ответ площадки в CandidateSpec. Сеть — фейковая."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.contracts import NftMint
from lab.discovery import (
    GithubSource,
    HyperliquidLeaderSource,
    NftLaunchpadSource,
    OkxLeadSource,
    PolymarketLeaderSource,
    SeedFileSource,
    SmartMoneySource,
)
from lab.discovery.config import (
    GithubConfig,
    HyperliquidConfig,
    ProviderConfig,
    SmartMoneyConfig,
)
from lab.feeds.chains.fake import FakeHttpTransport
from lab.feeds.polymarket.feed import LeaderEntry
from lab.feeds.quota import MemoryFeedsRegistry


def test_okx_lead_source_maps_public_leaders():
    transport = FakeHttpTransport().route(
        "GET",
        "public-lead-traders",
        {
            "data": [
                {
                    "ranks": [
                        {
                            "uniqueCode": "1AB2C3",
                            "nickName": "leader",
                            "pnlRatio": "0.42",
                            "winRatio": "0.64",
                            "aum": "120000",
                            "copyTraderNum": 250,
                            "leadDays": 400,
                        }
                    ]
                }
            ]
        },
    )
    quota = MemoryFeedsRegistry()
    from lab.executors.cex.copy_exchange import OkxLeadTraders

    source = OkxLeadSource(OkxLeadTraders(transport, quota=quota))

    specs = source.fetch()

    assert [s.ref for s in specs] == ["1AB2C3"]
    assert specs[0].venue == "okx" and specs[0].branch == "copy" and specs[0].kind == "trader"
    assert specs[0].facts["win_rate_pct"] == 60  # 64% огрублены до шага 10 (R15.2)
    assert quota.used["okx"] == 1
    assert source.health().status == "ok"


def test_hyperliquid_falls_back_to_manual_list():
    class DeadFeed:
        def leaderboard(self):
            raise RuntimeError("community-эндпоинт молчит")

    source = HyperliquidLeaderSource(
        DeadFeed(), config=HyperliquidConfig(manual=["0xdead", "0xbeef"])
    )

    specs = source.fetch()

    assert [s.ref for s in specs] == ["0xdead", "0xbeef"]
    assert all(s.venue == "hyperliquid" and s.facts["manual"] for s in specs)
    assert source.health().status == "degraded"
    assert "community-эндпоинт молчит" in source.health().detail


def test_hyperliquid_reads_leaderboard_when_alive():
    class LiveFeed:
        def leaderboard(self):
            return [
                {
                    "ethAddress": "0xabc",
                    "displayName": "whale",
                    "accountValue": "125000",
                    "windowPerformances": [["month", {"pnl": "42000", "roi": "0.3"}]],
                }
            ]

    source = HyperliquidLeaderSource(LiveFeed(), config=HyperliquidConfig(manual=["0xdead"]))

    specs = source.fetch()

    assert [s.ref for s in specs] == ["0xabc"]
    assert specs[0].facts["pnl_month"] == 40000
    assert source.health().status == "ok"


def test_polymarket_leaderboard_source():
    class Feed:
        def leaderboard(self, **kw):
            self.kw = kw
            return [
                LeaderEntry(
                    address="0x1",
                    name="prophet",
                    pnl_usd=Decimal("25000"),
                    volume_usd=Decimal("450000"),
                    rank=3,
                )
            ]

    source = PolymarketLeaderSource(Feed())

    specs = source.fetch()

    assert specs[0].ref == "0x1" and specs[0].branch == "prediction"
    assert specs[0].facts["pnl_usd"] == 20000 and specs[0].facts["rank"] == 0


def test_github_source_uses_search_api():
    transport = FakeHttpTransport().route(
        "GET",
        "search/repositories",
        {
            "items": [
                {
                    "full_name": "acme/grid-bot",
                    "html_url": "https://github.com/acme/grid-bot",
                    "stargazers_count": 320,
                    "pushed_at": "2026-08-01T10:00:00Z",
                    "description": "grid strategy",
                }
            ]
        },
    )
    source = GithubSource(transport, config=GithubConfig(queries=["grid strategy"], min_stars=50))

    specs = source.fetch()

    assert [s.ref for s in specs] == ["acme/grid-bot"]
    assert specs[0].kind == "strategy" and specs[0].venue == "github"
    assert specs[0].facts == {"stars": 300, "pushed": "2026-08", "query": "grid strategy"}
    assert "stars:>=50" in transport.calls[0].params["q"]


def test_smart_money_without_key_is_degraded_but_silent():
    source = SmartMoneySource(
        FakeHttpTransport(),
        config=SmartMoneyConfig(
            providers=[ProviderConfig(id="cielo", url="https://x/feed", key_env="CIELO_API_KEY")]
        ),
        env={},
    )

    assert source.fetch() == []
    assert source.health().status == "degraded"
    assert "CIELO_API_KEY" in source.health().detail


def test_smart_money_reads_provider_under_quota():
    transport = FakeHttpTransport().route(
        "GET", "/feed", {"items": [{"wallet": "So1111", "chain": "solana", "pnl_usd": 45000}]}
    )
    quota = MemoryFeedsRegistry()
    source = SmartMoneySource(
        transport,
        config=SmartMoneyConfig(
            providers=[
                ProviderConfig(
                    id="cielo", url="https://x/feed", key_env="CIELO_API_KEY", weight=3
                )
            ]
        ),
        quota=quota,
        env={"CIELO_API_KEY": "k"},
    )

    specs = source.fetch()

    assert [s.ref for s in specs] == ["So1111"]
    assert specs[0].chain == "solana" and specs[0].kind == "wallet"
    assert quota.used["cielo"] == 3


def test_nft_launchpad_source_reads_mint_feeds():
    class Launchpad:
        id = "magiceden_launchpad"

        def mints(self):
            return [
                NftMint(
                    collection="cool-cats",
                    chain="solana",
                    market="magiceden",
                    starts_at=datetime(2026, 9, 20, 15, tzinfo=UTC),
                    price=Decimal("1.5"),
                    supply=5000,
                    creator="acme",
                    meta={"url": "https://magiceden.io/launchpad/cool-cats"},
                )
            ]

    source = NftLaunchpadSource([Launchpad()])

    specs = source.fetch()

    assert [s.ref for s in specs] == ["cool-cats"]
    assert specs[0].branch == "nft" and specs[0].venue == "magiceden"
    assert specs[0].facts["starts_at"] == "2026-09-20"


def test_seed_file_source_reads_candidates_seed():
    source = SeedFileSource()

    specs = source.fetch()

    assert len(specs) >= 40, "candidates/seed.md — 43 кандидата от таска 13"
    assert any(s.ref == "@pifagortrade" and s.priority == "user" for s in specs)
    assert all(s.source == "seed" for s in specs)


def test_seed_file_source_rejects_foreign_format(tmp_path):
    path = tmp_path / "seed.md"
    path.write_text("---\nformat: something/else\n---\n", encoding="utf-8")

    source = SeedFileSource(path)

    assert source.fetch() == []
    assert source.health().status == "degraded"


@pytest.mark.parametrize("source_id", ["okx_lead", "hyperliquid", "polymarket", "github"])
def test_sources_do_not_touch_network_at_construction(source_id):
    from lab.discovery import default_sources

    built = {s.id: s for s in default_sources()}
    assert source_id in built
