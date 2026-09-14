"""Ряды CryptoQuant. Хранилище общее для источников — см. `lab.data.daily_market`.

Модуль оставлен точкой импорта: на него ссылаются фид, задание и тесты, и переписывать
их ради переезда класса незачем.
"""

from lab.data.daily_market import (
    FIELDS,
    MARKET,
    SCHEMA,
    CryptoQuantStore,
    DailyRow,
)

__all__ = ["FIELDS", "MARKET", "SCHEMA", "CryptoQuantStore", "DailyRow"]
