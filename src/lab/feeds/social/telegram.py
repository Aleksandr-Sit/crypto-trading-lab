"""Читалка Telegram-каналов авторов через Telethon (`TELEGRAM_API_ID/HASH`).

Без ключей — `available() == False`, `health()` = down «недоступен», `read()` — пустой список;
ничего не падает. Telethon импортируется лениво — тесты и среда без него живут.
Каналы — из `config/authors.yaml` (platform: telegram, url без `[ССЫЛКА — впиши]`)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from lab.config import CONFIG_DIR
from lab.contracts import Health

UNAVAILABLE = "недоступен: нет TELEGRAM_API_ID/TELEGRAM_API_HASH"


@dataclass(frozen=True)
class ChannelMessage:
    channel: str
    message_id: int
    published_at: datetime
    text: str

    @property
    def ref(self) -> str:
        return str(self.message_id)


def channel_slug(url: str) -> str | None:
    if "впиши" in url or "t.me/" not in url:
        return None
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug or None


def channels_from_authors(path: Path | str | None = None) -> list[tuple[str, str]]:
    """[(author_id, channel_slug)] — только telegram-каналы с реальной ссылкой;
    боты не включаются."""
    raw = (
        yaml.safe_load(Path(path or CONFIG_DIR / "authors.yaml").read_text(encoding="utf-8")) or {}
    )
    out: list[tuple[str, str]] = []
    for author in raw.get("authors") or []:
        for ch in author.get("channels") or []:
            if ch.get("platform") != "telegram":
                continue
            slug = channel_slug(str(ch.get("url", "")))
            if slug and not slug.lower().endswith("bot"):
                out.append((str(author["id"]), slug))
    return out


class TelegramReader:
    def __init__(
        self, api_id: str | None, api_hash: str | None, *, session: str = "lab-reader", client=None
    ) -> None:
        self.api_id, self.api_hash, self.session_name = api_id, api_hash, session
        self._client = client  # подставной клиент в тестах

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TelegramReader:
        env = os.environ if env is None else env
        return cls(env.get("TELEGRAM_API_ID") or None, env.get("TELEGRAM_API_HASH") or None)

    def available(self) -> bool:
        if self._client is not None:
            return True
        if not (self.api_id and self.api_hash):
            return False
        try:
            import telethon  # noqa: F401
        except ImportError:
            return False
        return True

    def health(self) -> Health:
        now = datetime.now(UTC)
        if not self.available():
            return Health(status="down", detail=UNAVAILABLE, checked_at=now)
        return Health(status="ok", detail="Telethon настроен", checked_at=now)

    def read(self, channel: str, *, since: datetime, limit: int = 200) -> list[ChannelMessage]:
        if not self.available():
            return []
        return asyncio.run(self._read(channel, since, limit))

    async def _read(self, channel: str, since: datetime, limit: int) -> list[ChannelMessage]:
        client = self._client
        if client is None:
            from telethon import TelegramClient

            client = TelegramClient(self.session_name, int(self.api_id), self.api_hash)  # type: ignore[arg-type]
        out: list[ChannelMessage] = []
        async with client:
            async for msg in client.iter_messages(channel, limit=limit):
                ts = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=UTC)
                if ts < since:
                    break
                if msg.message:
                    out.append(ChannelMessage(channel, msg.id, ts, msg.message))
        return out


__all__ = [
    "UNAVAILABLE",
    "ChannelMessage",
    "TelegramReader",
    "channel_slug",
    "channels_from_authors",
]
