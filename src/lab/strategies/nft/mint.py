"""Минт-снайпинг вариантами — равноправная гипотеза, а не отвергнутая (Истории 76, 77).

Пользователь: «какое-то рандомное количество можно получить на минте, без гарантий да, но
можно, и ты этого не проверял и не замерял. Будем тестировать и подбирать подход». Поэтому
здесь не одна «правильная» тактика, а четыре варианта — приоритетная fee, несколько
кошельков, ранняя отправка, allowlist-место — каждый отдельной строкой замера
`nft-mint-<вариант>-v1` со своими `mint_attempts / mint_success_rate / mint_cost_failed`.

Что из этого работает, покажет замер. Ни один вариант не выключен заранее.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lab.contracts import Event, MintAttemptSpec, Signal, StrategyManifest
from lab.feeds.nft import load_nft
from lab.strategies.base import Strategy
from lab.strategies.nft.secondary import (
    NFT_MINT_OPEN_EVENT,
    _dec,
    _num,
    _ts,
    nft_manifest,
)

MINT_EVENTS = (NFT_MINT_OPEN_EVENT,)


def mint_variants() -> dict[str, Any]:
    return {v.slug: v for v in load_nft().mint.variants}


MINT_VARIANTS: tuple[str, ...] = tuple(mint_variants())


class NftMintStrategy(Strategy):
    """Один вариант подхода к минту. Решение пишется в журнал до исхода (решение §10)."""

    entry_reason = "mint"

    def __init__(self, manifest: StrategyManifest) -> None:
        self.config = load_nft()
        super().__init__(manifest)

    def reset(self) -> None:
        self.attempted: set[str] = set()

    @property
    def variant(self) -> str:
        return str(self.param("variant", "") or "")

    def _wants(self, payload: dict) -> tuple[bool, str]:
        collection = str(payload.get("collection", ""))
        if not collection:
            return False, "нет коллекции"
        if collection in self.attempted:
            return False, "уже пробовали"
        if self.param("allowlist_only"):
            seat = str(payload.get("allowlist_seat", "")).lower() in ("true", "1", "yes")
            if not seat:
                return False, "места в allowlist нет"
        price = _dec(payload.get("price"))
        cap = _dec(self.param("max_price_usd"), "0")
        if cap > 0 and price * int(self.param("qty", 1) or 1) > cap:
            return False, "цена минта выше потолка"
        if _dec(payload.get("attention")) < _dec(self.param("min_attention"), "0"):
            return False, "индекс внимания ниже порога"
        return True, ""

    def on_event(self, event: Event) -> list[Signal]:
        if event.kind not in MINT_EVENTS:
            return []
        started = time.perf_counter()
        payload = event.payload
        ok, reason = self._wants(payload)
        if not ok:
            return []
        collection = str(payload.get("collection", ""))
        self.attempted.add(collection)
        price = _dec(payload.get("price"))
        qty = int(self.param("qty", 1) or 1)
        latency_ms = int((time.perf_counter() - started) * 1000)
        starts_at = _ts(payload.get("starts_at"))
        return [
            self.event_signal(
                event,
                collection,
                "buy",
                Decimal(qty),
                price_ref=price,
                inputs={
                    "supply": str(payload.get("supply", "")),
                    "attention": str(payload.get("attention", "")),
                    "starts_at": starts_at.isoformat() if starts_at else "",
                },
                meta={
                    "reason": self.entry_reason,
                    "variant": self.variant,
                    "collection": collection,
                    "chain": str(payload.get("chain", "")),
                    "venue": str(payload.get("market", "")) or self.manifest.venue,
                    "price": _num(price),
                    "qty": str(qty),
                    "wallets": str(self.param("wallets", 1)),
                    "priority_fee_usd": str(self.param("priority_fee_usd", "0")),
                    "send_offset_s": str(self.param("send_offset_s", 0)),
                    "allowlist_seat": str(payload.get("allowlist_seat", "")),
                    "decision_latency_ms": str(latency_ms),
                    "decision_budget_ms": str(self.param("decision_budget_ms", 2000)),
                },
            )
        ]

    def mint_spec(self, event: Event, *, mode: str | None = None) -> MintAttemptSpec | None:
        """Заявка на минт для `executors.nft.MintExecutor.mint(...)`."""
        payload = event.payload
        collection = str(payload.get("collection", ""))
        if not collection:
            return None
        price = _dec(payload.get("price"))
        return MintAttemptSpec(
            collection=collection,
            chain=str(payload.get("chain", "")),
            market=str(payload.get("market", "")) or self.manifest.venue,
            qty=int(self.param("qty", 1) or 1),
            max_price=price if price > 0 else _dec(self.param("max_price_usd"), "1"),
            priority_fee=_dec(self.param("priority_fee_usd"), "0"),
            mode=mode or str(self.param("mode", "paper")),
        )

    def wallets(self) -> list[str]:
        """Кошельки варианта: имена берёт worker, здесь — только их количество."""
        n = int(self.param("wallets", 1) or 1)
        return [f"wallet-{i + 1}" for i in range(max(1, n))]

    def ttl_s(self) -> int:
        return int(self.param("ttl_s", 60) or 60)


def make_mint_strategy(
    variant: str = "priority-fee",
    *,
    market: str = "magiceden",
    chain: str = "solana",
    version: str = "v1",
    **params: Any,
) -> NftMintStrategy:
    """`nft-mint-<вариант>-<версия>` — отдельная строка замера на каждый подход."""
    config = load_nft()
    spec = config.variant(variant)
    defaults: dict[str, Any] = {
        "variant": variant,
        "chain": chain,
        "qty": config.mint.qty,
        "max_price_usd": str(config.mint.max_price_usd),
        "priority_fee_usd": str(spec.priority_fee_usd),
        "wallets": spec.wallets,
        "send_offset_s": spec.send_offset_s,
        "allowlist_only": spec.allowlist_only,
        "min_attention": str(config.attention.min_score_to_watch),
        "max_attempts": config.mint.max_attempts,
        "ttl_s": 60,
    }
    defaults.update({k: v for k, v in params.items() if v is not None})
    return NftMintStrategy(
        nft_manifest(
            slug=f"{variant}-{version}",
            source_kind="mint",
            venue=market,
            description=f"минт-снайпинг, вариант «{spec.note or variant}» — гипотеза на замер",
            **defaults,
        )
    )


def mint_strategies(*, market: str = "magiceden") -> list[NftMintStrategy]:
    """Все варианты минта из конфига — ни один не выключен заранее (G09.1)."""
    return [make_mint_strategy(slug, market=market) for slug in mint_variants()]


def latency_metrics(signals_or_meta) -> dict[str, Any]:
    """`decision_latency_ms` из журнала решений — медиана и доля уложившихся в бюджет."""
    values: list[int] = []
    budget = 2000
    for item in signals_or_meta:
        meta = item.meta if hasattr(item, "meta") else item
        raw = meta.get("decision_latency_ms")
        if raw is None:
            continue
        values.append(int(raw))
        budget = int(meta.get("decision_budget_ms", budget))
    if not values:
        return {"decision_latency_ms": None, "decision_in_budget_pct": None, "n": 0}
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = (
        ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    )
    in_budget = sum(1 for v in values if v <= budget)
    return {
        "decision_latency_ms": median,
        "decision_in_budget_pct": Decimal(in_budget) / Decimal(len(values)) * 100,
        "n": len(values),
        "budget_ms": budget,
    }


def now_utc() -> datetime:
    return datetime.now(UTC)
