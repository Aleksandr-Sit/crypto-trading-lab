"""`pm-copy-*` — копия позиций топ-кошелька Polymarket (История 82).

Копируется не сделка, а **позиция**: Data API отдаёт срез позиций кошелька, и лидер для нас —
это набор «сколько контрактов какого исхода он держит». Разница между его позицией (в нашем
масштабе) и нашей и есть сигнал: вход, доливка, выход. Так копия не разъезжается с лидером,
даже если мы пропустили одно обновление опроса.

Масштаб — доля нашего капитала от капитала лидера, как в `strategies.copy`: у лидера 100k,
у нас 1k, значит копируем 1% его размера, а не его абсолют.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from lab.contracts import Branch, Event, Signal, StopSpec, StrategyManifest
from lab.strategies.base import Strategy

POSITION_EVENT = "pm_position"
VENUE = "polymarket"
SOURCE_KIND = "pm-copy"


def _dec(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value not in (None, "") else default))


def _num(value: Decimal) -> str:
    return format(value.normalize(), "f")


def slug_of(address: str) -> str:
    keep = [c if (c.isalnum() or c == "-") else "-" for c in address.lower()]
    return "".join(keep).strip("-")


def pm_copy_manifest(
    wallet: str,
    *,
    params: dict[str, Any],
    instruments: list[str] | None = None,
    description: str = "",
) -> StrategyManifest:
    base: dict[str, Any] = {
        "leader": wallet,
        "capital_usd": "1000",
        "leader_capital_usd": "100000",
        "max_trade_usd": "100",
        "min_trade_usd": "5",
        "min_size": "1",
        "ttl_s": 300,  # рынок предсказаний медленнее биржи, но не бесконечно
        "mode": "paper",
    }
    base.update({k: (str(v) if isinstance(v, Decimal) else v) for k, v in params.items()})
    return StrategyManifest(
        slug=slug_of(wallet),
        branch=Branch.PREDICTION,
        venue=VENUE,
        source_kind=SOURCE_KIND,
        source_ref=wallet,
        instruments=instruments or ["*"],
        params=base,
        can_backtest=False,
        stop=StopSpec(daily_pct=Decimal(5), max_dd_pct=Decimal(20)),
        description=description or f"Копия позиций кошелька {wallet} на Polymarket",
    )


class PmCopyStrategy(Strategy):
    """Событие — `pm_position` (payload из `feeds.polymarket.pm_position_payload`)."""

    def reset(self) -> None:
        self.positions: dict[str, Decimal] = {}

    @property
    def leader(self) -> str:
        return str(self.param("leader", ""))

    @property
    def scale(self) -> Decimal:
        leader_capital = _dec(self.param("leader_capital_usd"))
        if leader_capital <= 0:
            return Decimal(1)
        return _dec(self.param("capital_usd")) / leader_capital

    def target_size(self, leader_size: Decimal, price: Decimal) -> Decimal:
        target = leader_size * self.scale
        cap = _dec(self.param("max_trade_usd"))
        if cap > 0 and price > 0 and target * price > cap:
            target = cap / price
        return target

    def on_event(self, event: Event) -> list[Signal]:
        if event.kind != POSITION_EVENT:
            return []
        payload = event.payload
        if str(payload.get("wallet", "")).lower() != self.leader.lower():
            return []
        token = str(payload.get("token_id", ""))
        if not token:
            return []
        price = _dec(payload.get("price")) or _dec(payload.get("avg_price"))
        leader_size = _dec(payload.get("size"))
        held = self.positions.get(token, Decimal(0))
        target = self.target_size(leader_size, price) if leader_size > 0 else Decimal(0)
        delta = target - held
        if delta == 0:
            return []
        side = "buy" if delta > 0 else "sell"
        qty = abs(delta)
        if side == "buy" and not self._worth_trading(qty, price):
            return []
        reason = (
            "leader_exit"
            if target == 0
            else (
                "leader_entry"
                if held == 0
                else ("leader_increase" if delta > 0 else "leader_decrease")
            )
        )
        self.positions[token] = target
        if target == 0:
            self.positions.pop(token, None)
        meta = {
            "leader": self.leader,
            "venue": VENUE,
            "token_id": token,
            "condition_id": str(payload.get("condition_id", "")),
            "outcome": str(payload.get("outcome", "")),
            "title": str(payload.get("title", "")),
            "leader_size": _num(leader_size),
            "leader_avg_price": str(payload.get("avg_price", "")),
            "scale": _num(self.scale),
            "target_size": _num(target),
            "size_usd": _num(qty * price),
            "mode": str(self.param("mode", "paper")),
            "reason": reason,
        }
        return [
            self.event_signal(
                event,
                token,
                side,
                qty,
                price_ref=price,
                inputs={"leader_size": _num(leader_size), "price": _num(price)},
                meta=meta,
            )
        ]

    def _worth_trading(self, qty: Decimal, price: Decimal) -> bool:
        floor_usd = _dec(self.param("min_trade_usd"))
        min_size = _dec(self.param("min_size"))
        if qty < min_size:
            return False
        return not (floor_usd > 0 and qty * price < floor_usd)

    def note_fill(self, token: str, side: str, qty: Decimal) -> None:
        """Факт исполнения от worker: копия знает свой реальный размер, а не намерение."""
        signed = qty if side == "buy" else -qty
        size = self.positions.get(token, Decimal(0)) + signed
        if size <= 0:
            self.positions.pop(token, None)
        else:
            self.positions[token] = size


def make_pm_copy_strategy(
    wallet: str,
    *,
    instruments: list[str] | None = None,
    **params: Any,
) -> PmCopyStrategy:
    """`prediction-pm-copy-<кошелёк>`: копия позиций топ-кошелька Polymarket."""
    return PmCopyStrategy(pm_copy_manifest(wallet, params=params, instruments=instruments))


__all__ = [
    "POSITION_EVENT",
    "SOURCE_KIND",
    "VENUE",
    "PmCopyStrategy",
    "make_pm_copy_strategy",
    "pm_copy_manifest",
    "slug_of",
]
