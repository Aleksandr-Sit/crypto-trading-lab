"""Telegram-бот Trader (тикет 05): карточки, команды, утренний отчёт.

Логика — в `lab.bot.core.TraderBot` (без aiogram, тестируется на фейковом транспорте);
aiogram-адаптер и транспорт — в `lab.bot.telegram` (импортируется лениво, только в сервисе).
"""

from lab.bot.cards import CARD_KINDS, Card, render_card
from lab.bot.core import CallbackReply, Reply, TraderBot
from lab.bot.report import FeedsStatus, FeedStatus, MorningReport, build_morning_report

__all__ = [
    "CARD_KINDS",
    "CallbackReply",
    "Card",
    "FeedStatus",
    "FeedsStatus",
    "MorningReport",
    "Reply",
    "TraderBot",
    "build_morning_report",
    "render_card",
]
