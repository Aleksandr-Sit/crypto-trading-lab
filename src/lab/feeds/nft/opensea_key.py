"""Free-ключ OpenSea v2 живёт 7 дней — система обновляет его сама (research §6).

Ключ из `.env` (`OPENSEA_API_KEY`) используется, пока не истёк срок; дальше запрашивается
новый через `POST /api/v2/auth/keys`. Значение ключа не пишется в логи — наружу выходит
только факт обновления и время.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from lab.feeds.chains.transport import HttpTransport
from lab.feeds.nft.config import NftConfig, load_nft

AUTH_URL = "https://api.opensea.io/api/v2/auth/keys"
KEY_ENV = "OPENSEA_API_KEY"
ISSUED_ENV = "OPENSEA_API_KEY_ISSUED_AT"


class OpenSeaKeyError(RuntimeError):
    """Ключ не удалось получить — ветка читает OpenSea без ключа или не читает."""


@dataclass
class KeyState:
    value: str = ""
    issued_at: datetime | None = None
    refreshed: int = 0


class OpenSeaKey:
    """Держатель ключа: знает срок, обновляет заранее, не печатает значение."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        key: str | None = None,
        issued_at: datetime | None = None,
        config: NftConfig | None = None,
        env: dict[str, str] | None = None,
        renew_before_h: int = 12,
    ) -> None:
        self.transport = transport
        self.config = config or load_nft()
        source = env if env is not None else os.environ
        self.ttl_days = self.config.markets.get("opensea").key_ttl_days or 7
        self.renew_before = timedelta(hours=renew_before_h)
        issued = issued_at
        if issued is None and source.get(ISSUED_ENV):
            try:
                issued = datetime.fromisoformat(source[ISSUED_ENV])
            except ValueError:
                issued = None
        self.state = KeyState(value=key or source.get(KEY_ENV, "") or "", issued_at=issued)

    # -- срок ---------------------------------------------------------------------------

    def expires_at(self) -> datetime | None:
        if self.state.issued_at is None:
            return None
        return self.state.issued_at + timedelta(days=self.ttl_days)

    def expired(self, *, now: datetime | None = None) -> bool:
        """Истёкшим считаем и ключ, которому осталось меньше `renew_before`."""
        at = now or datetime.now(UTC)
        expires = self.expires_at()
        if not self.state.value:
            return True
        if expires is None:  # срок неизвестен — ключ из .env, доверяем ему
            return False
        return at >= expires - self.renew_before

    # -- обновление ---------------------------------------------------------------------

    def refresh(self, *, now: datetime | None = None) -> str:
        at = now or datetime.now(UTC)
        try:
            payload = self.transport.post(AUTH_URL, json={}) or {}
        except Exception as exc:  # noqa: BLE001 — сеть могла не ответить
            raise OpenSeaKeyError(f"opensea: ключ не выдан: {exc}") from exc
        value = str(payload.get("api_key") or payload.get("key") or "")
        if not value:
            raise OpenSeaKeyError("opensea: ответ без ключа")
        self.state = KeyState(value=value, issued_at=at, refreshed=self.state.refreshed + 1)
        return value

    def value(self, *, now: datetime | None = None) -> str:
        """Действующий ключ. Истёк — обновляется; не удалось обновить — пустая строка."""
        if not self.expired(now=now):
            return self.state.value
        try:
            return self.refresh(now=now)
        except OpenSeaKeyError:
            return self.state.value  # старый ключ лучше, чем ничего: пусть площадка ответит 401

    def status(self, *, now: datetime | None = None) -> str:
        expires = self.expires_at()
        if not self.state.value:
            return "ключа нет"
        if expires is None:
            return "ключ из .env, срок неизвестен"
        left = expires - (now or datetime.now(UTC))
        return f"ключ действует ещё {max(0, int(left.total_seconds() // 3600))} ч"
