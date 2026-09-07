"""Solana: Helius RPC + Enhanced Transactions (`research-sources.md` §3).

Enhanced Transactions отдают уже разобранный своп (`events.swap`) — это единственная
бесплатная возможность получить сделки кошелька без собственного индексатора
(Birdeye wallet — Premium, GMGN публичного API не имеет).
Квота Helius — 1M кредитов/мес, 2 RPS на Enhanced: считаем каждый вызов.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lab.feeds.chains.base import ChainFeed
from lab.feeds.chains.transport import ChainError
from lab.feeds.chains.types import STABLES, WalletTrade

API_BASE = "https://api.helius.xyz"
RPC_BASE = "https://mainnet.helius-rpc.com"
LAMPORTS = Decimal(10**9)

# Известные котируемые токены: своп «токен ↔ квота» читается как покупка/продажа токена.
QUOTE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "So11111111111111111111111111111111111111112": "SOL",
}
NATIVE = "SOL"


def _amount(raw: dict[str, Any]) -> Decimal:
    token = raw.get("rawTokenAmount") or {}
    value = Decimal(str(token.get("tokenAmount", raw.get("tokenAmount", 0))))
    decimals = int(token.get("decimals", raw.get("decimals", 0)))
    return value / (Decimal(10) ** decimals)


class SolanaFeed(ChainFeed):
    chain = "solana"
    config_key = "solana"

    def __init__(self, transport, *, api_base: str = API_BASE, rpc_base: str = RPC_BASE, **kw):
        super().__init__(transport, **kw)
        self.api_base = api_base
        self.rpc_base = rpc_base

    # -- сделки кошелька -------------------------------------------------------------

    def wallet_trades(
        self,
        address: str,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        *,
        limit: int = 100,
    ) -> list[WalletTrade]:
        params = {"api-key": self.api_key or "", "type": "SWAP", "limit": limit}
        rows = self._get(f"{self.api_base}/v0/addresses/{address}/transactions", params)
        if not isinstance(rows, list):
            raise ChainError(f"Helius: неожиданный ответ по {address}")
        out: list[WalletTrade] = []
        for row in rows:
            trade = self._parse(address, row)
            if trade is None:
                continue
            if from_ts and trade.ts < from_ts:
                continue
            if to_ts and trade.ts >= to_ts:
                continue
            out.append(trade)
        return sorted(out, key=lambda t: t.ts)

    def _parse(self, address: str, row: dict[str, Any]) -> WalletTrade | None:
        swap = (row.get("events") or {}).get("swap")
        if not swap:
            return None
        given = self._side_of(swap, "tokenInputs", swap.get("nativeInput"))
        got = self._side_of(swap, "tokenOutputs", swap.get("nativeOutput"))
        if given is None or got is None:
            return None
        (in_mint, in_symbol, in_qty), (out_mint, out_symbol, out_qty) = given, got
        if in_symbol in QUOTE_MINTS.values() or in_symbol == NATIVE:
            side, token, symbol, qty = "buy", out_mint, out_symbol, out_qty
            quote_asset, quote_qty = in_symbol, in_qty
        else:
            side, token, symbol, qty = "sell", in_mint, in_symbol, in_qty
            quote_asset, quote_qty = out_symbol, out_qty
        ts = datetime.fromtimestamp(int(row.get("timestamp", 0)), tz=UTC)
        return WalletTrade(
            chain=self.chain,
            address=address,
            tx=str(row.get("signature", "")),
            ts=ts,
            token=token,
            symbol=symbol,
            side=side,
            qty=qty,
            quote_asset=quote_asset,
            quote_qty=quote_qty,
            value_usd=quote_qty if quote_asset in STABLES else None,
            fee=Decimal(str(row.get("fee", 0))) / LAMPORTS,
            venue=str(row.get("source", "")),
        )

    def _side_of(
        self, swap: dict[str, Any], key: str, native: dict[str, Any] | None
    ) -> tuple[str, str, Decimal] | None:
        tokens = swap.get(key) or []
        if tokens:
            first = tokens[0]
            mint = str(first.get("mint", ""))
            return mint, QUOTE_MINTS.get(mint, mint[:6]), _amount(first)
        if native:
            amount = Decimal(str(native.get("amount", 0))) / LAMPORTS
            return QUOTE_MINTS_SOL, NATIVE, amount
        return None

    # -- health ----------------------------------------------------------------------

    def _ping(self) -> None:
        self._post(
            f"{self.rpc_base}/?api-key={self.api_key}",
            {"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
        )


QUOTE_MINTS_SOL = "So11111111111111111111111111111111111111112"

__all__ = ["API_BASE", "QUOTE_MINTS", "RPC_BASE", "SolanaFeed"]
