"""Минт как измеряемая гипотеза (Истории 76, 77; G09, G09.1).

Пользователь сказал прямо: «какое-то рандомное количество можно получить на минте, без
гарантий да, но можно… Будем тестировать и подбирать подход». Поэтому здесь ничего не
отвергается заранее: каждая попытка — в том числе неудачная — записывается с ценой, газом,
приоритетной fee и латентностями, а варианты подхода (приоритетная fee, несколько
кошельков, момент отправки) живут отдельными стратегиями `nft-mint-*`.

Метрики ветки: `mint_attempts`, `mint_success_rate`, `mint_cost_failed`. Без записи
неудачных попыток последняя метрика неотличима от нуля — и минт кажется бесплатным.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from lab.contracts import Costs, MintAttemptSpec, MintResult, ModeLiteral
from lab.executors.nft.executor import NftError, NftExecutor, NotConnected, TradingUnavailable

ZERO = Decimal(0)
RETRYABLE = ("stuck", "failed")  # обрыв и revert лечатся повтором
NOT_RETRYABLE = ("sold_out", "mev", "rejected")


@dataclass(frozen=True)
class MintPlan:
    """Что уходит в сеть: коллекция, кошелёк, приоритет и момент отправки."""

    collection: str
    chain: str
    market: str
    qty: int
    price: Decimal
    wallet: str = ""
    priority_fee_usd: Decimal = ZERO
    attempt: int = 1
    send_offset_s: int = 0
    program: str = ""  # candy machine / адрес контракта


@dataclass(frozen=True)
class MintTx:
    status: str  # confirmed | failed | mev | stuck | sold_out
    tx: str = ""
    minted: int = 0
    price: Decimal | None = None
    gas_usd: Decimal = ZERO
    priority_fee_usd: Decimal = ZERO
    confirm_latency_ms: int | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "confirmed"


@dataclass
class MintAttempt:
    """Попытка минта — то, из чего считаются метрики ветки."""

    collection: str
    chain: str
    market: str
    mode: str
    strategy_id: str = ""
    variant: str = ""
    wallet: str = ""
    attempt: int = 1
    ok: bool = False
    minted: int = 0
    tx: str = ""
    reason: str = ""
    price: Decimal | None = None
    gas_usd: Decimal = ZERO
    priority_fee_usd: Decimal = ZERO
    decision_latency_ms: int | None = None
    confirm_latency_ms: int | None = None
    sent_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def cost(self) -> Decimal:
        return self.gas_usd + self.priority_fee_usd


@runtime_checkable
class MintClient(Protocol):
    """Шов сети: отправка минт-транзакции. В тестах — `FakeMintClient`."""

    def send(self, plan: MintPlan) -> MintTx: ...


def mint_metrics(attempts: Sequence[MintAttempt]) -> dict[str, Any]:
    """`mint_attempts`, `mint_success_rate`, `mint_cost_failed` (История 76).

    Без попыток доля успеха — `None`, а не ноль: ноль означал бы «пробовали и не вышло».
    """
    total = len(attempts)
    if total == 0:
        return {
            "mint_attempts": 0,
            "mint_success_rate": None,
            "mint_cost_failed": ZERO,
            "mint_minted": 0,
        }
    ok = sum(1 for a in attempts if a.ok)
    failed_cost = sum((a.cost for a in attempts if not a.ok), start=ZERO)
    return {
        "mint_attempts": total,
        "mint_success_rate": Decimal(ok) / Decimal(total),
        "mint_cost_failed": failed_cost,
        "mint_minted": sum(a.minted for a in attempts),
    }


def measure_extra(attempts: Sequence[MintAttempt]) -> dict[str, Any]:
    """Метрики минта в `core.measure.run(..., extra_metrics=)`."""
    return {"mint": mint_metrics(attempts)}


class MintExecutor(NftExecutor):
    """Минт поверх обычного NFT-исполнителя: `mint(spec)` из границ модуля `nft`."""

    venue = "nft_mint"
    chain = ""
    quote_asset = "USD"

    def __init__(self, *a, max_attempts: int | None = None, **kw) -> None:
        super().__init__(*a, **kw)
        self.attempts: list[MintAttempt] = []
        self.max_attempts = max_attempts or self.config.mint.max_attempts

    # -- контракт NftMarket.mint ---------------------------------------------------------

    def mint(
        self,
        spec: MintAttemptSpec,
        *,
        strategy_id: str = "",
        variant: str = "",
        wallets: Sequence[str] | None = None,
        price: Decimal | None = None,
        decided_at: datetime | None = None,
        program: str = "",
        send_offset_s: int = 0,
        now: datetime | None = None,
    ) -> MintResult:
        """Одна попытка минта на кошелёк; при обрыве — повтор до `max_attempts`.

        `price` — фактическая цена минта, если она известна из ленты; выше `spec.max_price`
        не минтим и в сеть не идём.
        """
        if spec.mode != self.mode and spec.mode in ("paper", "live"):
            raise NftError(
                f"{self.venue}: исполнитель в режиме {self.mode}, попытка в режиме {spec.mode}"
            )
        if self.mode == "live" and not self.private_key:
            raise NotConnected(f"{self.venue}: нет ключа {self.key_env or 'кошелька'} для минта")
        at = now or datetime.now(UTC)
        actual = price if price is not None else spec.max_price
        if actual > spec.max_price:
            return MintResult(
                ok=False,
                detail=(
                    f"{self.venue}: цена минта {actual} выше потолка {spec.max_price} — "
                    "не отправляем"
                ),
            )
        cap = self.config.mint.max_price_usd
        if cap > 0 and actual * spec.qty > cap:
            return MintResult(
                ok=False,
                detail=f"{self.venue}: цена минта {actual} выше лимита конфига {cap}",
            )

        latency_ms = None
        if decided_at is not None:
            latency_ms = int((at - decided_at).total_seconds() * 1000)

        minted = 0
        gas = ZERO
        priority = ZERO
        last: MintTx | None = None
        tx = ""
        for wallet in list(wallets or [""]):
            for attempt in range(1, self.max_attempts + 1):
                plan = MintPlan(
                    collection=spec.collection,
                    chain=spec.chain or self.chain,
                    market=spec.market,
                    qty=spec.qty,
                    price=actual,
                    wallet=wallet,
                    priority_fee_usd=spec.priority_fee,
                    attempt=attempt,
                    send_offset_s=send_offset_s,
                    program=program,
                )
                result = self._send_mint(plan)
                record = MintAttempt(
                    collection=spec.collection,
                    chain=plan.chain,
                    market=spec.market,
                    mode=self.mode,
                    strategy_id=strategy_id,
                    variant=variant,
                    wallet=wallet,
                    attempt=attempt,
                    ok=result.ok,
                    minted=result.minted,
                    tx=result.tx,
                    reason=result.reason,
                    price=result.price or actual,
                    gas_usd=result.gas_usd,
                    priority_fee_usd=result.priority_fee_usd,
                    decision_latency_ms=latency_ms,
                    confirm_latency_ms=result.confirm_latency_ms,
                    sent_at=at,
                )
                self.attempts.append(record)
                gas += result.gas_usd
                priority += result.priority_fee_usd
                last = result
                if result.ok:
                    minted += result.minted
                    tx = tx or result.tx
                    break
                if result.status not in RETRYABLE:
                    break  # «разобрали» повтором не лечится

        costs = Costs(gas=gas, priority_fee=priority)
        if minted:
            return MintResult(ok=True, tx_id=tx, minted=minted, costs=costs)
        return MintResult(
            ok=False,
            minted=0,
            costs=costs,
            detail=f"{self.venue}: минт не прошёл ({last.status if last else 'нет попыток'})"
            + (f": {last.reason}" if last and last.reason else ""),
        )

    def _send_mint(self, plan: MintPlan) -> MintTx:
        if self.mode == "paper":
            return MintTx(
                status="confirmed",
                tx=f"paper-mint-{len(self.attempts) + 1}",
                minted=plan.qty,
                price=plan.price,
                gas_usd=self.config.costs.for_market(plan.market)[2],
                priority_fee_usd=plan.priority_fee_usd,
                confirm_latency_ms=0,
            )
        if self.client is None:
            raise TradingUnavailable(f"{self.venue}: клиент минта не подключён")
        self.quota.use(self.venue, 1)
        return self.client.send(plan)

    # -- метрики --------------------------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        return mint_metrics(self.attempts)

    def failed_costs(self) -> Costs:
        """Что стоили неудачные попытки — `mint_cost_failed` в деньгах."""
        gas = sum((a.gas_usd for a in self.attempts if not a.ok), start=ZERO)
        priority = sum((a.priority_fee_usd for a in self.attempts if not a.ok), start=ZERO)
        return Costs(gas=gas, priority_fee=priority)


class SolanaMintExecutor(MintExecutor):
    """Solana: Candy Machine и launchpad Magic Eden."""

    venue = "nft_mint_solana"
    chain = "solana"
    quote_asset = "SOL"
    key_env = "SOLANA_HOT_WALLET_KEY"


class EvmMintExecutor(MintExecutor):
    """EVM: вызов mint-функции контракта коллекции."""

    venue = "nft_mint_evm"
    chain = "ethereum"
    quote_asset = "ETH"
    key_env = "EVM_HOT_WALLET_KEY"


MINT_EXECUTORS: dict[str, type[MintExecutor]] = {
    "nft_mint_solana": SolanaMintExecutor,
    "nft_mint_evm": EvmMintExecutor,
}


def make_mint_executor(
    chain: str, *, mode: ModeLiteral = "paper", client: MintClient | None = None, **kw
) -> MintExecutor:
    name = "nft_mint_solana" if chain in ("solana", "sol") else "nft_mint_evm"
    return MINT_EXECUTORS[name](mode=mode, client=client, **kw)
