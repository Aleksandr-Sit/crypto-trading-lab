"""Ярус размещения: кто в него входит и как считается его стоп (решение владельца 4, 27.09.2026).

Одно определение на замер (`core.measure.simulator`), риск-ядро (`core.risk`) и живой портфель
(`ops.portfolio`). Разойдутся — замер снова начнёт проверять не то правило, что исполняется
в бою (`docs/research/allocator-2026-09-27.md`, «Что учесть при исполнении решения 4»).

* **Кто входит** — стратегия, чья карточка объявила `allocation: true`. Признак берётся из
  параметров, а не из ветки: ротация живёт в `cex-spot` рядом с обычными правилами. И не из
  `StopSpec`: он входит в отпечаток стратегии, и новое поле сделало бы «новой» каждую
  стратегию реестра.
* **Стоп по просадке** — от ВЕРШИНЫ капитала стратегии, с открытой позицией по текущей цене:
  правило держит актив неделями, и просадку внутри позиции стоп по закрытым сделкам
  не увидел бы, пока та не закроется. У обычных стратегий стоп прежний.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

ALLOCATION_FLAG = "allocation"


def is_allocation(params: Mapping[str, Any] | None) -> bool:
    """Правило размещения — карточка объявила `allocation: true`."""
    return bool((params or {}).get(ALLOCATION_FLAG, False))


def peak_drawdown_pct(peak: Decimal, equity: Decimal) -> Decimal:
    """Просадка от вершины капитала, %: (вершина − сейчас) / вершина. Выше вершины — ноль."""
    if peak <= 0 or equity >= peak:
        return Decimal(0)
    return (peak - equity) * 100 / peak


__all__ = ["ALLOCATION_FLAG", "is_allocation", "peak_drawdown_pct"]
