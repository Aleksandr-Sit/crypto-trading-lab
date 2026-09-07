"""Интерфейс индикатора (G08): `Indicator.compute(frame) -> frame`.

v0 — по публичным описаниям; v1 — по скрипту пользователя: v1 наследует тот же класс, меняет
`version = "v1"` и логику `compute`, тест `tests/strategies/test_indicators.py::test_indicator_reference_values`
сверяет с эталоном (`tests/strategies/fixtures/indicator_reference.csv`).
Где логика неизвестна — `compute` поднимает `IndicatorNotPorted` с меткой `[ИНДИКАТОР — нужен скрипт]`.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from lab.strategies.indicators.frame import Frame

LABEL_V0 = "[ИНДИКАТОР v0 — по публичным описаниям; v1 — по скрипту пользователя]"
LABEL_STUB = "[ИНДИКАТОР — нужен скрипт]"


class IndicatorNotPorted(NotImplementedError):
    """Логика индикатора публично не описана — порт ждёт скрипт пользователя."""


@runtime_checkable
class Indicator(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]

    def compute(self, frame: Frame) -> Frame: ...


class BaseIndicator:
    name: ClassVar[str] = ""
    version: ClassVar[str] = "v0"
    source: ClassVar[str] = ""  # ссылка на публичное описание

    def compute(self, frame: Frame) -> Frame:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name} {self.version})"


class StubIndicator(BaseIndicator):
    """Заглушка: имя и источник известны, формулы нет."""

    what_is_known: ClassVar[str] = ""

    def compute(self, frame: Frame) -> Frame:
        raise IndicatorNotPorted(
            f"{LABEL_STUB} {self.name}: {self.what_is_known or 'формула не раскрыта'}"
        )


__all__ = [
    "LABEL_STUB",
    "LABEL_V0",
    "BaseIndicator",
    "Indicator",
    "IndicatorNotPorted",
    "StubIndicator",
]
