"""Клиенты сетей за контрактом `Feed` (тикет 08, G07).

Сеть включается флагом `CHAINS_ENABLED` и в порядке `config/chains.yaml`:
Solana → Ethereum/Base → BNB → TON (Hyperliquid user-data — рядом, без флага порядка).
Клиенты выключенной сети не создаются: `make_chain_feed` поднимает `ChainDisabled`.
"""

from __future__ import annotations

from collections.abc import Mapping

from lab.feeds.chains.base import WALLET_TRADE_EVENT, ChainFeed, wallet_trade_payload
from lab.feeds.chains.config import (
    CHAINS_ENV,
    ChainsConfig,
    ChainSpec,
    WalletThresholds,
    enabled_chains,
    load_chains,
    wallet_thresholds,
)
from lab.feeds.chains.evm import CHAIN_IDS, EvmFeed
from lab.feeds.chains.fake import FakeHttpTransport
from lab.feeds.chains.hyperliquid_user import HyperliquidUserFeed
from lab.feeds.chains.solana import SolanaFeed
from lab.feeds.chains.ton import TonFeed
from lab.feeds.chains.transport import (
    ChainDisabled,
    ChainError,
    ChainUnsupported,
    HttpTransport,
    HttpxTransport,
)
from lab.feeds.chains.types import WalletTrade
from lab.feeds.quota import QuotaSink

CHAIN_FEEDS: dict[str, type[ChainFeed]] = {
    "solana": SolanaFeed,
    "evm": EvmFeed,
    "bnb": EvmFeed,
    "ton": TonFeed,
    "hyperliquid_user": HyperliquidUserFeed,
}


def make_chain_feed(
    chain: str,
    transport: HttpTransport | None = None,
    *,
    quota: QuotaSink | None = None,
    env: Mapping[str, str] | None = None,
    network: str | None = None,
    api_key: str | None = None,
) -> ChainFeed:
    """Клиент включённой сети. Ключ — из `env` по `key_env` конфига, если не передан явно."""
    config = load_chains()
    if chain not in CHAIN_FEEDS:
        raise ChainDisabled(f"сеть {chain!r} неизвестна; есть: {', '.join(config.order)}")
    if chain not in enabled_chains(env, config):
        raise ChainDisabled(
            f"сеть {chain!r} выключена: добавь её в {CHAINS_ENV} (порядок G07: "
            f"{', '.join(config.order)})"
        )
    from lab.config import environment

    values = dict(environment()) if env is None else dict(env)
    spec = config.spec(chain)
    key = api_key or (values.get(spec.key_env) if spec.key_env else None)
    if transport is None:
        transport = HttpxTransport()
    kwargs = {"quota": quota, "api_key": key or None, "config": config}
    if CHAIN_FEEDS[chain] is EvmFeed:
        kwargs["network"] = network or ("bnb" if chain == "bnb" else "ethereum")
    return CHAIN_FEEDS[chain](transport, **kwargs)


__all__ = [
    "CHAINS_ENV",
    "CHAIN_FEEDS",
    "CHAIN_IDS",
    "WALLET_TRADE_EVENT",
    "ChainDisabled",
    "ChainError",
    "ChainFeed",
    "ChainSpec",
    "ChainUnsupported",
    "ChainsConfig",
    "EvmFeed",
    "FakeHttpTransport",
    "HttpTransport",
    "HttpxTransport",
    "HyperliquidUserFeed",
    "SolanaFeed",
    "TonFeed",
    "WalletThresholds",
    "WalletTrade",
    "enabled_chains",
    "load_chains",
    "make_chain_feed",
    "wallet_thresholds",
    "wallet_trade_payload",
]
