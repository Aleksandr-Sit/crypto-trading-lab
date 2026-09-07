"""Клиенты сети для минта и сделок: Solana (Candy Machine / ME launchpad) и EVM.

Подпись грузится лениво: без `solders`/`web3` ветка честно уходит в «только замер»
(`TradingUnavailable`), а не притворяется, что торгует. Ключи — из `.env` по именам из
`.env.example`, в код и в логи не попадают.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lab.executors.nft.executor import NotConnected, TradingUnavailable
from lab.executors.nft.mint import MintPlan, MintTx

SOLANA_KEY_ENV = "SOLANA_HOT_WALLET_KEY"
EVM_KEY_ENV = "EVM_HOT_WALLET_KEY"


@dataclass(frozen=True)
class TradeResult:
    ok: bool
    price: Decimal | None = None
    tx: str = ""
    gas_usd: Decimal = Decimal(0)
    reason: str = ""


class SolanaMintClient:
    """Candy Machine v3 и launchpad Magic Eden. Программа — из ленты минта (`program`)."""

    chain = "solana"

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        private_key: str | None = None,
        env: dict | None = None,
    ) -> None:
        source = env if env is not None else os.environ
        self.rpc_url = rpc_url or source.get("SOLANA_RPC_URL", "")
        self.private_key = private_key or source.get(SOLANA_KEY_ENV, "")
        self._client: Any = None

    def _signer(self) -> Any:
        if not self.private_key:
            raise NotConnected(f"solana: нет ключа {SOLANA_KEY_ENV}")
        try:
            from solders.keypair import Keypair  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — пакета в дереве нет
            raise TradingUnavailable(
                "solana: нет пакета solders — минт идёт только в режиме бумаги"
            ) from exc
        return Keypair.from_base58_string(self.private_key)

    def send(self, plan: MintPlan) -> MintTx:  # pragma: no cover — живой путь за LAB_LIVE_TESTS
        self._signer()
        raise TradingUnavailable(
            "solana: сборка транзакции Candy Machine не подключена — нужен solders и адрес "
            "программы коллекции"
        )


class EvmMintClient:
    """EVM: вызов mint-функции контракта коллекции (`program` — адрес контракта)."""

    chain = "ethereum"

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        private_key: str | None = None,
        env: dict | None = None,
    ) -> None:
        source = env if env is not None else os.environ
        self.rpc_url = rpc_url or source.get("EVM_RPC_URL", "")
        self.private_key = private_key or source.get(EVM_KEY_ENV, "")

    def _web3(self) -> Any:
        if not self.private_key:
            raise NotConnected(f"evm: нет ключа {EVM_KEY_ENV}")
        try:
            from web3 import Web3  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — пакета в дереве нет
            raise TradingUnavailable(
                "evm: нет пакета web3 — минт идёт только в режиме бумаги"
            ) from exc
        return Web3(Web3.HTTPProvider(self.rpc_url))

    def send(self, plan: MintPlan) -> MintTx:  # pragma: no cover — живой путь за LAB_LIVE_TESTS
        self._web3()
        raise TradingUnavailable(
            "evm: сборка mint-транзакции не подключена — нужен ABI контракта коллекции"
        )


class MarketTradeClient:
    """Сделка на площадке: принять листинг или выставить свой. Живой путь — по ключу."""

    def __init__(self, market: Any = None, *, private_key: str | None = None) -> None:
        self.market = market
        self.private_key = private_key

    def trade(
        self,
        *,
        market: str,
        collection: str,
        token_id: str | None,
        side: str,
        qty: Decimal,
        price: Decimal,
        client_order_id: str = "",
    ) -> TradeResult:  # pragma: no cover — живой путь за LAB_LIVE_TESTS
        if not self.private_key:
            raise NotConnected(f"{market}: нет ключа горячего кошелька")
        raise TradingUnavailable(
            f"{market}: подпись сделки не подключена — нужен клиент площадки (solders/web3)"
        )

    def health_at(self) -> datetime:
        return datetime.now(UTC)
