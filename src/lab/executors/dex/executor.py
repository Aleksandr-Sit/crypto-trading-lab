"""DEX-исполнители за контрактом `Executor`: Jupiter, Uniswap/Aerodrome, PancakeSwap, STON.fi.

Режим задаётся экземпляру (как в `executors.cex` и `executors.polymarket`): бумажный
экземпляр считает позиции и баланс у себя, живой — отправляет транзакции; ордер с чужим
режимом отклоняется. Один класс на все четыре площадки: различаются они клиентом свопа
и тарифом, а правила защиты — общие.

Что здесь принципиально:
- проскальзывание и приоритетная fee берутся из манифеста стратегии (`set_limits`) или
  `config/meme.yaml`; превышение — отказ **до** отправки (История 66);
- каждая попытка транзакции записывается с причиной и стоимостью, включая неудачную:
  застрявшая повторяется с бо́льшим приоритетом, съеденная MEV — нет (История 70).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

from lab.contracts import (
    Balance,
    Costs,
    Fill,
    Health,
    KeyRights,
    ModeLiteral,
    Order,
    OrderIntent,
    OrderState,
    Position,
)
from lab.core.costs import CostModel, default_model
from lab.executors.dex.swap import (
    SwapClient,
    SwapLimits,
    SwapPlan,
    SwapQuote,
    TxAttempt,
    limits_from_config,
    swap_limits_from_manifest,
)
from lab.feeds import NullQuota, QuotaSink


class DexError(RuntimeError):
    """Отказ DEX-исполнителя: чужой режим, неизвестный ордер, отсутствие маршрута."""


class NotConnected(DexError):
    """Нет ключа горячего кошелька — живая торговля в сети невозможна."""


class TradingUnavailable(DexError):
    """Нет библиотеки подписи (solders / web3) — ветка идёт в режиме «только замер»."""


class PaperFill(Fill):
    """Бумажный филл плюс цена-ориентир на момент решения (для `Journal.record_fill`)."""

    ref_price: Decimal


class DexExecutor:
    """`client=None` — бумага по цене заявки: без сети и без котировок."""

    venue: str = "dex"
    chain: str = ""
    branch = "meme"
    quote_asset: str = "USDC"
    key_env: str = ""

    def __init__(
        self,
        client: SwapClient | None = None,
        *,
        mode: ModeLiteral = "paper",
        costs: CostModel | None = None,
        quota: QuotaSink | None = None,
        limits: SwapLimits | None = None,
        paper_balance: Decimal = Decimal(10000),
        private_key: str | None = None,
    ) -> None:
        self.client = client
        self.mode: ModeLiteral = mode
        self.costs = costs or default_model()
        self.quota = quota or NullQuota()
        self.limits = limits or limits_from_config()
        self.private_key = private_key
        self._limits_by_strategy: dict[str, SwapLimits] = {}
        self._ids = count(1)
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._positions: dict[str, Position] = {}
        self._costs: dict[str, Costs] = {}
        self.attempts: list[TxAttempt] = []
        self._cash = paper_balance

    # -- лимиты защиты -------------------------------------------------------------------

    def set_limits(self, strategy_id: str, limits: SwapLimits) -> None:
        """Лимиты стратегии: worker кладёт сюда `swap_limits_from_manifest(manifest)`."""
        self._limits_by_strategy[strategy_id] = limits

    def limits_for(self, strategy_id: str) -> SwapLimits:
        return self._limits_by_strategy.get(strategy_id, self.limits)

    # -- ключи -----------------------------------------------------------------------------

    def rights(self) -> KeyRights:
        """Ключ горячего кошелька подписывает свопы; вывода средств у исполнителя нет."""
        return KeyRights(trade=bool(self.private_key), withdraw=False)

    # -- ордера -----------------------------------------------------------------------------

    def place(self, intent: OrderIntent, mode: ModeLiteral) -> Order:
        if mode != self.mode:
            raise DexError(f"{self.venue}: исполнитель в режиме {self.mode}, ордер в режиме {mode}")
        if mode == "live" and not self.private_key:
            raise NotConnected(f"{self.venue}: нет ключа {self.key_env} — живой своп невозможен")
        limits = self.limits_for(intent.strategy_id)
        now = datetime.now(UTC)
        order = Order(
            id=f"{self.venue}-{mode}-{next(self._ids)}",
            signal_id=intent.signal_id,
            venue=self.venue,
            client_order_id=intent.client_order_id,
            mode=mode,
            state=OrderState.NEW,
            instrument=intent.instrument,
            side=intent.side,
            qty=intent.qty,
            price=intent.price,
            created_at=now,
        )
        quote = self._quote(intent)
        refusal = self._refusal(quote, limits)
        if refusal:
            return self._store(
                order.model_copy(update={"state": OrderState.REJECTED, "reason": refusal})
            )
        price = (quote.price if quote is not None else intent.price) or Decimal(0)
        if price <= 0:
            return self._store(
                order.model_copy(
                    update={
                        "state": OrderState.REJECTED,
                        "reason": f"{self.venue}: нет цены маршрута",
                    }
                )
            )
        if mode == "paper":
            return self._fill(order, intent, price, quote, Costs())
        return self._send(order, intent, quote, limits)

    def _quote(self, intent: OrderIntent) -> SwapQuote | None:
        if self.client is None:
            return None
        self.quota.use(self.venue, 1)
        return self.client.quote(intent.instrument, intent.side, intent.qty, price=intent.price)

    def _refusal(self, quote: SwapQuote | None, limits: SwapLimits) -> str:
        """Отказ до отправки: превышен потолок проскальзывания или приоритетной fee."""
        if limits.priority_fee_usd > limits.max_priority_fee_usd:
            return (
                f"{self.venue}: приоритет ${limits.priority_fee_usd} выше потолка "
                f"${limits.max_priority_fee_usd}"
            )
        if quote is not None and quote.price_impact_pct > limits.max_slippage_pct:
            return (
                f"{self.venue}: проскальзывание маршрута {quote.price_impact_pct}% выше "
                f"потолка {limits.max_slippage_pct}%"
            )
        return ""

    # -- отправка и повторы ------------------------------------------------------------------

    def _send(
        self, order: Order, intent: OrderIntent, quote: SwapQuote | None, limits: SwapLimits
    ) -> Order:
        if self.client is None:
            raise TradingUnavailable(f"{self.venue}: клиент свопа не подключён")
        quote = quote or SwapQuote(price=intent.price or Decimal(0))
        priority = limits.priority_fee_usd
        gas_total, priority_total = Decimal(0), Decimal(0)
        last = None
        for attempt in range(1, max(1, limits.max_attempts) + 1):
            plan = SwapPlan(
                instrument=intent.instrument,
                side=intent.side,
                qty=intent.qty,
                quote=quote,
                slippage_pct=limits.slippage_pct,
                priority_fee_usd=priority,
                attempt=attempt,
                client_order_id=intent.client_order_id,
            )
            self.quota.use(self.venue, 1)
            result = self.client.send(plan)
            gas_total += result.gas_usd
            priority_total += result.priority_fee_usd
            self.attempts.append(
                TxAttempt(
                    order_id=order.id,
                    attempt=attempt,
                    status=result.status,
                    tx=result.tx,
                    reason=result.reason,
                    gas_usd=result.gas_usd,
                    priority_fee_usd=result.priority_fee_usd,
                    at=datetime.now(UTC),
                )
            )
            last = result
            if result.status == "confirmed":
                attempt_costs = Costs(gas=gas_total, priority_fee=priority_total)
                return self._fill(order, intent, result.price or quote.price, quote, attempt_costs)
            if result.status == "stuck":  # застряла — повтор с бо́льшим приоритетом
                escalated = priority * limits.retry_priority_multiplier
                if escalated > limits.max_priority_fee_usd:
                    break
                priority = escalated
        reason = (
            f"{self.venue}: транзакция не прошла ({last.status if last else 'нет ответа'})"
            f"{': ' + last.reason if last and last.reason else ''}"
        )
        self._costs[order.id] = Costs(gas=gas_total, priority_fee=priority_total)
        return self._store(
            order.model_copy(update={"state": OrderState.REJECTED, "reason": reason})
        )

    # -- филл и состояние ---------------------------------------------------------------------

    def _fill(
        self,
        order: Order,
        intent: OrderIntent,
        price: Decimal,
        quote: SwapQuote | None,
        attempt_costs: Costs,
    ) -> Order:
        now = datetime.now(UTC)
        estimate = self._estimate(intent, quote)
        fill = PaperFill(
            id=f"{order.id}-f1",
            order_id=order.id,
            price=price,
            qty=intent.qty,
            fee=estimate.fee,
            fee_asset=self.quote_asset,
            ts=now,
            ref_price=quote.price if quote is not None else price,
        )
        self._fills.append(fill)
        self._absorb(intent, fill)
        # Факт бьёт оценку: газ и приоритет живых попыток (включая неудачные) заменяют тариф.
        live = attempt_costs.gas > 0 or attempt_costs.priority_fee > 0
        self._costs[order.id] = estimate.model_copy(
            update={
                "gas": attempt_costs.gas if live else estimate.gas,
                "priority_fee": attempt_costs.priority_fee if live else estimate.priority_fee,
            }
        )
        return self._store(
            order.model_copy(update={"state": OrderState.FILLED, "filled_qty": intent.qty})
        )

    def _estimate(self, intent: OrderIntent, quote: SwapQuote | None) -> Costs:
        try:
            return self.costs.estimate(self.venue, intent)
        except Exception:  # noqa: BLE001 — тариф площадки может быть не описан
            return Costs()

    def _store(self, order: Order) -> Order:
        self._orders[order.id] = order
        return order

    def _absorb(self, intent: OrderIntent, fill: Fill) -> None:
        notional = fill.price * fill.qty
        self._cash += (-notional if intent.side == "buy" else notional) - fill.fee
        pos = self._positions.get(intent.instrument)
        signed = fill.qty if intent.side == "buy" else -fill.qty
        if pos is None:
            if signed != 0:
                self._positions[intent.instrument] = Position(
                    instrument=intent.instrument,
                    side="buy" if signed > 0 else "sell",
                    qty=abs(signed),
                    entry_price=fill.price,
                )
            return
        current = pos.qty if pos.side == "buy" else -pos.qty
        total = current + signed
        if total == 0:
            self._positions.pop(intent.instrument, None)
            return
        entry = pos.entry_price
        if (current >= 0) == (signed >= 0):
            entry = (abs(current) * pos.entry_price + abs(signed) * fill.price) / abs(total)
        self._positions[intent.instrument] = pos.model_copy(
            update={"qty": abs(total), "side": "buy" if total > 0 else "sell", "entry_price": entry}
        )

    def cancel(self, order_id: str) -> Order:
        """Своп в сети не отменяют — отменить можно только неотправленный ордер."""
        order = self._orders.get(order_id)
        if order is None:
            raise DexError(f"{self.venue}: ордер {order_id} неизвестен")
        if order.state == OrderState.FILLED:
            raise DexError(f"{self.venue}: своп {order_id} уже исполнен — отмена невозможна")
        return self._store(order.model_copy(update={"state": OrderState.CANCELLED}))

    def positions(self) -> Sequence[Position]:
        return list(self._positions.values())

    def fills(self, since: datetime) -> Sequence[Fill]:
        return [f for f in self._fills if f.ts >= since]

    def balance(self) -> Sequence[Balance]:
        now = datetime.now(UTC)
        return [Balance(asset=self.quote_asset, total=self._cash, free=self._cash, as_of=now)]

    # -- издержки попыток -----------------------------------------------------------------

    def costs_of(self, order_id: str) -> Costs:
        """Издержки ордера, включая газ и приоритет неудачных попыток (История 70)."""
        return self._costs.get(order_id, Costs())

    def failed_costs(self, *, order_id: str | None = None) -> Costs:
        """Что стоили неудачные попытки — «сгоревшие» деньги без филла."""
        gas = priority = Decimal(0)
        for attempt in self.attempts:
            if not attempt.failed or (order_id is not None and attempt.order_id != order_id):
                continue
            gas += attempt.gas_usd
            priority += attempt.priority_fee_usd
        return Costs(gas=gas, priority_fee=priority)

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.state in (OrderState.NEW, OrderState.OPEN)]

    def health(self) -> Health:
        now = datetime.now(UTC)
        if self.client is None:
            return Health(
                status="degraded",
                detail=f"{self.venue}: клиента свопа нет — бумага по цене заявки",
                checked_at=now,
            )
        probe = getattr(self.client, "health", None)
        if callable(probe):
            return probe()
        return Health(status="ok", detail=self.venue, checked_at=now)


class JupiterExecutor(DexExecutor):
    venue = "jupiter"
    chain = "solana"
    quote_asset = "USDC"
    key_env = "SOLANA_HOT_WALLET_KEY"


class UniswapExecutor(DexExecutor):
    """Ethereum и Base: Uniswap, на Base — Aerodrome (маршрут выбирает клиент)."""

    venue = "uniswap"
    chain = "ethereum"
    quote_asset = "USDC"
    key_env = "EVM_HOT_WALLET_KEY"


class PancakeExecutor(DexExecutor):
    venue = "pancake"
    chain = "bnb"
    quote_asset = "USDT"
    key_env = "EVM_HOT_WALLET_KEY"


class StonFiExecutor(DexExecutor):
    venue = "stonfi"
    chain = "ton"
    quote_asset = "TON"
    key_env = "TON_HOT_WALLET_MNEMONIC"


EXECUTORS: dict[str, type[DexExecutor]] = {
    "jupiter": JupiterExecutor,
    "uniswap": UniswapExecutor,
    "pancake": PancakeExecutor,
    "stonfi": StonFiExecutor,
}


def make_executor(
    venue: str,
    client: SwapClient | None = None,
    *,
    mode: ModeLiteral = "paper",
    quota: QuotaSink | None = None,
    limits: SwapLimits | None = None,
    private_key: str | None = None,
    env: dict[str, str] | None = None,
) -> DexExecutor:
    try:
        cls = EXECUTORS[venue]
    except KeyError as err:
        raise DexError(f"неизвестная DEX-площадка {venue!r}") from err
    if private_key is None and mode == "live":
        import os

        private_key = (env or os.environ).get(cls.key_env) or None
    if client is None and mode == "live":  # живой клиент грузится только для live
        from lab.executors.dex.clients import make_client

        client = make_client(venue, private_key=private_key)
    return cls(client, mode=mode, quota=quota, limits=limits, private_key=private_key)


__all__ = [
    "EXECUTORS",
    "DexError",
    "DexExecutor",
    "JupiterExecutor",
    "NotConnected",
    "PaperFill",
    "PancakeExecutor",
    "StonFiExecutor",
    "TradingUnavailable",
    "UniswapExecutor",
    "make_executor",
    "swap_limits_from_manifest",
]
