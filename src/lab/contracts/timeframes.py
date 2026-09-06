"""Таймфреймы: строка тикета («1h») → шаг времени. Общий словарь для ядра замера и слоя данных,
чтобы слой данных не зависел от симулятора."""

from datetime import timedelta

TIMEFRAMES: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


def parse_tf(tf: str) -> timedelta:
    try:
        return TIMEFRAMES[tf]
    except KeyError as err:
        raise ValueError(f"неизвестный таймфрейм {tf!r}") from err


__all__ = ["TIMEFRAMES", "parse_tf"]
