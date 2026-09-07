"""Календари предстоящих минтов (История 74).

Публичных API у календарей нет — только парсинг. Разбираем разметку `application/ld+json`
(schema.org Event), которую отдают nftcalendar.io и подобные: она устойчивее вёрстки и
не требует внешней библиотеки. Не разобралось — источник честно говорит `degraded`,
а не отдаёт пустую ленту как «минтов нет».
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from lab.contracts import Health, NftMint
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.chains.transport import HttpTransport, HttpxTransport
from lab.feeds.nft.config import NftConfig, load_nft

LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


class MintCalendar(Protocol):
    """Источник предстоящих минтов: launchpad площадки или парсер календаря."""

    id: str

    def mints(self) -> Sequence[NftMint]: ...

    def health(self) -> Health: ...


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _price(offers: Any) -> Decimal | None:
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None
    try:
        return Decimal(str(offers.get("price")))
    except Exception:  # noqa: BLE001 — цена в календаре бывает текстом «TBA»
        return None


def parse_ld_events(html: str, *, source: str, chain: str = "") -> list[NftMint]:
    """Разбор schema.org Event из HTML календаря. Тест кормит сюда сохранённую страницу."""
    out: list[NftMint] = []
    for block in LD_JSON.findall(html or ""):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                items = items + [g for g in graph if isinstance(g, dict)]
                continue
            if str(item.get("@type", "")).lower() != "event":
                continue
            name = str(item.get("name", "")).strip()
            organizer = item.get("organizer")
            if not name:
                continue
            out.append(
                NftMint(
                    collection=name,
                    chain=chain or str(item.get("location", {}).get("name", "")) or "",
                    market=source,
                    starts_at=_parse_ts(item.get("startDate")),
                    price=_price(item.get("offers")),
                    creator=organizer.get("name") if isinstance(organizer, dict) else None,
                    meta={"source": source, "url": str(item.get("url", ""))},
                )
            )
    return out


class HtmlCalendar:
    """Парсер календаря по HTML. `id` — из `config/nft.yaml`, секция `calendars`."""

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        id: str = "nftcalendar",
        url: str = "",
        chain: str = "",
        quota: QuotaSink | None = None,
        config: NftConfig | None = None,
    ) -> None:
        cfg = config or load_nft()
        spec = next((c for c in cfg.calendars if c.id == id), None)
        self.id = id
        self.url = url or (spec.url if spec else "")
        self.enabled = spec.enabled if spec else True
        self.chain = chain
        self.transport = transport or HttpxTransport()
        self.quota = quota or NullQuota()
        self._last_error = ""
        self._last_count = 0

    def mints(self) -> Sequence[NftMint]:
        if not self.enabled or not self.url:
            return []
        self.quota.use(self.id, 1)
        try:
            page = self.transport.get(self.url)
        except Exception as exc:  # noqa: BLE001 — календарь может лечь, лента не должна
            self._last_error = str(exc)
            return []
        html = page if isinstance(page, str) else str(page)
        mints = parse_ld_events(html, source=self.id, chain=self.chain)
        self._last_count = len(mints)
        self._last_error = "" if mints else "разметка календаря не разобралась"
        return mints

    def health(self) -> Health:
        now = datetime.now(UTC)
        if not self.enabled:
            return Health(status="down", detail=f"{self.id}: выключён", checked_at=now)
        if self._last_error:
            return Health(
                status="degraded", detail=f"{self.id}: {self._last_error}", checked_at=now
            )
        return Health(
            status="ok", detail=f"{self.id}: {self._last_count} минтов", checked_at=now
        )


class MarketLaunchpad:
    """Launchpad площадки как источник ленты: Magic Eden отдаёт его через `upcoming()`."""

    def __init__(self, market: Any, *, id: str | None = None) -> None:
        self.market = market
        self.id = id or f"{getattr(market, 'market', 'market')}_launchpad"

    def mints(self) -> Sequence[NftMint]:
        try:
            return list(self.market.upcoming())
        except Exception:  # noqa: BLE001 — площадка легла, лента остаётся из других источников
            return []

    def health(self) -> Health:
        return self.market.health()
