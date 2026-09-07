"""Фейковый клиент свопа: котировка задаётся, исходы транзакций программируются.

Тесты DEX-исполнителей не ходят в сеть и не подписывают ничего — это и есть подстановка.
"""

from __future__ import annotations

from decimal import Decimal

from lab.executors.dex.swap import SwapPlan, SwapQuote, TxResult, TxStatus


class FakeSwapClient:
    def __init__(
        self,
        *,
        price: Decimal = Decimal(1),
        price_impact_pct: Decimal = Decimal(0),
        liquidity_usd: Decimal | None = None,
        gas_usd: Decimal = Decimal("0.01"),
        offline: bool = False,
    ) -> None:
        self.price = price
        self.price_impact_pct = price_impact_pct
        self.liquidity_usd = liquidity_usd
        self.gas_usd = gas_usd
        self.offline = offline
        self.sent: list[SwapPlan] = []
        self.quotes: list[tuple[str, str, Decimal]] = []
        self._failures: list[tuple[TxStatus, Decimal, str]] = []

    def fail_next(
        self,
        status: TxStatus = "failed",
        *,
        times: int = 1,
        gas_usd: Decimal | None = None,
        reason: str = "",
    ) -> FakeSwapClient:
        for _ in range(times):
            gas = gas_usd if gas_usd is not None else self.gas_usd
            self._failures.append((status, gas, reason))
        return self

    def quote(self, instrument: str, side: str, qty: Decimal, **kw) -> SwapQuote:
        self.quotes.append((instrument, side, qty))
        return SwapQuote(
            price=self.price,
            out_amount=qty * self.price,
            price_impact_pct=self.price_impact_pct,
            liquidity_usd=self.liquidity_usd,
            route="fake",
        )

    def send(self, plan: SwapPlan) -> TxResult:
        self.sent.append(plan)
        n = len(self.sent)
        if self._failures:
            status, gas, reason = self._failures.pop(0)
            return TxResult(
                status=status,
                tx=f"tx-fail-{n}",
                gas_usd=gas,
                priority_fee_usd=plan.priority_fee_usd,
                reason=reason or f"транзакция: {status}",
            )
        return TxResult(
            status="confirmed",
            tx=f"tx-{n}",
            price=plan.quote.price,
            gas_usd=self.gas_usd,
            priority_fee_usd=plan.priority_fee_usd,
        )


__all__ = ["FakeSwapClient"]
