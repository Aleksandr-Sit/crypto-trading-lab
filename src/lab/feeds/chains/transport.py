"""HTTP-транспорт сетевых клиентов: протокол, живая реализация на httpx и ошибки.

Сеть спрятана за протоколом ровно ради тестов: в них подставляется `FakeHttpTransport`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

DEFAULT_TIMEOUT_S = 15.0


class ChainError(RuntimeError):
    """Ошибка сетевого клиента: нет связи, отказ провайдера, неразобранный ответ."""


class ChainUnsupported(NotImplementedError):
    """Метод `Feed`, которого у сети нет (свечи и стакан — не к ончейн-клиенту)."""


class ChainDisabled(RuntimeError):
    """Сеть не включена флагом `CHAINS_ENABLED` (G07: включаем по одной)."""


@runtime_checkable
class HttpTransport(Protocol):
    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None) -> Any: ...

    def post(self, url: str, *, json: dict | None = None, headers: dict | None = None) -> Any: ...


class HttpxTransport:
    """Живой транспорт. `httpx` импортируется лениво: без сети клиент просто не создаётся."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S, client: Any = None) -> None:
        self.timeout_s = timeout_s
        self._client = client

    def _c(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout_s)
        return self._client

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None) -> Any:
        return self._request("GET", url, params=params, headers=headers)

    def post(self, url: str, *, json: dict | None = None, headers: dict | None = None) -> Any:
        return self._request("POST", url, json=json, headers=headers)

    def _request(self, method: str, url: str, **kw: Any) -> Any:
        import httpx

        try:
            response = self._c().request(method, url, **kw)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as err:
            raise ChainError(f"{url}: {err.response.status_code}") from err
        except httpx.HTTPError as err:
            raise ChainError(f"{url}: нет связи ({err})") from err

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "ChainDisabled",
    "ChainError",
    "ChainUnsupported",
    "HttpTransport",
    "HttpxTransport",
]
