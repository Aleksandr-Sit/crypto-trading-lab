"""Типы свопа: котировка маршрута, план отправки, результат транзакции, лимиты защиты.

Лимиты (`SwapLimits`) приезжают из манифеста стратегии или `config/meme.yaml`: превышение
проскальзывания или приоритетной fee — отказ до отправки, а не «попробуем и посмотрим»
(История 66).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

TxStatus = Literal["confirmed", "failed", "mev", "stuck"]
FAILED_STATUSES: tuple[str, ...] = ("failed", "mev", "stuck")


@dataclass(frozen=True)
class SwapQuote:
    price: Decimal
    out_amount: Decimal = Decimal(0)
    price_impact_pct: Decimal = Decimal(0)
    fee_bps: Decimal | None = None
    liquidity_usd: Decimal | None = None
    route: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SwapPlan:
    """Что уходит в сеть: маршрут, защита от проскальзывания и приоритетная fee."""

    instrument: str
    side: str
    qty: Decimal
    quote: SwapQuote
    slippage_pct: Decimal
    priority_fee_usd: Decimal
    attempt: int = 1
    client_order_id: str = ""


@dataclass(frozen=True)
class TxResult:
    status: TxStatus
    tx: str = ""
    price: Decimal | None = None
    gas_usd: Decimal = Decimal(0)
    priority_fee_usd: Decimal = Decimal(0)
    reason: str = ""


@dataclass(frozen=True)
class TxAttempt:
    """Попытка транзакции — в том числе неудачная: причина и стоимость (История 70)."""

    order_id: str
    attempt: int
    status: str
    tx: str = ""
    reason: str = ""
    gas_usd: Decimal = Decimal(0)
    priority_fee_usd: Decimal = Decimal(0)
    at: datetime = datetime(1970, 1, 1, tzinfo=UTC)

    @property
    def failed(self) -> bool:
        return self.status in FAILED_STATUSES


@dataclass(frozen=True)
class SwapLimits:
    """Защита сделки. `max_slippage_pct` — потолок ожидаемого проскальзывания маршрута."""

    max_slippage_pct: Decimal = Decimal(5)
    slippage_pct: Decimal = Decimal("1.5")
    priority_fee_usd: Decimal = Decimal("0.05")
    max_priority_fee_usd: Decimal = Decimal(1)
    retry_priority_multiplier: Decimal = Decimal(2)
    max_attempts: int = 3


def _dec(value: Any, default: Decimal) -> Decimal:
    if value is None:
        return default
    return Decimal(str(value))


def swap_limits_from_manifest(source: Any, *, defaults: SwapLimits | None = None) -> SwapLimits:
    """Лимиты из манифеста стратегии (`manifest.params`) или готового словаря параметров."""
    params = getattr(source, "params", source) or {}
    base = defaults or SwapLimits()
    return SwapLimits(
        max_slippage_pct=_dec(params.get("max_slippage_pct"), base.max_slippage_pct),
        slippage_pct=_dec(params.get("slippage_pct"), base.slippage_pct),
        priority_fee_usd=_dec(params.get("priority_fee_usd"), base.priority_fee_usd),
        max_priority_fee_usd=_dec(params.get("max_priority_fee_usd"), base.max_priority_fee_usd),
        retry_priority_multiplier=_dec(
            params.get("retry_priority_multiplier"), base.retry_priority_multiplier
        ),
        max_attempts=int(params.get("max_attempts", base.max_attempts)),
    )


def limits_from_config(config: Any = None) -> SwapLimits:
    """Лимиты по умолчанию — из `config/meme.yaml` (секция `execution`)."""
    if config is None:
        from lab.feeds.dex.config import load_meme

        config = load_meme().execution
    return SwapLimits(
        max_slippage_pct=config.max_slippage_pct,
        slippage_pct=config.slippage_pct,
        priority_fee_usd=config.priority_fee_usd,
        max_priority_fee_usd=config.max_priority_fee_usd,
        retry_priority_multiplier=config.retry_priority_multiplier,
        max_attempts=config.max_attempts,
    )


@runtime_checkable
class SwapClient(Protocol):
    """Шов сети: котировка маршрута и отправка транзакции. В тестах — `FakeSwapClient`."""

    def quote(self, instrument: str, side: str, qty: Decimal, **kw: Any) -> SwapQuote: ...

    def send(self, plan: SwapPlan) -> TxResult: ...


__all__ = [
    "FAILED_STATUSES",
    "SwapClient",
    "SwapLimits",
    "SwapPlan",
    "SwapQuote",
    "TxAttempt",
    "TxResult",
    "TxStatus",
    "limits_from_config",
    "swap_limits_from_manifest",
]
