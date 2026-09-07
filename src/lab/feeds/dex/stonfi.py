"""STON.fi (`research-sources.md` §5): жетоны и пулы TON без ключа (`api.ston.fi/v1`)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lab.contracts import Event
from lab.feeds.dex.base import DexFeed
from lab.feeds.dex.types import NEW_PAIR_EVENT, TokenInfo, token_payload

BASE = "https://api.ston.fi/v1"
FEED_ID = "stonfi"
VENUE = "stonfi"


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class StonFiFeed(DexFeed):
    feed_id = FEED_ID
    chain = "ton"

    def __init__(self, transport, *, base: str = BASE, quota=None) -> None:
        super().__init__(transport, quota=quota)
        self.base = base

    def to_token(self, raw: dict[str, Any]) -> TokenInfo | None:
        address = str(raw.get("contract_address", ""))
        if not address or raw.get("deprecated"):
            return None
        return TokenInfo(
            address=address,
            chain=self.chain,
            symbol=str(raw.get("symbol", "")),
            name=str(raw.get("display_name", "")),
            source=FEED_ID,
            venue=VENUE,
            price_usd=_dec(raw.get("dex_price_usd") or raw.get("dex_usd_price")),
            liquidity_usd=_dec(raw.get("liquidity_usd") or raw.get("tvl_usd")),
            volume_usd=_dec(raw.get("volume_usd")),
        )

    def new_tokens(self, *, limit: int = 100) -> list[TokenInfo]:
        raw = self._get(f"{self.base}/assets")
        rows = (raw or {}).get("asset_list") or []
        out = []
        for row in rows[:limit]:
            token = self.to_token(row)
            if token is not None:
                out.append(token)
        return out

    def token(self, address: str) -> TokenInfo | None:
        raw = self._get(f"{self.base}/assets/{address}")
        asset = (raw or {}).get("asset") or {}
        return self.to_token(asset) if asset else None

    def pools(self) -> list[dict[str, Any]]:
        raw = self._get(f"{self.base}/pools")
        return list((raw or {}).get("pool_list") or [])

    async def events(  # type: ignore[override]
        self, kind: str = NEW_PAIR_EVENT, *, poll_s: float = 60.0, **kw: Any
    ) -> AsyncIterator[Event]:
        if kind != NEW_PAIR_EVENT:
            raise ValueError(f"stonfi: неизвестный вид событий {kind!r}")
        seen: set[str] = set()
        while True:
            for token in self.new_tokens():
                if token.key in seen:
                    continue
                seen.add(token.key)
                yield Event(kind=kind, ts=datetime.now(UTC), payload=token_payload(token))
            await asyncio.sleep(poll_s)

    def _ping(self) -> None:
        self._get(f"{self.base}/assets")


__all__ = ["BASE", "FEED_ID", "VENUE", "StonFiFeed"]
