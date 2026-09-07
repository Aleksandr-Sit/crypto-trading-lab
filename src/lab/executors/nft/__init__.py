"""NFT-исполнители: покупка/листинг на площадках и минт (Solana Candy Machine, EVM).

Blur и Alchemy сюда не попадают: они помечены read-only, торгового исполнителя у них нет
(История 81) — и лучше явный отказ, чем исполнитель, который молча ничего не делает.
"""

from datetime import UTC, datetime

from lab.executors import registry
from lab.executors.access import BranchMode, set_branch_mode
from lab.executors.nft.clients import (
    EvmMintClient,
    MarketTradeClient,
    SolanaMintClient,
    TradeResult,
)
from lab.executors.nft.executor import (
    EXECUTORS,
    MagicEdenExecutor,
    NftError,
    NftExecutor,
    NotConnected,
    OpenSeaExecutor,
    PaperFill,
    TensorExecutor,
    TradingUnavailable,
    ZoraExecutor,
    make_executor,
)
from lab.executors.nft.fake import FakeMintClient, FakeTradeClient
from lab.executors.nft.mint import (
    MINT_EXECUTORS,
    EvmMintExecutor,
    MintAttempt,
    MintClient,
    MintExecutor,
    MintPlan,
    MintTx,
    SolanaMintExecutor,
    make_mint_executor,
    measure_extra,
    mint_metrics,
)

BRANCH = "nft"


def check_trading_access(executor, *, session=None, branch: str = BRANCH) -> BranchMode:
    """Может ли ветка торговать. Нет ключа — «только замер», а не тихий отказ в бою.

    Worker (T14) зовёт это при старте; `/status` бота печатает результат.
    """
    now = datetime.now(UTC)
    reason = ""
    try:
        rights = executor.rights()
        trade = bool(rights.trade)
    except Exception as exc:  # noqa: BLE001 — недоступность площадки не роняет старт
        trade = False
        reason = str(exc)
    if not trade and not reason:
        env = getattr(executor, "key_env", "") or "кошелька"
        reason = f"нет ключа {env} — минт и покупки идут только на бумаге"
    read_only = not trade
    if session is not None:
        return set_branch_mode(
            session, branch, read_only=read_only, reason=reason, by="system", now=now
        )
    return BranchMode(branch=branch, read_only=read_only, reason=reason, checked_at=now)


def _register() -> None:
    """Фабрика без аргументов — бумажный исполнитель без площадки: сеть не трогается."""
    for name, cls in EXECUTORS.items():
        registry.register(name, (lambda c=cls: c(mode="paper")), replace=True)
    for name, cls in MINT_EXECUTORS.items():
        registry.register(name, (lambda c=cls: c(mode="paper")), replace=True)


_register()

__all__ = [
    "BRANCH",
    "EXECUTORS",
    "MINT_EXECUTORS",
    "EvmMintClient",
    "EvmMintExecutor",
    "FakeMintClient",
    "FakeTradeClient",
    "MagicEdenExecutor",
    "MarketTradeClient",
    "MintAttempt",
    "MintClient",
    "MintExecutor",
    "MintPlan",
    "MintTx",
    "NftError",
    "NftExecutor",
    "NotConnected",
    "OpenSeaExecutor",
    "PaperFill",
    "SolanaMintClient",
    "SolanaMintExecutor",
    "TensorExecutor",
    "TradeResult",
    "TradingUnavailable",
    "ZoraExecutor",
    "check_trading_access",
    "make_executor",
    "make_mint_executor",
    "measure_extra",
    "mint_metrics",
]
