"""pump.fun через PumpPortal WS (`research-sources.md` §3): новые токены и миграции.

Бесплатны только `subscribeNewToken` и `subscribeMigration` — трейд-стримы платные,
поэтому цена и объём берутся уже из DexScreener/агрегатора. Обрыв соединения —
обычное дело: фид переподключается сам и заново оформляет подписку, считая реконнекты.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lab.contracts import Event, Health
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.dex.base import DexFeed
from lab.feeds.dex.types import MIGRATION_EVENT, NEW_TOKEN_EVENT, TokenInfo, token_payload
from lab.feeds.dex.ws import (
    DEFAULT_RECONNECT_DELAY_S,
    MAX_RECONNECT_DELAY_S,
    FakeWsFactory,
    WebsocketsFactory,
    WsClosed,
    WsFactory,
)

WS_URL = "wss://pumpportal.fun/api/data"
FEED_ID = "pumpportal"
VENUE = "pumpfun"
METHODS = {NEW_TOKEN_EVENT: "subscribeNewToken", MIGRATION_EVENT: "subscribeMigration"}


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class PumpPortalFeed(DexFeed):
    """Стрим новых токенов и миграций. Транспорт — фабрика WS-соединений (в тестах фейк)."""

    feed_id = FEED_ID
    chain = "solana"

    def __init__(
        self,
        ws_factory: WsFactory | FakeWsFactory | None = None,
        *,
        url: str = WS_URL,
        quota: QuotaSink | None = None,
        sol_price_usd: Decimal | None = None,
        reconnect_delay_s: float = DEFAULT_RECONNECT_DELAY_S,
        max_reconnects: int | None = None,
    ) -> None:
        self.transport = None  # WS-фид не ходит по HTTP
        self.quota = quota or NullQuota()
        self.id = FEED_ID
        self.ws_factory = ws_factory or WebsocketsFactory()
        self.url = url
        self.sol_price_usd = sol_price_usd
        self.reconnect_delay_s = reconnect_delay_s
        self.max_reconnects = max_reconnects
        self.reconnects = 0
        self.last_error = ""

    # -- разбор сообщений ----------------------------------------------------------------

    def to_token(self, raw: dict[str, Any], *, migrated: bool = False) -> TokenInfo | None:
        mint = str(raw.get("mint") or raw.get("mintAddress") or "")
        if not mint:
            return None
        liquidity_sol = _dec(raw.get("vSolInBondingCurve"))
        cap_sol = _dec(raw.get("marketCapSol"))
        price = self.sol_price_usd
        return TokenInfo(
            address=mint,
            chain=self.chain,
            symbol=str(raw.get("symbol", "")),
            name=str(raw.get("name", "")),
            created_at=datetime.now(UTC),
            creator=str(raw.get("traderPublicKey", "")) or None,
            source=FEED_ID,
            venue=str(raw.get("pool", "")) or VENUE,
            pair=str(raw.get("bondingCurveKey", "") or raw.get("pool", "")),
            liquidity_usd=(
                None if (liquidity_sol is None or price is None) else liquidity_sol * price
            ),
            market_cap_usd=None if (cap_sol is None or price is None) else cap_sol * price,
            migrated=migrated,
            meta={k: str(v) for k, v in raw.items() if k not in {"mint", "name", "symbol"}},
        )

    # -- поток ---------------------------------------------------------------------------

    async def events(  # type: ignore[override]
        self, kind: str = NEW_TOKEN_EVENT, **kw: Any
    ) -> AsyncIterator[Event]:
        """`new_token` | `migration`. Обрыв → переподключение с подпиской заново."""
        if kind not in METHODS:
            raise ValueError(f"pumpportal: неизвестный вид событий {kind!r}")
        delay = self.reconnect_delay_s
        while True:
            try:
                conn = await self.ws_factory(self.url)
            except Exception as err:  # noqa: BLE001 — любой отказ подключения повторяем
                if not self._may_reconnect(err):
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY_S) if delay else 0
                continue
            self.quota.use(FEED_ID, 1)
            try:
                await conn.send(json.dumps({"method": METHODS[kind]}))
                delay = self.reconnect_delay_s
                while True:
                    message = await conn.recv()
                    event = self._event(kind, message)
                    if event is not None:
                        yield event
            except (TimeoutError, WsClosed, OSError) as err:
                if not self._may_reconnect(err):
                    return
                await asyncio.sleep(delay)
            finally:
                await conn.close()

    def _may_reconnect(self, err: Exception) -> bool:
        self.last_error = str(err)
        if self.max_reconnects is not None and self.reconnects >= self.max_reconnects:
            return False
        self.reconnects += 1
        return True

    def _event(self, kind: str, message: str) -> Event | None:
        try:
            raw = json.loads(message)
        except (TypeError, ValueError):
            return None
        if not isinstance(raw, dict) or "message" in raw:  # ответ на подписку
            return None
        tx_type = str(raw.get("txType", ""))
        if kind == MIGRATION_EVENT and tx_type not in ("migrate", ""):
            return None
        if kind == NEW_TOKEN_EVENT and tx_type not in ("create", ""):
            return None
        token = self.to_token(raw, migrated=kind == MIGRATION_EVENT)
        if token is None:
            return None
        return Event(kind=kind, ts=datetime.now(UTC), payload=token_payload(token))

    def health(self) -> Health:
        now = datetime.now(UTC)
        if self.last_error:
            return Health(
                status="degraded",
                detail=f"pumpportal: {self.reconnects} реконнект(ов), последний: {self.last_error}",
                checked_at=now,
            )
        return Health(status="ok", detail="pumpportal WS", checked_at=now)


__all__ = ["FEED_ID", "METHODS", "VENUE", "WS_URL", "PumpPortalFeed"]
