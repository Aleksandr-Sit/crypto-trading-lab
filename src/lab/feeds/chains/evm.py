"""Ethereum / Base / BNB: Etherscan V2 (один ключ на 60+ сетей, сеть — `chainid`) + Alchemy RPC.

Сделок в API нет — есть переводы токенов: свопом считается транзакция, где кошелёк
одну сторону отдал, другую получил. Котируемой стороной считается стейбл или нативная обёртка.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lab.config import ConfigError
from lab.feeds.chains.base import ChainFeed
from lab.feeds.chains.transport import ChainError
from lab.feeds.chains.types import STABLES, WalletTrade

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
ALCHEMY_HOSTS = {
    "ethereum": "https://eth-mainnet.g.alchemy.com/v2",
    "base": "https://base-mainnet.g.alchemy.com/v2",
    "bnb": "https://bnb-mainnet.g.alchemy.com/v2",
}
CHAIN_IDS = {"ethereum": 1, "base": 8453, "bnb": 56}
QUOTE_SYMBOLS = set(STABLES) | {"WETH", "ETH", "WBNB", "BNB", "WBTC"}


class EvmFeed(ChainFeed):
    chain = "ethereum"
    config_key = "evm"

    def __init__(
        self,
        transport,
        *,
        network: str = "ethereum",
        alchemy_key: str | None = None,
        api_base: str = ETHERSCAN_V2,
        **kw,
    ) -> None:
        if network not in CHAIN_IDS:
            raise ConfigError(f"EVM: сеть {network!r} не поддержана; есть {', '.join(CHAIN_IDS)}")
        self.chain = network
        self.config_key = "bnb" if network == "bnb" else "evm"
        super().__init__(transport, **kw)
        self.network = network
        self.chain_id = CHAIN_IDS[network]
        self.alchemy_key = alchemy_key
        self.api_base = api_base

    def wallet_trades(
        self,
        address: str,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        *,
        limit: int = 100,
    ) -> list[WalletTrade]:
        params = {
            "chainid": self.chain_id,
            "module": "account",
            "action": "tokentx",
            "address": address,
            "page": 1,
            "offset": limit,
            "sort": "desc",
            "apikey": self.api_key or "",
        }
        raw = self._get(self.api_base, params)
        if not isinstance(raw, dict) or "result" not in raw:
            raise ChainError(f"Etherscan V2: неожиданный ответ по {address}")
        rows = raw["result"]
        if isinstance(rows, str):  # NOTOK возвращает текст вместо списка
            raise ChainError(f"Etherscan V2: {rows}")
        by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_tx[str(row.get("hash"))].append(row)
        out: list[WalletTrade] = []
        for tx, transfers in by_tx.items():
            trade = self._parse(address.lower(), tx, transfers)
            if trade is None:
                continue
            if (from_ts and trade.ts < from_ts) or (to_ts and trade.ts >= to_ts):
                continue
            out.append(trade)
        return sorted(out, key=lambda t: t.ts)

    def _parse(
        self, address: str, tx: str, transfers: list[dict[str, Any]]
    ) -> WalletTrade | None:
        out_leg = next((r for r in transfers if str(r.get("from", "")).lower() == address), None)
        in_leg = next((r for r in transfers if str(r.get("to", "")).lower() == address), None)
        if out_leg is None or in_leg is None:
            return None
        out_symbol, out_qty = _leg(out_leg)
        in_symbol, in_qty = _leg(in_leg)
        if out_symbol.upper() in QUOTE_SYMBOLS:
            side, leg, qty, symbol = "buy", in_leg, in_qty, in_symbol
            quote_asset, quote_qty = out_symbol, out_qty
        else:
            side, leg, qty, symbol = "sell", out_leg, out_qty, out_symbol
            quote_asset, quote_qty = in_symbol, in_qty
        return WalletTrade(
            chain=self.chain,
            address=address,
            tx=tx,
            ts=datetime.fromtimestamp(int(transfers[0].get("timeStamp", 0)), tz=UTC),
            token=str(leg.get("contractAddress", "")),
            symbol=symbol,
            side=side,
            qty=qty,
            quote_asset=quote_asset,
            quote_qty=quote_qty,
            value_usd=quote_qty if quote_asset.upper() in STABLES else None,
            venue="dex",
        )

    def _ping(self) -> None:
        if self.alchemy_key:
            self._post(
                f"{ALCHEMY_HOSTS[self.network]}/{self.alchemy_key}",
                {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
            )
            return
        self._get(
            self.api_base,
            {
                "chainid": self.chain_id,
                "module": "proxy",
                "action": "eth_blockNumber",
                "apikey": self.api_key or "",
            },
        )


def _leg(row: dict[str, Any]) -> tuple[str, Decimal]:
    decimals = int(row.get("tokenDecimal") or 18)
    qty = Decimal(str(row.get("value", 0))) / (Decimal(10) ** decimals)
    return str(row.get("tokenSymbol", "")), qty


__all__ = ["ALCHEMY_HOSTS", "CHAIN_IDS", "ETHERSCAN_V2", "EvmFeed"]
