"""DEX-исполнители ранней стадии: Jupiter, Uniswap/Aerodrome, PancakeSwap, STON.fi."""

from lab.executors import registry
from lab.executors.dex.executor import (
    EXECUTORS,
    DexError,
    DexExecutor,
    JupiterExecutor,
    NotConnected,
    PancakeExecutor,
    PaperFill,
    StonFiExecutor,
    TradingUnavailable,
    UniswapExecutor,
    make_executor,
)
from lab.executors.dex.swap import (
    SwapClient,
    SwapLimits,
    SwapPlan,
    SwapQuote,
    TxAttempt,
    TxResult,
    limits_from_config,
    swap_limits_from_manifest,
)


def _register() -> None:
    """Фабрика без аргументов — бумажный исполнитель без клиента: сеть не трогается."""
    for name, cls in EXECUTORS.items():
        registry.register(name, (lambda c=cls: c(mode="paper")), replace=True)


_register()

__all__ = [
    "EXECUTORS",
    "DexError",
    "DexExecutor",
    "JupiterExecutor",
    "NotConnected",
    "PaperFill",
    "PancakeExecutor",
    "StonFiExecutor",
    "SwapClient",
    "SwapLimits",
    "SwapPlan",
    "SwapQuote",
    "StonFiExecutor",
    "TradingUnavailable",
    "TxAttempt",
    "TxResult",
    "UniswapExecutor",
    "limits_from_config",
    "make_executor",
    "swap_limits_from_manifest",
]
