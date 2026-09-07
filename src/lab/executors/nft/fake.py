"""Фейковые клиенты минта и сделок: сценарий задаёт тест, сеть не трогается."""

from __future__ import annotations

from decimal import Decimal

from lab.executors.nft.clients import TradeResult
from lab.executors.nft.mint import MintPlan, MintTx


class FakeMintClient:
    """`script(...)` задаёт исходы попыток по порядку; дальше — успех."""

    def __init__(self, *, gas_usd: Decimal = Decimal("0.02")) -> None:
        self.sent: list[MintPlan] = []
        self.gas_usd = gas_usd
        self._script: list[str] = []
        self.confirm_latency_ms = 700

    def script(self, *statuses: str) -> FakeMintClient:
        self._script = list(statuses)
        return self

    def send(self, plan: MintPlan) -> MintTx:
        self.sent.append(plan)
        status = self._script.pop(0) if self._script else "confirmed"
        ok = status == "confirmed"
        return MintTx(
            status=status,
            tx=f"tx-{len(self.sent)}" if ok else "",
            minted=plan.qty if ok else 0,
            price=plan.price,
            gas_usd=self.gas_usd,
            priority_fee_usd=plan.priority_fee_usd,
            confirm_latency_ms=self.confirm_latency_ms if ok else None,
            reason="" if ok else f"попытка {status}",
        )


class FakeTradeClient:
    """Сделка на площадке в памяти: цена исполнения и газ задаются тестом."""

    def __init__(
        self, *, gas_usd: Decimal = Decimal("0.5"), ok: bool = True, reason: str = ""
    ) -> None:
        self.calls: list[dict] = []
        self.gas_usd = gas_usd
        self.ok = ok
        self.reason = reason
        self.price_override: Decimal | None = None

    def trade(self, **kw) -> TradeResult:
        self.calls.append(kw)
        return TradeResult(
            ok=self.ok,
            price=self.price_override or kw.get("price"),
            tx=f"tx-{len(self.calls)}",
            gas_usd=self.gas_usd,
            reason=self.reason,
        )
