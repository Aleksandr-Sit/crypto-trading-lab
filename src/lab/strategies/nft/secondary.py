"""Ранняя вторичка: покупка по флору в первые минуты листинга (Истории 75, 75a, R24).

Из брифа: «нужно выявить интересные коллекции и опередить с покупкой, дальше часть будет
продаваться на одних значениях, часть будем оставлять для большего роста». Отсюда два
свойства правила: решение измеряется в миллисекундах (`decision_latency_ms` ≤ 2 с — иначе
подход надо менять, а не оправдывать), а выход разделён — цели плюс остаток на удержание.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from lab.contracts import Branch, Event, Signal, StopSpec, StrategyManifest
from lab.feeds.nft import instrument_of, load_nft
from lab.nft import NftPosition, illiquid_flag, sell_plan
from lab.strategies.base import Strategy

NFT_LISTING_EVENT = "nft_listing"
NFT_FLOOR_EVENT = "nft_floor"
NFT_MINT_OPEN_EVENT = "nft_mint_open"
ENTRY_EVENTS = (NFT_LISTING_EVENT, NFT_FLOOR_EVENT)
DEFAULT_TTL_S = 60


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — площадка может прислать мусор
        return Decimal(default)


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _num(value: Decimal) -> str:
    return format(value.normalize(), "f")


def nft_stop() -> StopSpec:
    return StopSpec(daily_pct=Decimal(10), max_dd_pct=Decimal(30), max_position_pct=Decimal(5))


def nft_manifest(
    *,
    slug: str,
    source_kind: str,
    venue: str,
    instruments: list[str] | None = None,
    description: str = "",
    **params: Any,
) -> StrategyManifest:
    config = load_nft()
    defaults: dict[str, Any] = {
        "capital_usd": "1000",
        "position_pct": "10",
        "max_trade_usd": "200",
        "ttl_s": DEFAULT_TTL_S,
        "window_min": config.secondary.window_min,
        "max_floor_premium_pct": str(config.secondary.max_floor_premium_pct),
        "min_volume_usd": str(config.secondary.min_volume_usd),
        "min_sales_per_min": str(config.secondary.min_sales_per_min),
        "min_attention": str(config.secondary.min_attention),
        "decision_budget_ms": config.secondary.decision_budget_ms,
        "hold_pct": str(config.ladder.hold_pct),
        "mode": "paper",
    }
    defaults.update({k: v for k, v in params.items() if v is not None})
    return StrategyManifest(
        slug=slug,
        branch=Branch.NFT if hasattr(Branch, "NFT") else "nft",
        venue=venue,
        source_kind=source_kind,
        instruments=instruments or ["*"],
        params=defaults,
        can_backtest=False,  # ранняя стадия меряется только форвардом (История 69, 76)
        stop=nft_stop(),
        description=description,
    )


class NftSecondaryStrategy(Strategy):
    """Покупка листинга у флора в первые минуты, выход лестницей с удержанием остатка."""

    entry_reason = "secondary_entry"

    def __init__(self, manifest: StrategyManifest) -> None:
        self.config = load_nft()
        super().__init__(manifest)

    def reset(self) -> None:
        self.positions: dict[str, NftPosition] = {}
        self.flags: dict[str, Any] = {}

    # -- вход ------------------------------------------------------------------------

    def collection(self) -> str:
        return str(self.param("collection", "") or "")

    def _wants(self, payload: dict, now: datetime) -> tuple[bool, str]:
        collection = str(payload.get("collection", ""))
        mine = self.collection()
        if mine and collection != mine:
            return False, "другая коллекция"
        price = _dec(payload.get("price"))
        floor = _dec(payload.get("floor"), str(price))
        if price <= 0:
            return False, "нет цены листинга"
        premium = ((price - floor) / floor * 100) if floor > 0 else Decimal(0)
        if premium > _dec(self.param("max_floor_premium_pct"), "5"):
            return False, "дороже флора"
        listed_at = _ts(payload.get("listed_at"))
        window = int(self.param("window_min", 15) or 15)
        if listed_at is not None and now - listed_at > timedelta(minutes=window):
            return False, "листинг не свежий"
        if _dec(payload.get("volume_usd")) < _dec(self.param("min_volume_usd"), "0"):
            return False, "объём ниже порога"
        if _dec(payload.get("sales_per_min")) < _dec(self.param("min_sales_per_min"), "0"):
            return False, "скорость продаж ниже порога"
        if _dec(payload.get("attention")) < _dec(self.param("min_attention"), "0"):
            return False, "индекс внимания ниже порога"
        return True, ""

    def on_event(self, event: Event) -> list[Signal]:
        if event.kind not in ENTRY_EVENTS:
            return []
        started = time.perf_counter()
        payload = event.payload
        ok, reason = self._wants(payload, event.ts)
        if not ok:
            self.flags[str(payload.get("collection", ""))] = reason
            return []
        price = _dec(payload.get("price"))
        size_usd = self.size_usd()
        if size_usd <= 0:
            return []
        qty = payload.get("qty")
        quantity = _dec(qty, "1") if qty else Decimal(1)
        instrument = instrument_of(
            str(payload.get("collection", "")), str(payload.get("token_id", "")) or None
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        budget = int(self.param("decision_budget_ms", 2000) or 2000)
        return [
            self.event_signal(
                event,
                instrument,
                "buy",
                quantity,
                price_ref=price,
                inputs={
                    "floor": str(payload.get("floor", "")),
                    "volume_usd": str(payload.get("volume_usd", "")),
                    "sales_per_min": str(payload.get("sales_per_min", "")),
                    "attention": str(payload.get("attention", "")),
                },
                meta={
                    "reason": self.entry_reason,
                    "collection": str(payload.get("collection", "")),
                    "chain": str(payload.get("chain", "")),
                    "venue": str(payload.get("market", "")) or self.manifest.venue,
                    "token_id": str(payload.get("token_id", "")),
                    "floor": str(payload.get("floor", "")),
                    "price": _num(price),
                    "size_usd": _num(size_usd),
                    "attention": str(payload.get("attention", "")),
                    # История 75a: латентность решения — часть замера, не украшение
                    "decision_latency_ms": str(latency_ms),
                    "decision_budget_ms": str(budget),
                    "latency_ok": str(latency_ms <= budget).lower(),
                },
            )
        ]

    def size_usd(self) -> Decimal:
        capital = _dec(self.param("capital_usd"), "1000")
        share = _dec(self.param("position_pct"), "10")
        size = capital * share / 100
        cap = _dec(self.param("max_trade_usd"), "0")
        return min(size, cap) if cap > 0 else size

    def ttl_s(self) -> int:
        return int(self.param("ttl_s", DEFAULT_TTL_S) or DEFAULT_TTL_S)

    # -- позиция и выход ---------------------------------------------------------------

    def position(self, instrument: str) -> NftPosition | None:
        return self.positions.get(instrument)

    def note_fill(
        self, instrument: str, side: str, qty: Decimal, price: Decimal, ts: datetime
    ) -> None:
        """Факт исполнения от worker — без него лестница не знает, чем управляет."""
        position = self.positions.get(instrument)
        collection, _, token_id = instrument.partition(":")
        if side == "buy":
            if position is None:
                self.positions[instrument] = NftPosition(
                    collection=collection,
                    token_id=token_id or None,
                    qty=qty,
                    entry_price=price,
                    opened_at=ts,
                    market=self.manifest.venue,
                    strategy_id=self.strategy_id,
                )
                return
            total = position.qty + qty
            position.entry_price = (
                position.entry_price * position.qty + price * qty
            ) / total
            position.qty = total
            return
        if position is not None:
            position.sold_qty += qty

    def on_price(self, instrument: str, price: Decimal, ts: datetime) -> list[Signal]:
        """Цели лестницы: часть продаём, `hold_pct` остаётся (История 78, G06)."""
        position = self.positions.get(instrument)
        if position is None:
            return []
        orders = sell_plan(position, price, config=self.config)
        event = Event(kind="nft_price", ts=ts, payload={"instrument": instrument})
        out: list[Signal] = []
        for order in orders:
            out.append(
                self.event_signal(
                    event,
                    instrument,
                    "sell",
                    order.qty,
                    price_ref=price,
                    inputs={"gain_pct": str(order.gain_pct)},
                    meta={
                        "reason": f"ladder_{order.kind}",
                        "detail": order.reason,
                        "gain_pct": _num(order.gain_pct),
                        "hold_pct": str(self.config.ladder.hold_pct),
                        "venue": self.manifest.venue,
                    },
                )
            )
        return out

    def illiquid(
        self, *, now: datetime | None = None, last_sale_at: datetime | None = None
    ) -> list:
        """Позиции без покупателей: флаг с возрастом и предложенной ценой (История 80)."""
        at = now or datetime.now(UTC)
        flags = []
        for position in self.positions.values():
            flag = illiquid_flag(position, now=at, last_sale_at=last_sale_at, config=self.config)
            if flag is not None:
                flags.append(flag)
        return flags


def make_secondary_strategy(
    collection: str = "",
    *,
    market: str = "magiceden",
    slug: str | None = None,
    **params: Any,
) -> NftSecondaryStrategy:
    """`nft-secondary-<коллекция>-v1` — правило ранней вторички по одной коллекции."""
    name = slug or (f"{collection}-v1" if collection else "any-v1")
    return NftSecondaryStrategy(
        nft_manifest(
            slug=name,
            source_kind="secondary",
            venue=market,
            instruments=[collection or "*"],
            description="покупка у флора в первые минуты листинга, выход лестницей с удержанием",
            collection=collection,
            **params,
        )
    )
