"""Клиент CryptoQuant: суточные ряды раздела `market-data` (бесплатный тариф).

Что здесь важно знать до чтения кода — границы тарифа, выясненные живым вызовом 14.09.2026,
а не по документации:

* окно **последние 30 суток**, и обойти его нельзя: `from=20240101` даёт
  `400 Out of allowed request range`, а не укороченный ответ;
* суточное окно открыто только в `market-data`; `exchange-flows` (резервы бирж),
  `flow-indicator`, `network-*`, `market-indicator` — `403`, Professional и выше;
* часовое окно в `market-data` — `403`, Advanced и выше;
* неверный ключ отвечает `401`, верный без права на данные — `403`. Это и есть проверка
  ключа: `403` означает, что ключ настоящий.

Отсюда единственный осмысленный режим работы: **собирать вперёд, по дню за день**.
Хранилище — `lab.data.cryptoquant.CryptoQuantStore`, задание — `ops.jobs.cryptoquant`.

Ключ берётся из `CRYPTOQUANT_API_KEY`; без него фид не падает, а честно сообщает
«недоступен», как и все остальные источники лаборатории.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from lab.config import CONFIG_DIR, load_config
from lab.contracts import Health
from lab.data.cryptoquant import MARKET, DailyRow

log = logging.getLogger(__name__)

FEED_ID = "cryptoquant"
BASE_URL = "https://api.cryptoquant.com/v1"
FREE_WINDOW_DAYS = 30

# Точка API → как её колонки называются у нас. Слева имя в ответе, справа поле `DailyRow`.
# Точки, которым нужен параметр `exchange`, и точка премии Coinbase, которой он не нужен,
# разделены намеренно: премия считается по рынку, и приписать её бирже значило бы соврать.
BY_EXCHANGE: dict[str, dict[str, str]] = {
    "market-data/liquidations": {
        "long_liquidations": "long_liq",
        "short_liquidations": "short_liq",
        "long_liquidations_usd": "long_liq_usd",
        "short_liquidations_usd": "short_liq_usd",
    },
    "market-data/taker-buy-sell-stats": {
        "taker_buy_volume": "taker_buy",
        "taker_sell_volume": "taker_sell",
        "taker_buy_ratio": "taker_buy_ratio",
    },
    "market-data/open-interest": {"open_interest": "open_interest"},
    "market-data/funding-rates": {"funding_rates": "funding_rate"},
}
MARKET_WIDE: dict[str, dict[str, str]] = {
    "market-data/coinbase-premium-index": {
        "coinbase_premium_gap": "coinbase_premium_gap",
        "coinbase_premium_index": "coinbase_premium_index",
    },
}


class CryptoQuantError(RuntimeError):
    """Источник ответил отказом или не ответил вовсе."""


class Transport(Protocol):
    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None) -> Any: ...


class CryptoQuantConfig(BaseModel):
    version: int = 1
    assets: list[str] = Field(default_factory=lambda: ["btc", "eth"])
    exchanges: list[str] = Field(
        default_factory=lambda: ["all_exchange", "binance", "bybit", "okx"]
    )
    pause_s: float = 1.0


def load_cryptoquant(path: Path | str | None = None) -> CryptoQuantConfig:
    return load_config(path or CONFIG_DIR / "cryptoquant.yaml", CryptoQuantConfig)


def _num(value: Any) -> Decimal | None:
    """Число ответа → `Decimal`, а мусор и пропуск → `None`.

    Через float не идём: деньги в лаборатории считаются `Decimal`, и округление
    источника нам добавлять незачем.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _day(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass
class CollectReport:
    """Что вышло за один проход: по сериям, запросам и отказам."""

    requests: int = 0
    rows: int = 0
    written: int = 0
    skipped: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.skipped.append(text)


class CryptoQuantFeed:
    """Чтение суточных рядов. Ничего не пишет — запись делает задание."""

    feed_id = FEED_ID

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport | None = None,
        quota: Any = None,
        base_url: str = BASE_URL,
        pause_s: float = 0.0,
        sleep: Any = time.sleep,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.quota = quota
        self.pause_s = pause_s
        self._sleep = sleep
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def transport(self) -> Transport:
        if self._transport is None:
            from lab.feeds.chains.transport import HttpxTransport

            self._transport = HttpxTransport()
        return self._transport

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.enabled:
            raise CryptoQuantError("CRYPTOQUANT_API_KEY не задан")
        if self.quota is not None:
            self.quota.use(FEED_ID, 1)
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            payload = self.transport().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except Exception as err:  # noqa: BLE001 — отказ источника не наша авария
            raise CryptoQuantError(f"{path}: {err}") from err
        if self.pause_s:
            self._sleep(self.pause_s)
        if not isinstance(payload, dict):
            raise CryptoQuantError(f"{path}: неожиданный ответ {type(payload).__name__}")
        status = payload.get("status") or {}
        code = status.get("code")
        if code not in (None, 200):
            raise CryptoQuantError(f"{path}: {code} {status.get('message', '')}".strip())
        data = (payload.get("result") or {}).get("data") or []
        return [r for r in data if isinstance(r, dict)]

    def series(
        self, asset: str, path: str, mapping: dict[str, str], *, exchange: str | None = None
    ) -> list[DailyRow]:
        """Один ряд за всё доступное окно — максимум `FREE_WINDOW_DAYS` суток."""
        params: dict[str, Any] = {"window": "day", "limit": FREE_WINDOW_DAYS}
        if exchange is not None:
            params["exchange"] = exchange
        out: list[DailyRow] = []
        for row in self._get(f"{asset}/{path}", params):
            ts = _day(row.get("date"))
            if ts is None:
                continue
            values = {
                ours: _num(row.get(theirs))
                for theirs, ours in mapping.items()
                if row.get(theirs) is not None
            }
            if values:
                out.append(DailyRow(ts=ts, **values))
        return out

    def collect(
        self, asset: str, exchange: str
    ) -> tuple[list[DailyRow], list[str]]:
        """Все точки одной пары «монета × площадка», склеенные по суткам.

        Отказ одной точки не отменяет остальные: тариф отдаёт разные наборы по разным
        монетам (`sol` закрыт, `xrp` открыт), и падение на первой же дыре означало бы,
        что не собрано ничего.
        """
        by_day: dict[datetime, DailyRow] = {}
        notes: list[str] = []
        plan = MARKET_WIDE if exchange == MARKET else BY_EXCHANGE
        for path, mapping in plan.items():
            try:
                rows = self.series(
                    asset, path, mapping, exchange=None if exchange == MARKET else exchange
                )
            except CryptoQuantError as err:
                notes.append(f"{asset}/{exchange}/{path}: {err}")
                continue
            for row in rows:
                prev = by_day.get(row.ts)
                by_day[row.ts] = row if prev is None else prev.merge(row)
        return [by_day[k] for k in sorted(by_day)], notes

    def health(self) -> Health:
        now = datetime.now(UTC)
        if not self.enabled:
            return Health(status="down", detail="CRYPTOQUANT_API_KEY не задан", checked_at=now)
        try:
            rows = self.series(
                "btc",
                "market-data/liquidations",
                BY_EXCHANGE["market-data/liquidations"],
                exchange="all_exchange",
            )
        except CryptoQuantError as err:
            return Health(status="down", detail=str(err), checked_at=now)
        # Окно тарифа — 30 суток; заметно меньше означает, что источник отдаёт неполно,
        # и сбор в этот день будет с дырой. Это «деградация», а не отказ.
        status = "ok" if len(rows) >= FREE_WINDOW_DAYS - 2 else "degraded"
        return Health(status=status, detail=f"суток в окне: {len(rows)}", checked_at=now)


def make_feed(
    env: Any = None, *, transport: Transport | None = None, quota: Any = None, pause_s: float = 0.0
) -> CryptoQuantFeed:
    if env is None:
        from lab.config.env import environment

        env = environment()
    return CryptoQuantFeed(
        env.get("CRYPTOQUANT_API_KEY", ""), transport=transport, quota=quota, pause_s=pause_s
    )


def plan_series(config: CryptoQuantConfig) -> list[tuple[str, str]]:
    """Пары «монета × площадка» для сбора, плюс рыночный ряд по каждой монете."""
    out: list[tuple[str, str]] = []
    for asset in config.assets:
        for exchange in config.exchanges:
            out.append((asset, exchange))
        out.append((asset, MARKET))
    return out


__all__ = [
    "BASE_URL",
    "BY_EXCHANGE",
    "FEED_ID",
    "FREE_WINDOW_DAYS",
    "MARKET_WIDE",
    "CollectReport",
    "CryptoQuantConfig",
    "CryptoQuantError",
    "CryptoQuantFeed",
    "load_cryptoquant",
    "make_feed",
    "plan_series",
]
