"""DexScreener (`research-sources.md` §3): пары и новые токены EVM/BNB/Solana без ключа.

Лимит 300 rpm на публичных эндпоинтах — каждый вызов считается через `feeds_registry`.
OHLCV и истории сделок здесь нет (см. `DexFeed`), только снимок пары: цена, ликвидность,
объём, счётчики покупок/продаж — ровно то, на что смотрит первичный фильтр потока.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lab.contracts import Event
from lab.feeds.dex.base import DexFeed
from lab.feeds.dex.types import NEW_PAIR_EVENT, TokenInfo, token_payload

BASE = "https://api.dexscreener.com"
FEED_ID = "dexscreener"
RATE_LIMIT_RPM = 300

# Имена сетей DexScreener → наши имена сетей (`config/chains.yaml`).
CHAIN_IDS = {"solana": "solana", "ethereum": "ethereum", "base": "base", "bsc": "bnb"}
OUR_CHAINS = {v: k for k, v in CHAIN_IDS.items()}


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class DexScreenerFeed(DexFeed):
    feed_id = FEED_ID
    rate_limit_rpm = RATE_LIMIT_RPM

    def __init__(self, transport, *, base: str = BASE, quota=None) -> None:
        super().__init__(transport, quota=quota)
        self.base = base

    # -- разбор ---------------------------------------------------------------------------

    def to_token(self, pair: dict[str, Any]) -> TokenInfo | None:
        base_token = pair.get("baseToken") or {}
        address = str(base_token.get("address", ""))
        if not address:
            return None
        created = pair.get("pairCreatedAt")
        txns = (pair.get("txns") or {}).get("h24") or {}
        chain_id = str(pair.get("chainId", ""))
        return TokenInfo(
            address=address,
            chain=CHAIN_IDS.get(chain_id, chain_id),
            symbol=str(base_token.get("symbol", "")),
            name=str(base_token.get("name", "")),
            created_at=(
                datetime.fromtimestamp(int(created) / 1000, tz=UTC) if created else None
            ),
            source=FEED_ID,
            venue=str(pair.get("dexId", "")),
            pair=str(pair.get("pairAddress", "")),
            price_usd=_dec(pair.get("priceUsd")),
            liquidity_usd=_dec((pair.get("liquidity") or {}).get("usd")),
            volume_usd=_dec((pair.get("volume") or {}).get("h24")),
            market_cap_usd=_dec(pair.get("marketCap") or pair.get("fdv")),
            buys=int(txns["buys"]) if txns.get("buys") is not None else None,
            sells=int(txns["sells"]) if txns.get("sells") is not None else None,
        )

    # -- чтение ---------------------------------------------------------------------------

    def pairs(self, chain: str, token_address: str) -> list[TokenInfo]:
        raw = self._get(f"{self.base}/latest/dex/tokens/{token_address}")
        rows = (raw or {}).get("pairs") or []
        want = OUR_CHAINS.get(chain, chain)
        out = []
        for row in rows:
            if row.get("chainId") not in (want, chain):
                continue
            token = self.to_token(row)
            if token is not None:
                out.append(token)
        return out

    def token(self, chain: str, token_address: str) -> TokenInfo | None:
        """Пара с наибольшей ликвидностью — она и представляет токен."""
        pairs = self.pairs(chain, token_address)
        if not pairs:
            return None
        return max(pairs, key=lambda t: t.liquidity_usd or Decimal(0))

    def new_tokens(self, *, chains: Sequence[str] = ()) -> list[TokenInfo]:
        """Свежие профили токенов (`token-profiles/latest`) с добором снимка пары."""
        raw = self._get(f"{self.base}/token-profiles/latest/v1")
        rows = raw if isinstance(raw, list) else (raw or {}).get("profiles") or []
        wanted = {OUR_CHAINS.get(c, c) for c in chains} if chains else None
        out: list[TokenInfo] = []
        for row in rows:
            chain_id = str(row.get("chainId", ""))
            if wanted is not None and chain_id not in wanted:
                continue
            address = str(row.get("tokenAddress", ""))
            if not address:
                continue
            token = self.token(CHAIN_IDS.get(chain_id, chain_id), address)
            if token is not None:
                out.append(token)
        return out

    async def events(  # type: ignore[override]
        self,
        kind: str = NEW_PAIR_EVENT,
        *,
        chains: Sequence[str] = (),
        poll_s: float = 30.0,
        **kw: Any,
    ) -> AsyncIterator[Event]:
        """Поток новых пар опросом: WS у DexScreener нет."""
        if kind != NEW_PAIR_EVENT:
            raise ValueError(f"dexscreener: неизвестный вид событий {kind!r}")
        seen: set[str] = set()
        while True:
            for token in self.new_tokens(chains=chains):
                if token.key in seen:
                    continue
                seen.add(token.key)
                yield Event(kind=kind, ts=datetime.now(UTC), payload=token_payload(token))
            await asyncio.sleep(poll_s)

    def _ping(self) -> None:
        self._get(f"{self.base}/token-profiles/latest/v1")


__all__ = ["BASE", "CHAIN_IDS", "FEED_ID", "RATE_LIMIT_RPM", "DexScreenerFeed"]
