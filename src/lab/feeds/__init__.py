"""Фиды площадок и сетей (Границы: `feeds.*`).

Квоты: каждый сетевой вызов фида проходит через `FeedsRegistry.use(feed_id, n)`
(`lab.feeds.quota`). Настоящая реализация — `ops.feeds_registry` (таск 14); до неё
фиды принимают любой объект с методом `use`.
"""

from lab.feeds.quota import (
    CountingQuota,
    FeedsRegistry,
    MemoryFeedsRegistry,
    NullQuota,
    QuotaSink,
)

__all__ = ["CountingQuota", "FeedsRegistry", "MemoryFeedsRegistry", "NullQuota", "QuotaSink"]
