"""WebSocket за протоколом: живой клиент на `websockets`, в тестах — фейк со сценарием.

Реконнект живёт здесь, а не в фиде: обрыв стрима новых токенов — норма, а не исключение,
и фид обязан пережить его молча (История 65).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, runtime_checkable

DEFAULT_RECONNECT_DELAY_S = 1.0
MAX_RECONNECT_DELAY_S = 30.0


class WsClosed(RuntimeError):
    """Соединение закрылось — фид переподключается."""


@runtime_checkable
class WsConnection(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


WsFactory = Callable[[str], Awaitable[WsConnection]]


class WebsocketsFactory:
    """Живой транспорт. `websockets` импортируется лениво: без сети фид просто не открыть."""

    def __init__(self, *, open_timeout_s: float = 10.0) -> None:
        self.open_timeout_s = open_timeout_s

    async def __call__(self, url: str) -> WsConnection:
        import websockets

        return await websockets.connect(url, open_timeout=self.open_timeout_s)  # type: ignore[return-value]


class FakeWebSocket:
    """Сценарий сообщений; `drop_after` — обрыв после N сообщений (проверка реконнекта)."""

    def __init__(self, messages: Sequence[str], *, drop_after: int | None = None) -> None:
        self.messages = list(messages)
        self.drop_after = drop_after
        self.sent: list[str] = []
        self.closed = False
        self._read = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if self.drop_after is not None and self._read >= self.drop_after:
            raise WsClosed("фейковое соединение оборвано")
        if self._read >= len(self.messages):
            await asyncio.sleep(0)
            raise WsClosed("сообщения сценария кончились")
        message = self.messages[self._read]
        self._read += 1
        return message

    async def close(self) -> None:
        self.closed = True


class FakeWsFactory:
    """Очередь соединений: каждое подключение берёт следующее из сценария."""

    def __init__(self, connections: Sequence[FakeWebSocket]) -> None:
        self.connections = list(connections)
        self.opened: list[FakeWebSocket] = []
        self.urls: list[str] = []

    async def __call__(self, url: str) -> Any:
        self.urls.append(url)
        if not self.connections:
            raise WsClosed("сценарий фейковых соединений исчерпан")
        conn = self.connections.pop(0)
        self.opened.append(conn)
        return conn


__all__ = [
    "DEFAULT_RECONNECT_DELAY_S",
    "MAX_RECONNECT_DELAY_S",
    "FakeWebSocket",
    "FakeWsFactory",
    "WebsocketsFactory",
    "WsClosed",
    "WsConnection",
    "WsFactory",
]
