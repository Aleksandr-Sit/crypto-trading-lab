"""TON: tonapi.io (события аккаунта) + цены STON.fi (`research-sources.md` §5).

Без ключа tonapi даёт 0.25 rps — этого хватает на слежение за десятком кошельков,
но не на индексацию; ключ (`TONAPI_KEY`) поднимает лимит.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lab.feeds.chains.base import ChainFeed
from lab.feeds.chains.transport import ChainError
from lab.feeds.chains.types import WalletTrade

TONAPI = "https://tonapi.io"
STONFI = "https://api.ston.fi/v1"
NANO = Decimal(10**9)


def _jetton(raw: dict[str, Any] | None) -> tuple[str, str, int]:
    raw = raw or {}
    return (
        str(raw.get("address", "")),
        str(raw.get("symbol", "") or raw.get("name", "")),
        int(raw.get("decimals", 9)),
    )


class TonFeed(ChainFeed):
    chain = "ton"
    config_key = "ton"

    def __init__(self, transport, *, api_base: str = TONAPI, stonfi: str = STONFI, **kw) -> None:
        super().__init__(transport, **kw)
        self.api_base = api_base
        self.stonfi = stonfi

    def wallet_trades(
        self,
        address: str,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        *,
        limit: int = 100,
    ) -> list[WalletTrade]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        raw = self._get(
            f"{self.api_base}/v2/accounts/{address}/events", {"limit": limit}, headers
        )
        if not isinstance(raw, dict) or "events" not in raw:
            raise ChainError(f"tonapi: неожиданный ответ по {address}")
        out: list[WalletTrade] = []
        for event in raw["events"]:
            ts = datetime.fromtimestamp(int(event.get("timestamp", 0)), tz=UTC)
            if (from_ts and ts < from_ts) or (to_ts and ts >= to_ts):
                continue
            for action in event.get("actions", []):
                trade = self._parse(address, str(event.get("event_id", "")), ts, action)
                if trade is not None:
                    out.append(trade)
        return sorted(out, key=lambda t: t.ts)

    def _parse(
        self, address: str, tx: str, ts: datetime, action: dict[str, Any]
    ) -> WalletTrade | None:
        if action.get("type") != "JettonSwap":
            return None
        swap = action.get("JettonSwap") or {}
        ton_in = Decimal(str(swap.get("ton_in", 0) or 0)) / NANO
        ton_out = Decimal(str(swap.get("ton_out", 0) or 0)) / NANO
        in_addr, in_symbol, in_dec = _jetton(swap.get("jetton_master_in"))
        out_addr, out_symbol, out_dec = _jetton(swap.get("jetton_master_out"))
        amount_in = Decimal(str(swap.get("amount_in", 0) or 0))
        amount_out = Decimal(str(swap.get("amount_out", 0) or 0))
        if ton_in > 0 or (in_symbol == "" and not in_addr):
            side, token, symbol = "buy", out_addr, out_symbol
            qty = amount_out / (Decimal(10) ** out_dec)
            quote_asset, quote_qty = "TON", ton_in
        else:
            side, token, symbol = "sell", in_addr, in_symbol
            qty = amount_in / (Decimal(10) ** in_dec)
            quote_asset, quote_qty = "TON", ton_out
        if qty <= 0:
            return None
        return WalletTrade(
            chain=self.chain,
            address=address,
            tx=tx,
            ts=ts,
            token=token,
            symbol=symbol,
            side=side,
            qty=qty,
            quote_asset=quote_asset,
            quote_qty=quote_qty,
            value_usd=None,  # TON-квота: USD появляется только с ценой TON
            venue="stonfi",
        )

    def price_usd(self, token: str) -> Decimal | None:
        """Цена жетона в USD со STON.fi (`/v1/assets/{addr}`), если она там есть."""
        raw = self._get(f"{self.stonfi}/assets/{token}")
        asset = (raw or {}).get("asset") or {}
        price = asset.get("dex_price_usd") or asset.get("price_usd")
        return Decimal(str(price)) if price is not None else None

    def _ping(self) -> None:
        self._get(f"{self.api_base}/v2/status")


__all__ = ["STONFI", "TONAPI", "TonFeed"]
