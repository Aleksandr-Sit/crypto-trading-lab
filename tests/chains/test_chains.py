"""Клиенты сетей за контрактом `Feed` (История 64, G07): включение флагом `CHAINS_ENABLED`,
порядок Solana → Ethereum/Base → BNB → TON, разбор сделок кошелька, квота на каждый вызов.

Сети в тестах не вызываются: транспорт — `FakeHttpTransport` с готовыми ответами.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.config import ConfigError
from lab.contracts import Feed
from lab.feeds import MemoryFeedsRegistry
from lab.feeds.chains import (
    ChainDisabled,
    EvmFeed,
    FakeHttpTransport,
    HyperliquidUserFeed,
    SolanaFeed,
    TonFeed,
    enabled_chains,
    load_chains,
    make_chain_feed,
)

WALLET = "So1Wa11etAddressForTests1111111111111111111"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

HELIUS_SWAPS = [
    {
        "signature": "sig-buy",
        "timestamp": 1_757_000_000,
        "type": "SWAP",
        "source": "JUPITER",
        "fee": 5000,
        "events": {
            "swap": {
                "tokenInputs": [
                    {
                        "userAccount": WALLET,
                        "mint": USDC,
                        "rawTokenAmount": {"tokenAmount": "1000000000", "decimals": 6},
                    }
                ],
                "tokenOutputs": [
                    {
                        "userAccount": WALLET,
                        "mint": BONK,
                        "rawTokenAmount": {"tokenAmount": "5000000000", "decimals": 5},
                    }
                ],
            }
        },
    },
    {
        "signature": "sig-sell",
        "timestamp": 1_757_003_600,
        "type": "SWAP",
        "source": "JUPITER",
        "fee": 5000,
        "events": {
            "swap": {
                "tokenInputs": [
                    {
                        "userAccount": WALLET,
                        "mint": BONK,
                        "rawTokenAmount": {"tokenAmount": "5000000000", "decimals": 5},
                    }
                ],
                "tokenOutputs": [
                    {
                        "userAccount": WALLET,
                        "mint": USDC,
                        "rawTokenAmount": {"tokenAmount": "1500000000", "decimals": 6},
                    }
                ],
            }
        },
    },
]

EVM_WALLET = "0x1111111111111111111111111111111111111111"
EVM_POOL = "0x2222222222222222222222222222222222222222"
EVM_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
EVM_TOKEN = "0x6982508145454ce325ddbe47a25d4ec3d2311933"

ETHERSCAN_TOKENTX = {
    "status": "1",
    "message": "OK",
    "result": [
        {
            "hash": "0xaa",
            "timeStamp": "1757000000",
            "from": EVM_WALLET,
            "to": EVM_POOL,
            "contractAddress": EVM_USDC,
            "tokenSymbol": "USDC",
            "tokenDecimal": "6",
            "value": "1000000000",
        },
        {
            "hash": "0xaa",
            "timeStamp": "1757000000",
            "from": EVM_POOL,
            "to": EVM_WALLET,
            "contractAddress": EVM_TOKEN,
            "tokenSymbol": "PEPE",
            "tokenDecimal": "18",
            "value": "2000000000000000000000",
        },
    ],
}

TON_EVENTS = {
    "events": [
        {
            "event_id": "ton-tx-1",
            "timestamp": 1_757_000_000,
            "actions": [
                {
                    "type": "JettonSwap",
                    "JettonSwap": {
                        "ton_in": "5000000000",
                        "amount_out": "1000000000",
                        "jetton_master_out": {
                            "address": "0:token",
                            "symbol": "SCALE",
                            "decimals": 9,
                        },
                        "user_wallet": {"address": "0:me"},
                    },
                }
            ],
        }
    ]
}

HL_FILLS = [
    {
        "coin": "BTC",
        "px": "60000",
        "sz": "0.1",
        "side": "B",
        "time": 1_757_000_000_000,
        "hash": "0xhh",
        "fee": "1.2",
        "closedPnl": "0",
        "dir": "Open Long",
    },
    {
        "coin": "BTC",
        "px": "63000",
        "sz": "0.1",
        "side": "A",
        "time": 1_757_003_600_000,
        "hash": "0xhh2",
        "fee": "1.26",
        "closedPnl": "300",
        "dir": "Close Long",
    },
]


def test_chains_enabled_by_flag_in_g07_order():
    """Порядок фиксирован конфигом (Solana → EVM → BNB → TON), а не порядком во флаге."""
    assert enabled_chains({"CHAINS_ENABLED": "ton,solana,evm"}) == ["solana", "evm", "ton"]
    assert enabled_chains({"CHAINS_ENABLED": ""}) == []
    assert enabled_chains({}) == []
    with pytest.raises(ConfigError):
        enabled_chains({"CHAINS_ENABLED": "aptos"})


def test_config_order_starts_with_solana_and_ends_with_ton():
    assert load_chains().order == ["solana", "evm", "bnb", "ton", "hyperliquid_user"]


def test_disabled_chain_is_not_constructed():
    with pytest.raises(ChainDisabled):
        make_chain_feed("ton", FakeHttpTransport(), env={"CHAINS_ENABLED": "solana"})
    feed = make_chain_feed("solana", FakeHttpTransport(), env={"CHAINS_ENABLED": "solana"})
    assert isinstance(feed, SolanaFeed)


def test_chain_feeds_satisfy_feed_protocol():
    for feed in (
        SolanaFeed(FakeHttpTransport()),
        EvmFeed(FakeHttpTransport(), network="ethereum"),
        TonFeed(FakeHttpTransport()),
        HyperliquidUserFeed(FakeHttpTransport()),
    ):
        assert isinstance(feed, Feed)


def test_solana_wallet_trades_parsed_from_enhanced_transactions():
    transport = FakeHttpTransport()
    transport.route("GET", "/v0/addresses/", HELIUS_SWAPS)
    quota = MemoryFeedsRegistry()
    feed = SolanaFeed(transport, api_key="k", quota=quota)

    trades = feed.wallet_trades(WALLET)

    assert [t.side for t in trades] == ["buy", "sell"]
    buy, sell = trades
    assert buy.token == BONK and buy.qty == Decimal("50000")
    assert buy.quote_asset == "USDC" and buy.quote_qty == Decimal("1000")
    assert buy.price == Decimal("0.02")  # 1000 USDC / 50000 BONK
    assert buy.value_usd == Decimal("1000")
    assert sell.price == Decimal("0.03") and sell.tx == "sig-sell"
    assert buy.ts == datetime(2025, 9, 4, 15, 33, 20, tzinfo=UTC)
    assert quota.calls["solana"] == 1


def test_solana_health_without_key_is_down():
    health = SolanaFeed(FakeHttpTransport(), api_key=None).health()
    assert health.status == "down" and "HELIUS_API_KEY" in health.detail


def test_evm_wallet_trades_group_transfers_into_swap():
    transport = FakeHttpTransport()
    transport.route("GET", "etherscan.io", ETHERSCAN_TOKENTX)
    quota = MemoryFeedsRegistry()
    feed = EvmFeed(transport, network="base", api_key="k", quota=quota)

    (trade,) = feed.wallet_trades(EVM_WALLET)

    assert trade.side == "buy" and trade.symbol == "PEPE"
    assert trade.qty == Decimal("2000") and trade.quote_qty == Decimal("1000")
    assert trade.price == Decimal("0.5") and trade.chain == "base"
    assert quota.calls["evm"] == 1
    # Etherscan V2: один ключ на все сети, сеть — параметром chainid
    assert transport.calls[0].params["chainid"] == 8453


def test_evm_supports_bnb_as_own_chain_id():
    assert EvmFeed(FakeHttpTransport(), network="bnb").chain_id == 56


def test_ton_wallet_trades_from_tonapi_events():
    transport = FakeHttpTransport()
    transport.route("GET", "tonapi.io", TON_EVENTS)
    feed = TonFeed(transport, api_key=None)

    (trade,) = feed.wallet_trades("0:me")

    assert trade.side == "buy" and trade.symbol == "SCALE"
    assert trade.quote_asset == "TON" and trade.quote_qty == Decimal("5")
    assert trade.qty == Decimal("1") and trade.price == Decimal("5")
    assert trade.value_usd is None  # TON-квота: в USD не переводим без цены


def test_hyperliquid_user_fills_are_trades_with_venue_weight():
    transport = FakeHttpTransport()
    transport.route("POST", "/info", HL_FILLS)
    quota = MemoryFeedsRegistry()
    feed = HyperliquidUserFeed(transport, quota=quota)

    trades = feed.wallet_trades("0xleader")

    assert [t.side for t in trades] == ["buy", "sell"]
    assert trades[0].price == Decimal("60000") and trades[0].qty == Decimal("0.1")
    assert trades[1].pnl == Decimal("300")
    assert quota.used["hyperliquid"] == 20  # вес запроса HL — 20 (таск 04)
    assert transport.calls[0].json["type"] == "userFills"


def test_hyperliquid_history_is_capped_at_10k_fills():
    transport = FakeHttpTransport()
    transport.route("POST", "/info", HL_FILLS)
    feed = HyperliquidUserFeed(transport)
    from_ts = datetime(2025, 1, 1, tzinfo=UTC)
    to_ts = datetime(2025, 2, 1, tzinfo=UTC)

    trades = feed.wallet_trades("0xleader", from_ts, to_ts)

    assert transport.calls[0].json["type"] == "userFillsByTime"
    assert feed.MAX_FILLS == 10_000
    assert len(trades) <= feed.MAX_FILLS


def test_trades_method_of_feed_maps_wallet_swaps():
    transport = FakeHttpTransport()
    transport.route("GET", "/v0/addresses/", HELIUS_SWAPS)
    feed = SolanaFeed(transport, api_key="k")
    rows = feed.trades(WALLET, datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert [r.side for r in rows] == ["buy", "sell"] and rows[0].qty == Decimal("50000")
