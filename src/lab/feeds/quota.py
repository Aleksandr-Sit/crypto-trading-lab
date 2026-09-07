"""Квоты источников: протокол `FeedsRegistry.use(feed_id, n)` и счётчик в памяти.

Настоящая реализация (`ops.feeds_registry`: квоты, здоровье, бюджет) — таск 14. До неё
фиды и исполнители принимают любой объект с методом `use`: по умолчанию `NullQuota`
(ничего не считает), в тестах и офлайн-режиме — `MemoryFeedsRegistry` (считает вызовы
и вес по feed_id). Каждый сетевой вызов площадки проходит через `use()` до отправки.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol, runtime_checkable


@runtime_checkable
class FeedsRegistry(Protocol):
    """Интерфейс из тикета 01 (`ops.feeds_registry.use(feed_id, n)`)."""

    def use(self, feed_id: str, n: int = 1) -> None: ...


class NullQuota:
    """Заглушка: ничего не считает."""

    def use(self, feed_id: str, n: int = 1) -> None:
        return None


class MemoryFeedsRegistry:
    """Счётчик в памяти: `used[feed_id]` — суммарный вес, `calls[feed_id]` — число вызовов."""

    def __init__(self) -> None:
        self.used: dict[str, int] = defaultdict(int)
        self.calls: dict[str, int] = defaultdict(int)

    def use(self, feed_id: str, n: int = 1) -> None:
        self.used[feed_id] += n
        self.calls[feed_id] += 1


# совместимые имена
QuotaSink = FeedsRegistry
CountingQuota = MemoryFeedsRegistry

__all__ = ["CountingQuota", "FeedsRegistry", "MemoryFeedsRegistry", "NullQuota", "QuotaSink"]
