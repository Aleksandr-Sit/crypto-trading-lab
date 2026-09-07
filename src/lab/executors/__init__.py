"""Исполнители площадок. Импорт пакета регистрирует встроенные исполнители."""

from lab.executors import registry
from lab.executors.fake import FakeExecutor

registry.register("fake", FakeExecutor, replace=True)
import lab.executors.cex  # noqa: E402,F401 — регистрирует bybit/okx/binance/hyperliquid (таск 04)

__all__ = ["FakeExecutor", "registry"]
