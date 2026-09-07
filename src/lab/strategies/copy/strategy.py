"""Стратегия `copy-<chain>-<addr>`: повтор сделок лидера своим исполнителем (Истории 60, 62).

Что здесь важно и почему:
- размер копии — доля от капитала, а не абсолют лидера: у лидера 100k, у нас 1k;
- каждая копия ссылается на сделку лидера (`leader_tx`) и несёт лаг в миллисекундах —
  без этого «копия vs лидер» в замере не посчитать;
- «висящая» позиция (лидер вышел, мы не успели) закрывается правилом из манифеста:
  по времени или по отходу цены от цены выхода лидера.
Исполнение — чужая забота: стратегия отдаёт сигналы, исполнителя выбирает `execution`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lab.contracts import Branch, Event, Signal, StopSpec, StrategyManifest
from lab.strategies.base import Strategy

WALLET_TRADE_EVENT = "wallet_trade"
DEFAULT_HANGING = {"timeout_s": 900, "drop_pct": "10"}


def _num(value: Decimal) -> str:
    """Число для `meta`: без хвостовых нулей и без экспоненты."""
    return format(value.normalize(), "f")


def _dec(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value is not None else default))


def slug_of(address: str) -> str:
    keep = [c if (c.isalnum() or c == "-") else "-" for c in address.lower()]
    return "".join(keep).strip("-")


@dataclass
class CopyPosition:
    token: str
    qty: Decimal = Decimal(0)
    entry_price: Decimal = Decimal(0)
    opened_at: datetime | None = None
    leader_tx: str = ""
    exit_requested_at: datetime | None = None
    exit_price: Decimal | None = None
    history: list[str] = field(default_factory=list)


class CopyStrategy(Strategy):
    """Копия сделок одного лидера. Событие — `wallet_trade` (payload из `feeds.chains`)."""

    def __init__(
        self,
        manifest: StrategyManifest,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))
        super().__init__(manifest)

    def reset(self) -> None:
        self.positions: dict[str, CopyPosition] = {}

    # -- параметры -------------------------------------------------------------------

    @property
    def leader(self) -> str:
        return str(self.param("leader", ""))

    @property
    def scale(self) -> Decimal:
        """Доля нашего капитала от капитала лидера — она и есть масштаб копии."""
        leader_capital = _dec(self.param("leader_capital_usd"), "0")
        if leader_capital <= 0:
            return Decimal(1)
        return _dec(self.param("capital_usd")) / leader_capital

    # -- события лидера --------------------------------------------------------------

    def on_event(self, event: Event) -> list[Signal]:
        if event.kind != WALLET_TRADE_EVENT:
            return []
        payload = event.payload
        if str(payload.get("address", "")).lower() != self.leader.lower():
            return []
        token = str(payload.get("token", ""))
        price = _dec(payload.get("price"))
        side = str(payload.get("side", ""))
        if side == "buy":
            return self._copy_entry(event, token, price, payload)
        return self._copy_exit(event, token, price, payload)

    def _copy_entry(self, event, token: str, price: Decimal, payload: dict) -> list[Signal]:
        leader_usd = payload.get("value_usd") or payload.get("quote_qty")
        size_usd = _dec(leader_usd) * self.scale
        cap = _dec(self.param("max_trade_usd"), "0")
        if cap > 0:
            size_usd = min(size_usd, cap)
        floor = _dec(self.param("min_trade_usd"), "0")
        if size_usd <= 0 or (floor > 0 and size_usd < floor):
            return []
        qty = size_usd / price if price > 0 else Decimal(0)
        if qty <= 0:
            return []
        return [
            self._signal(
                event,
                token,
                "buy",
                qty,
                price,
                payload,
                {"size_usd": _num(size_usd), "reason": "leader_entry"},
            )
        ]

    def _copy_exit(self, event, token: str, price: Decimal, payload: dict) -> list[Signal]:
        position = self.positions.get(token)
        if position is None or position.qty <= 0:
            return []
        ts = _ts(payload.get("ts")) or self.clock()
        position.exit_requested_at = ts
        position.exit_price = price
        return [
            self._signal(
                event,
                token,
                "sell",
                position.qty,
                price,
                payload,
                {"size_usd": _num(position.qty * price), "reason": "leader_exit"},
            )
        ]

    def _signal(self, event, token, side, qty, price, payload, extra) -> Signal:
        leader_ts = _ts(payload.get("ts")) or event.ts
        lag_ms = int((self.clock() - leader_ts).total_seconds() * 1000)
        meta = {
            "leader": self.leader,
            "chain": str(payload.get("chain", self.param("chain", ""))),
            "leader_tx": str(payload.get("tx", "")),
            "leader_ts": leader_ts.isoformat(),
            "leader_price": _num(price) if price else "0",
            "leader_qty": str(payload.get("qty", "")),
            "lag_ms": lag_ms,
            "scale": _num(self.scale),
            "venue": self.manifest.venue,
            "mode": self.param("mode", "paper"),
            "symbol": str(payload.get("symbol", "")),
        }
        meta.update(extra)
        return self.event_signal(
            event,
            token,
            side,
            qty,
            price_ref=price,
            inputs={"leader_tx": meta["leader_tx"], "leader": self.leader},
            meta=meta,
        )

    # -- обратная связь исполнения ------------------------------------------------------

    def note_fill(self, token: str, side: str, qty: Decimal, price: Decimal, ts: datetime) -> None:
        """Копия исполнилась: позиция появилась или закрылась (зовёт worker после филла)."""
        position = self.positions.setdefault(token, CopyPosition(token=token))
        if side == "buy":
            total = position.qty + qty
            position.entry_price = (
                (position.entry_price * position.qty + price * qty) / total if total else price
            )
            position.qty = total
            position.opened_at = position.opened_at or ts
        else:
            position.qty = max(Decimal(0), position.qty - qty)
            if position.qty == 0:
                position.exit_requested_at = None
                position.exit_price = None

    # -- «висящая» позиция ---------------------------------------------------------------

    def hanging(
        self, now: datetime, prices: dict[str, Decimal] | None = None
    ) -> list[Signal]:
        """Лидер вышел, а копия осталась: закрываем по правилу манифеста (История 62)."""
        rule = dict(DEFAULT_HANGING) | dict(self.param("hanging", {}) or {})
        timeout_s = int(rule.get("timeout_s", 900))
        drop_pct = _dec(rule.get("drop_pct"), "0")
        out: list[Signal] = []
        for token, position in self.positions.items():
            if position.qty <= 0 or position.exit_requested_at is None:
                continue
            waited = (now - position.exit_requested_at).total_seconds()
            price = (prices or {}).get(token)
            reason = ""
            if waited >= timeout_s:
                reason = "hanging_timeout"
            elif price is not None and position.exit_price and drop_pct > 0:
                drift = (position.exit_price - price) / position.exit_price * 100
                if drift >= drop_pct:
                    reason = "hanging_price"
            if not reason:
                continue
            event = Event(
                kind="hanging",
                ts=now,
                payload={"token": token, "waited_s": waited, "reason": reason},
            )
            out.append(
                self.event_signal(
                    event,
                    token,
                    "sell",
                    position.qty,
                    price_ref=price or position.exit_price,
                    inputs={"token": token, "reason": reason},
                    meta={
                        "leader": self.leader,
                        "reason": reason,
                        "waited_s": int(waited),
                        "leader_tx": position.leader_tx,
                        "venue": self.manifest.venue,
                    },
                )
            )
        return out


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return None


def copy_manifest(
    *,
    source_kind: str,
    slug: str,
    venue: str,
    leader: str,
    chain: str,
    instruments: list[str],
    params: dict[str, Any],
    description: str,
) -> StrategyManifest:
    base = {
        "leader": leader,
        "chain": chain,
        "capital_usd": "1000",
        "leader_capital_usd": "100000",
        "max_trade_usd": "100",
        "min_trade_usd": "5",
        "hanging": dict(DEFAULT_HANGING),
        "ttl_s": 120,  # копия, опоздавшая на две минуты, — уже не копия
        "mode": "paper",
    }
    base.update({k: (str(v) if isinstance(v, Decimal) else v) for k, v in params.items()})
    return StrategyManifest(
        slug=slug,
        branch=Branch.COPY if hasattr(Branch, "COPY") else "copy",
        venue=venue,
        source_kind=source_kind,
        source_ref=leader,
        instruments=instruments,
        params=base,
        can_backtest=False,
        stop=StopSpec(daily_pct=Decimal(5), max_dd_pct=Decimal(20)),
        description=description,
    )


def make_copy_strategy(
    chain: str,
    address: str,
    *,
    venue: str | None = None,
    instruments: list[str] | None = None,
    clock: Callable[[], datetime] | None = None,
    **params: Any,
) -> CopyStrategy:
    """`copy-<chain>-<addr>`: копия кошелька в сети. Без DEX-исполнителя работает в `paper`."""
    source_kind = {"hyperliquid_user": "hyperliquid"}.get(chain, chain).replace("_", "-")
    manifest = copy_manifest(
        source_kind=source_kind,
        slug=slug_of(address),
        venue=venue or chain,
        leader=address,
        chain=chain,
        instruments=instruments or ["*"],
        params=params,
        description=f"Копия кошелька {address} в сети {chain}",
    )
    return CopyStrategy(manifest, clock=clock)


__all__ = [
    "DEFAULT_HANGING",
    "WALLET_TRADE_EVENT",
    "CopyPosition",
    "CopyStrategy",
    "copy_manifest",
    "make_copy_strategy",
    "slug_of",
]
