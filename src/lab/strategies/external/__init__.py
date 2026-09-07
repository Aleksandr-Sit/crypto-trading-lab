"""Адаптер внешних проектов `external-signal` (R01.3, Решения §9 — `ExternalSignalStrategy`)."""

from lab.strategies.external.adapter import (
    ExternalSignalsConfig,
    ExternalSignalStrategy,
    ExternalSource,
    file_events,
    load_external_signals,
    telegram_event,
    webhook_event,
)

__all__ = [
    "ExternalSignalStrategy",
    "ExternalSignalsConfig",
    "ExternalSource",
    "file_events",
    "load_external_signals",
    "telegram_event",
    "webhook_event",
]
