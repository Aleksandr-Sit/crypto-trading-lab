"""Фейковый HTTP-транспорт: ответы задаются маршрутами, вызовы пишутся в журнал.

Тесты сетей не ходят в сеть — это и есть подстановка.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from lab.feeds.chains.transport import ChainError


class Call(NamedTuple):
    method: str
    url: str
    params: dict | None
    json: dict | None


class FakeHttpTransport:
    def __init__(self, *, offline: bool = False) -> None:
        self.routes: list[tuple[str, str, Any]] = []
        self.calls: list[Call] = []
        self.offline = offline

    def route(self, method: str, match: str, value: Any) -> FakeHttpTransport:
        """`match` — подстрока URL; `value` — готовый ответ или `f(url, params, json)`."""
        self.routes.append((method.upper(), match, value))
        return self

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None) -> Any:
        return self._call("GET", url, params, None)

    def post(self, url: str, *, json: dict | None = None, headers: dict | None = None) -> Any:
        return self._call("POST", url, None, json)

    def _call(self, method: str, url: str, params: dict | None, json: dict | None) -> Any:
        self.calls.append(Call(method, url, params, json))
        if self.offline:
            raise ChainError(f"{url}: нет связи (офлайн-транспорт)")
        for route_method, match, value in self.routes:
            if route_method == method and match in url:
                if isinstance(value, Callable):
                    return value(url, params, json)
                return value
        raise ChainError(f"{method} {url}: маршрут не задан в FakeHttpTransport")


__all__ = ["Call", "FakeHttpTransport"]
