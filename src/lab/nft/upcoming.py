"""Лента предстоящих минтов с оценкой (Истории 74, 74a).

Источников минимум два: launchpad Magic Eden и хотя бы один парсер календаря. Один и тот
же минт приходит из разных мест — карточка склеивается по (коллекция, сеть), а источники
остаются перечисленными: по ним видно, откуда пришла оценка и чему верить.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from lab.contracts import NftMint
from lab.feeds.nft.config import NftConfig, load_nft
from lab.feeds.nft.types import CollectionHistory, LaunchpadSlot
from lab.nft.attention import (
    AttentionComponents,
    AttentionIndex,
    allowlist_demand,
    attention_index,
    mentions_growth,
)
from lab.nft.creators import CreatorScore, creator_score


class MentionsSource(Protocol):
    """Упоминания коллекции: (было за прошлое окно, стало за текущее)."""

    def mentions(self, collection: str, *, window_h: int) -> tuple[int, int]: ...


@dataclass(frozen=True)
class MintCandidate:
    """Строка ленты: минт, индекс внимания, создатель, источники."""

    mint: NftMint
    attention: AttentionIndex
    creator: CreatorScore | None = None
    slot: LaunchpadSlot | None = None
    sources: tuple[str, ...] = ()

    @property
    def collection(self) -> str:
        return self.mint.collection

    @property
    def starts_at(self) -> datetime | None:
        return self.mint.starts_at

    @property
    def score(self) -> Decimal:
        return self.attention.value

    def card(self) -> dict:
        """Payload карточки минта — значение индекса и пометка про гипотезу видны сразу."""
        return {
            "collection": self.collection,
            "chain": self.mint.chain,
            "market": self.mint.market,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "price": None if self.mint.price is None else str(self.mint.price),
            "supply": self.mint.supply,
            "creator": self.mint.creator,
            "creator_score": None if self.creator is None else str(self.creator.score),
            "attention": str(self.attention.value),
            "attention_components": {k: str(v) for k, v in self.attention.components.items()},
            "attention_note": self.attention.note,
            "sources": list(self.sources),
        }


def _key(mint: NftMint) -> tuple[str, str]:
    return (mint.collection.strip().lower(), mint.chain.strip().lower())


def _merge(base: NftMint, other: NftMint) -> NftMint:
    """Пустое поле дополняется из второго источника, заполненное не перетирается."""
    update = {}
    for name in ("starts_at", "price", "supply", "creator"):
        if getattr(base, name) is None and getattr(other, name) is not None:
            update[name] = getattr(other, name)
    meta = dict(other.meta)
    meta.update(base.meta)
    update["meta"] = meta
    return base.model_copy(update=update)


class UpcomingFeed:
    """Собирает ленту из календарей, считает индекс внимания и балл создателя."""

    def __init__(
        self,
        calendars: Sequence,
        *,
        histories: Sequence[CollectionHistory] = (),
        mentions: MentionsSource | None = None,
        slots: dict[str, LaunchpadSlot] | None = None,
        config: NftConfig | None = None,
        clock=None,
    ) -> None:
        self.calendars = list(calendars)
        self.histories = list(histories)
        self.mentions = mentions
        self.slots = slots or {}
        self.config = config or load_nft()
        self._clock = clock or (lambda: datetime.now(UTC))

    def _components(self, mint: NftMint, creator: CreatorScore | None) -> AttentionComponents:
        slot = self.slots.get(mint.collection)
        growth = Decimal(0)
        if self.mentions is not None:
            try:
                before, after = self.mentions.mentions(
                    mint.collection, window_h=self.config.attention.mentions_window_h
                )
                growth = mentions_growth(before, after)
            except Exception:  # noqa: BLE001 — соцсети недоступны, индекс считается без них
                growth = Decimal(0)
        demand = Decimal(0)
        fill = Decimal(0)
        if slot is not None:
            demand = allowlist_demand(slot.allowlist_seats, slot.allowlist_demand)
            fill = slot.fill
        return AttentionComponents(
            mentions_growth=growth,
            allowlist_demand=demand,
            launchpad_fill=fill,
            creator_score=creator.score if creator else Decimal(0),
        )

    def collect(self) -> list[NftMint]:
        """Сырьё ленты: всё, что отдали источники, склеенное по (коллекция, сеть)."""
        merged: dict[tuple[str, str], NftMint] = {}
        sources: dict[tuple[str, str], list[str]] = {}
        for calendar in self.calendars:
            try:
                mints = list(calendar.mints())
            except Exception:  # noqa: BLE001 — источник лёг, лента остаётся из остальных
                continue
            for mint in mints:
                key = _key(mint)
                sources.setdefault(key, []).append(getattr(calendar, "id", mint.market))
                merged[key] = _merge(merged[key], mint) if key in merged else mint
        self._sources = {k: tuple(dict.fromkeys(v)) for k, v in sources.items()}
        return list(merged.values())

    def upcoming(
        self, *, now: datetime | None = None, min_score: Decimal | None = None
    ) -> list[MintCandidate]:
        at = now or self._clock()
        floor_score = self.config.attention.min_score_to_watch if min_score is None else min_score
        self._sources: dict[tuple[str, str], tuple[str, ...]] = {}
        out: list[MintCandidate] = []
        for mint in self.collect():
            if mint.starts_at is not None and mint.starts_at < at:
                continue  # минт уже прошёл — это не «предстоящий»
            creator = (
                creator_score(mint.creator, self.histories, config=self.config, now=at)
                if mint.creator
                else None
            )
            index = attention_index(self._components(mint, creator), config=self.config)
            if index.value < floor_score:
                continue
            out.append(
                MintCandidate(
                    mint=mint,
                    attention=index,
                    creator=creator,
                    slot=self.slots.get(mint.collection),
                    sources=self._sources.get(_key(mint), (mint.market,)),
                )
            )
        return sorted(
            out,
            key=lambda c: (c.score, c.starts_at or datetime.max.replace(tzinfo=UTC)),
            reverse=True,
        )
