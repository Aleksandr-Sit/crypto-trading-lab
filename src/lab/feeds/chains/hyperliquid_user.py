"""Hyperliquid user-data: `userFills` и `userFillsByTime` (`research-sources.md` §2).

Ограничения площадки, из-за которых глубокой истории лидера тут не будет:
`userFills` — до 2000 филлов, `userFillsByTime` — не более 10 000 последних.
Вес запроса Info API — 20 (как в таске 04), считается через квоту.
Лидерборда в официальных docs нет: `leaderboard()` ходит на общинный stats-хост
и честно помечает источник неофициальным.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lab.feeds.chains.base import ChainFeed
from lab.feeds.chains.transport import ChainError
from lab.feeds.chains.types import WalletTrade

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


class HyperliquidUserFeed(ChainFeed):
    chain = "hyperliquid"
    config_key = "hyperliquid_user"
    MAX_FILLS = 10_000

    def __init__(
        self,
        transport,
        *,
        info_url: str = INFO_URL,
        leaderboard_url: str = LEADERBOARD_URL,
        **kw,
    ) -> None:
        super().__init__(transport, **kw)
        self.info_url = info_url
        self.leaderboard_url = leaderboard_url

    def wallet_trades(
        self,
        address: str,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        *,
        limit: int = 2000,
    ) -> list[WalletTrade]:
        if from_ts is None:
            payload: dict[str, Any] = {"type": "userFills", "user": address}
        else:
            payload = {
                "type": "userFillsByTime",
                "user": address,
                "startTime": int(from_ts.timestamp() * 1000),
            }
            if to_ts is not None:
                payload["endTime"] = int(to_ts.timestamp() * 1000)
        rows = self._post(self.info_url, payload)
        if not isinstance(rows, list):
            raise ChainError(f"Hyperliquid: неожиданный ответ по {address}")
        out = [self._parse(address, row) for row in rows[: self.MAX_FILLS]]
        return sorted([t for t in out if t is not None], key=lambda t: t.ts)

    def _parse(self, address: str, row: dict[str, Any]) -> WalletTrade | None:
        try:
            qty = Decimal(str(row["sz"]))
            price = Decimal(str(row["px"]))
        except (KeyError, ValueError):
            return None
        side = "buy" if str(row.get("side", "B")).upper().startswith("B") else "sell"
        pnl = row.get("closedPnl")
        return WalletTrade(
            chain=self.chain,
            address=address,
            tx=str(row.get("hash", "")),
            ts=datetime.fromtimestamp(int(row.get("time", 0)) / 1000, tz=UTC),
            token=str(row.get("coin", "")),
            symbol=str(row.get("coin", "")),
            side=side,
            qty=qty,
            quote_asset="USDC",
            quote_qty=price * qty,
            value_usd=price * qty,
            fee=Decimal(str(row.get("fee", 0) or 0)),
            pnl=Decimal(str(pnl)) if pnl is not None else None,
            venue="hyperliquid",
        )

    def user_state(self, address: str) -> dict[str, Any]:
        return self._post(self.info_url, {"type": "clearinghouseState", "user": address})

    def leaderboard(self) -> list[dict[str, Any]]:
        """Неофициальный источник: в docs Hyperliquid лидерборда нет (см. research §2)."""
        raw = self._get(self.leaderboard_url)
        rows = (raw or {}).get("leaderboardRows", raw if isinstance(raw, list) else [])
        return list(rows)

    def _ping(self) -> None:
        self._post(self.info_url, {"type": "meta"})


__all__ = ["INFO_URL", "LEADERBOARD_URL", "HyperliquidUserFeed"]
