"""Где лежат карточки каталога стратегий (тикет 13)."""

from pathlib import Path

CARDS_DIR = Path(__file__).resolve().parents[4] / "docs" / "research" / "strategies"

__all__ = ["CARDS_DIR"]
