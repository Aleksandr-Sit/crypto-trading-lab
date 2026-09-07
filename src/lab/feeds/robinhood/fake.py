"""Фейковый транспорт Robinhood: ответы по маршрутам, журнал вызовов вместе с заголовками.

Заголовки нужны в журнале ровно потому, что подпись — часть контракта: без неё запрос
не отличить от неподписанного.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from lab.feeds.robinhood.feed import RobinhoodError


class RhCall(NamedTuple):
    method: str
    url: str
    params: dict | None
    body: str | None
    headers: dict | None


class FakeRhTransport:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []
        self.failures: list[tuple[str, int, str]] = []
        self.calls: list[RhCall] = []

    def route(self, method: str, match: str, value: Any) -> FakeRhTransport:
        self.routes.append((method.upper(), match, value))
        return self

    def fail(self, match: str, *, status: int = 403, detail: str = "") -> FakeRhTransport:
        self.failures.append((match, status, detail))
        return self

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        body: str | None = None,
        headers: dict | None = None,
    ) -> Any:
        self.calls.append(RhCall(method.upper(), url, params, body, headers))
        for match, status, detail in self.failures:
            if match in url:
                raise RobinhoodError(f"{url}: {status} {detail}")
        for route_method, match, value in self.routes:
            if route_method == method.upper() and match in url:
                return value(url, params, body) if isinstance(value, Callable) else value
        raise RobinhoodError(f"{method} {url}: маршрут не задан в FakeRhTransport")


__all__ = ["FakeRhTransport", "RhCall"]
