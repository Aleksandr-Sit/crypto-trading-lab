"""Транспорт ccxt: протокол в форме унифицированных структур ccxt и живая обёртка.

Фид и исполнитель говорят с площадкой только через `Transport`; в тестах — `FakeTransport`
(`lab.feeds.cex.fake`), который отдаёт те же словари ccxt. Исключения — классы ccxt
(NetworkError / RequestTimeout / PermissionDenied ...), фейк поднимает те же.

Секреты читаются из окружения по именам `lab.config.VENUE_ENV`; значения не логируются.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from lab.config import VENUE_ENV

VENUES = ("bybit", "okx", "binance", "hyperliquid")


@dataclass(frozen=True)
class VenueSpec:
    id: str
    page_limit: int  # свечей за запрос (Bybit 1000, OKX 100, Binance 1000, HL candleSnapshot 5000)
    quote_asset: str
    weight: int = 1  # вес одного запроса для квоты (HL — weight-based)
    client_id_prefix: str = "lab"
    rate_limit_note: str = ""
    extra_options: dict[str, Any] = field(default_factory=dict)


VENUE_SPECS: dict[str, VenueSpec] = {
    "bybit": VenueSpec("bybit", 1000, "USDT", rate_limit_note="600 req / 5 s на IP"),
    "okx": VenueSpec("okx", 100, "USDT", rate_limit_note="candles 40 req / 2 s"),
    "binance": VenueSpec("binance", 1000, "USDT", rate_limit_note="6000 weight / мин"),
    "hyperliquid": VenueSpec(
        "hyperliquid",
        5000,
        "USDC",
        weight=20,
        rate_limit_note="1200 weight / мин; candleSnapshot — 5000 последних свечей",
    ),
}


@runtime_checkable
class Transport(Protocol):
    """Подмножество ccxt.Exchange, которым пользуются фид и исполнитель, плюс права ключа."""

    id: str
    has_keys: bool

    def load_markets(self) -> dict[str, Any]: ...
    def market(self, symbol: str) -> dict[str, Any]: ...
    def fetch_time(self) -> int: ...
    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None, limit: int | None
    ) -> list[list[Any]]: ...
    def fetch_trades(self, symbol: str, since: int | None, limit: int | None) -> list[dict]: ...
    def fetch_order_book(self, symbol: str, limit: int | None) -> dict[str, Any]: ...
    def fetch_ticker(self, symbol: str) -> dict[str, Any]: ...
    def fetch_funding_rate(self, symbol: str) -> dict[str, Any]: ...
    def fetch_funding_history(self, symbol: str | None, since: int | None) -> list[dict]: ...
    def create_order(
        self, symbol: str, type: str, side: str, amount: float, price: float | None, params: dict
    ) -> dict[str, Any]: ...
    def cancel_order(self, id: str, symbol: str | None) -> dict[str, Any]: ...
    def fetch_order(self, id: str, symbol: str | None) -> dict[str, Any]: ...
    def fetch_open_orders(self, symbol: str | None) -> list[dict]: ...
    def fetch_closed_orders(self, symbol: str | None, since: int | None) -> list[dict]: ...
    def fetch_my_trades(self, symbol: str | None, since: int | None) -> list[dict]: ...
    def fetch_positions(self, symbols: list[str] | None) -> list[dict]: ...
    def fetch_balance(self) -> dict[str, Any]: ...
    def set_leverage(self, leverage: int, symbol: str) -> Any: ...
    def key_rights(self) -> dict[str, bool]: ...


def credentials_from_env(venue: str, env: dict[str, str] | None = None) -> dict[str, str]:
    """Ключи площадки из окружения; пустые значения = ключа нет (режим «только данные»)."""
    env = os.environ if env is None else env
    names = VENUE_ENV.get(venue, ())
    values = {name: (env.get(name) or "").strip() for name in names}
    if not all(values.values()):
        return {}
    if venue == "hyperliquid":
        return {"privateKey": values["HYPERLIQUID_PRIVATE_KEY"]}
    out = {"apiKey": values[names[0]], "secret": values[names[1]]}
    if venue == "okx":
        out["password"] = values["OKX_API_PASSPHRASE"]
    return out


class CcxtTransport:
    """Живая обёртка над ccxt.Exchange (сеть). В тестах не используется."""

    def __init__(self, venue: str, credentials: dict[str, str] | None = None, **options: Any):
        import ccxt  # локальный импорт: тесты без сети не тянут клиентов

        if venue not in VENUE_SPECS:
            raise ValueError(f"неизвестная площадка: {venue}")
        creds = credentials if credentials is not None else credentials_from_env(venue)
        cfg: dict[str, Any] = {"enableRateLimit": True, **VENUE_SPECS[venue].extra_options}
        cfg.update(creds)
        cfg.update(options)
        if venue == "hyperliquid" and "privateKey" in creds:
            # адрес кошелька выводится из ключа агентом ccxt; walletAddress можно задать options
            cfg.setdefault("walletAddress", options.get("walletAddress"))
        self.id = venue
        self.has_keys = bool(creds)
        self._ex = getattr(ccxt, venue)(cfg)

    def __getattr__(self, name: str) -> Any:  # проксируем всё унифицированное API ccxt
        return getattr(self._ex, name)

    def key_rights(self) -> dict[str, bool]:
        """Права ключа из реального API площадки (История 54)."""
        if not self.has_keys:
            return {"trade": False, "withdraw": False}
        ex = self._ex
        if self.id == "bybit":
            info = ex.privateGetV5UserQueryApi()["result"]
            perms = info.get("permissions") or {}
            wallet = [p.lower() for p in perms.get("Wallet", [])]
            trade = any(perms.get(k) for k in ("Spot", "ContractTrade", "Derivatives"))
            return {"trade": bool(trade), "withdraw": "withdraw" in wallet}
        if self.id == "binance":
            info = ex.sapiGetAccountApiRestrictions()
            return {
                "trade": bool(info.get("enableSpotAndMarginTrading") or info.get("enableFutures")),
                "withdraw": bool(info.get("enableWithdrawals")),
            }
        if self.id == "okx":
            data = ex.privateGetAccountConfig()["data"][0]
            perm = str(data.get("perm", "")).split(",")
            return {"trade": "trade" in perm, "withdraw": "withdraw" in perm}
        # Hyperliquid: агентский ключ подписывает только ордера, вывод невозможен by design
        return {"trade": True, "withdraw": False}


__all__ = [
    "VENUES",
    "VENUE_SPECS",
    "CcxtTransport",
    "Transport",
    "VenueSpec",
    "credentials_from_env",
]
