"""Социальные источники: Telegram-каналы авторов, парсер публичных сигналов,
журнал `signals_public` (R06.2, G08.1).

Читалку каналов бери фабрикой `make_reader()`: с `TELEGRAM_API_ID/HASH` это Telethon
(`TelegramReader`), без них — веб-превью `t.me/s/<канал>` (`TmePreviewReader`), которое
ключей не требует вовсе. Тип сообщения у обеих один — `ChannelMessage`."""

from lab.feeds.social.telegram import (
    ChannelMessage,
    TelegramReader,
    channel_slug,
    channels_from_authors,
)
from lab.feeds.social.tme import TmePreviewReader, make_reader

__all__ = [
    "ChannelMessage",
    "TelegramReader",
    "TmePreviewReader",
    "channel_slug",
    "channels_from_authors",
    "make_reader",
]
