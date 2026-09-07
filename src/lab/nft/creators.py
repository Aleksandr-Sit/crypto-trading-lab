"""Рейтинг создателей NFT по их прошлым коллекциям (История 73, R23).

Мера одна: что стало с флором через 1/7/30 дней после минта относительно цены минта.
Свежие коллекции весят больше старых (период полураспада из конфига) — автор, у которого
всё получалось три года назад, и автор, у которого получилось в прошлом месяце, не равны.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from lab.feeds.nft.config import CreatorConfig, NftConfig, load_nft
from lab.feeds.nft.types import CollectionHistory

ZERO = Decimal(0)
ONE = Decimal(1)
GOOD_RATIO = Decimal(3)  # флор втрое выше минта — потолок шкалы качества


def median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _clamp01(value: Decimal) -> Decimal:
    return ZERO if value < ZERO else (ONE if value > ONE else value)


@dataclass(frozen=True)
class CreatorScore:
    """Карточка создателя: сколько минтов, что с флором, доля успешных, итоговый балл."""

    creator: str
    collections: int
    successful: int
    success_rate_pct: Decimal
    weighted_success_rate_pct: Decimal
    median_ratio: dict[int, Decimal | None] = field(default_factory=dict)
    score: Decimal = ZERO
    detail: str = ""
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def enough_history(self) -> bool:
        return self.collections > 0

    def ratio(self, days: int) -> Decimal | None:
        return self.median_ratio.get(days)


def _recency_weight(history: CollectionHistory, now: datetime, half_life_days: int) -> Decimal:
    if half_life_days <= 0:
        return ONE
    minted = history.minted_at
    if minted.tzinfo is None:
        minted = minted.replace(tzinfo=UTC)
    age_days = max(Decimal(0), Decimal((now - minted).total_seconds()) / Decimal(86400))
    return Decimal(2) ** (-(age_days / Decimal(half_life_days)))


def creator_score(
    creator: str,
    histories: list[CollectionHistory],
    *,
    config: NftConfig | CreatorConfig | None = None,
    now: datetime | None = None,
) -> CreatorScore:
    """Балл создателя в [0, 1] по его прошлым коллекциям.

    `score` = 0.5 × доля успешных (с весом свежести) + 0.5 × медианный флор/минт,
    срезанный на трёхкратном росте. Без истории — ноль и явная причина в `detail`.
    """
    cfg = config.creator if isinstance(config, NftConfig) else (config or load_nft().creator)
    at = now or datetime.now(UTC)
    mine = [h for h in histories if h.creator == creator]
    if not mine:
        return CreatorScore(
            creator=creator,
            collections=0,
            successful=0,
            success_rate_pct=ZERO,
            weighted_success_rate_pct=ZERO,
            detail="истории коллекций нет — балл не назначается",
            computed_at=at,
        )

    ratios: dict[int, list[Decimal]] = {d: [] for d in cfg.windows_days}
    for history in mine:
        for days in cfg.windows_days:
            value = history.ratio_at(days)
            if value is not None:
                ratios[days].append(value)

    window = cfg.success_window_days
    successful = 0
    weight_total = ZERO
    weight_success = ZERO
    for history in mine:
        value = history.ratio_at(window)
        if value is None:  # окна нет — берём ближайшее известное
            value = next(
                (history.ratio_at(d) for d in sorted(cfg.windows_days) if history.ratio_at(d)),
                None,
            )
        weight = _recency_weight(history, at, cfg.half_life_days)
        weight_total += weight
        if value is not None and value >= cfg.success_floor_ratio:
            successful += 1
            weight_success += weight

    n = len(mine)
    rate = Decimal(successful) / Decimal(n) * Decimal(100)
    weighted = (weight_success / weight_total * Decimal(100)) if weight_total > ZERO else ZERO
    medians = {d: median(ratios[d]) for d in cfg.windows_days}
    quality_source = medians.get(window) or next(
        (medians[d] for d in sorted(cfg.windows_days) if medians.get(d)), None
    )
    quality = _clamp01((quality_source or ZERO) / GOOD_RATIO)
    score = _clamp01(weighted / Decimal(200) + quality / Decimal(2))
    detail = f"{n} коллекций, успешных {successful}"
    if n < cfg.min_collections:
        detail += " — истории мало, балл предварительный"
    return CreatorScore(
        creator=creator,
        collections=n,
        successful=successful,
        success_rate_pct=rate,
        weighted_success_rate_pct=weighted,
        median_ratio=medians,
        score=score,
        detail=detail,
        computed_at=at,
    )


def rank_creators(
    histories: list[CollectionHistory],
    *,
    config: NftConfig | None = None,
    now: datetime | None = None,
) -> list[CreatorScore]:
    """Рейтинг: все создатели из истории, по убыванию балла."""
    creators = sorted({h.creator for h in histories if h.creator})
    scores = [creator_score(c, histories, config=config, now=now) for c in creators]
    return sorted(scores, key=lambda s: (s.score, s.collections), reverse=True)
