"""Исполнители площадок. Импорт пакета регистрирует встроенные исполнители."""

from lab.executors import registry
from lab.executors.fake import FakeExecutor

registry.register("fake", FakeExecutor, replace=True)

__all__ = ["FakeExecutor", "registry"]
