"""Клиент Coinalyze: суточные ликвидации, открытый интерес и фандинг по площадкам.

**Ради чего.** Ликвидаций нет ни в архивах бирж, ни у одного бесплатного источника с
историей: Coinglass платный целиком (от $29/мес), OKX отдаёт одну последнюю запись,
CryptoQuant — окно в 30 суток. Здесь бесплатный ключ даёт около 1500 суточных точек,
то есть примерно четыре года (проверено вызовом 14.09.2026).

**Почему их надо складывать к себе.** Окно ПЛЫВЁТ: держится 1500 точек, и самый старый
день каждый день выпадает. Сегодня доступен август 2022, через год будет август 2023.
Гипотеза про ликвидации на четырёх годах от шума не отличается
(`docs/research/liquidations-2026-09-14.md`, раздел 6), и единственный способ когда-нибудь
её разрешить — не потерять то, что уже есть.

Границы, выясненные вызовом, а не по документации: ключ обязателен (без него `401`),
лимит 40 запросов в минуту, агрегата по площадкам нет — суммировать надо самим,
`convert_to_usd=true` переводит объёмы в доллары.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from lab.config import CONFIG_DIR, load_config
from lab.contracts import Health
from lab.data.daily_market import DailyRow

log = logging.getLogger(__name__)

FEED_ID = "coinalyze"
BASE_URL = "https://api.coinalyze.net/v1"
# Тариф держит около 1500 точек на ряд; просим с запасом — лишнее источник просто не отдаст.
HISTORY_DAYS = 1600
RATE_LIMIT_PER_MIN = 40

# Точка API → её колонки в наших именах. Только то, чего у нас нет глубже из других
# источников: фандинг и цены у Binance лежат с большей историей, их здесь не берём.
ENDPOINTS: dict[str, dict[str, str]] = {
    "liquidation-history": {"l": "long_liq_usd", "s": "short_liq_usd"},
    "open-interest-history": {"c": "open_interest"},
}


class CoinalyzeError(RuntimeError):
    """Источник ответил отказом или не ответил вовсе."""


class Transport(Protocol):
    def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any: ...


class UrllibTransport:
    """Живой транспорт. Отдельный класс, чтобы в тестах его подменял фейк."""

    def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read())


class CoinalyzeConfig(BaseModel):
    version: int = 1
    # «Монета → символы перпов главных площадок». Агрегата у источника нет: суммируем сами.
    markets: dict[str, list[str]] = Field(default_factory=dict)
    pause_s: float = 1.6


def load_coinalyze(path: Path | str | None = None) -> CoinalyzeConfig:
    return load_config(path or CONFIG_DIR / "coinalyze.yaml", CoinalyzeConfig)


def _num(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class CoinalyzeFeed:
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
        self._transport = transport or UrllibTransport()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.enabled:
            raise CoinalyzeError("COINALYZE_API_KEY не задан")
        if self.quota is not None:
            self.quota.use(FEED_ID, 1)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self.base_url}/{path.lstrip('/')}?{query}"
        try:
            payload = self._transport.get_json(url, headers={"api_key": self.api_key})
        except Exception as err:  # noqa: BLE001 — отказ источника не наша авария
            raise CoinalyzeError(f"{path}: {err}") from err
        if self.pause_s:
            self._sleep(self.pause_s)
        if not isinstance(payload, list):
            raise CoinalyzeError(f"{path}: неожиданный ответ {type(payload).__name__}")
        return [block for block in payload if isinstance(block, dict)]

    def series(self, symbol: str, path: str, mapping: dict[str, str], days: int) -> list[DailyRow]:
        """Один ряд одной площадки за всё доступное окно."""
        now = int(time.time())
        blocks = self._get(
            path,
            {
                "symbols": symbol,
                "interval": "daily",
                "convert_to_usd": "true",
                "from": now - days * 86400,
                "to": now,
            },
        )
        out: list[DailyRow] = []
        for block in blocks:
            for point in block.get("history") or []:
                stamp = point.get("t")
                if stamp is None:
                    continue
                ts = datetime.fromtimestamp(int(stamp), UTC).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                values = {
                    ours: _num(point.get(theirs))
                    for theirs, ours in mapping.items()
                    if point.get(theirs) is not None
                }
                if values:
                    out.append(DailyRow(ts=ts, **values))
        return out

    def collect(
        self, symbols: list[str], days: int = HISTORY_DAYS
    ) -> tuple[list[DailyRow], list[str]]:
        """Сумма по площадкам, склеенная по суткам, плюс список пропусков.

        Суммируем МЫ, потому что агрегата источник не отдаёт. Отказ одной площадки
        не отменяет остальные: пропуск попадает в отчёт, а не гасит весь проход.
        """
        totals: dict[datetime, dict[str, Decimal]] = defaultdict(dict)
        notes: list[str] = []
        for symbol in symbols:
            for path, mapping in ENDPOINTS.items():
                try:
                    rows = self.series(symbol, path, mapping, days)
                except CoinalyzeError as err:
                    notes.append(f"{symbol}/{path}: {err}")
                    continue
                for row in rows:
                    bucket = totals[row.ts]
                    for name in mapping.values():
                        value = getattr(row, name)
                        if value is not None:
                            bucket[name] = bucket.get(name, Decimal(0)) + value
        out = [DailyRow(ts=ts, **values) for ts, values in sorted(totals.items()) if values]
        return out, notes

    def health(self) -> Health:
        now = datetime.now(UTC)
        if not self.enabled:
            return Health(status="down", detail="COINALYZE_API_KEY не задан", checked_at=now)
        try:
            blocks = self._get("exchanges", {})
        except CoinalyzeError as err:
            return Health(status="down", detail=str(err), checked_at=now)
        return Health(status="ok", detail=f"площадок в справочнике: {len(blocks)}", checked_at=now)


def make_feed(
    env: Any = None, *, transport: Transport | None = None, quota: Any = None, pause_s: float = 0.0
) -> CoinalyzeFeed:
    if env is None:
        from lab.config.env import environment

        env = environment()
    return CoinalyzeFeed(
        env.get("COINALYZE_API_KEY", ""), transport=transport, quota=quota, pause_s=pause_s
    )


__all__ = [
    "BASE_URL",
    "ENDPOINTS",
    "FEED_ID",
    "HISTORY_DAYS",
    "RATE_LIMIT_PER_MIN",
    "CoinalyzeConfig",
    "CoinalyzeError",
    "CoinalyzeFeed",
    "Transport",
    "UrllibTransport",
    "load_coinalyze",
    "make_feed",
]
