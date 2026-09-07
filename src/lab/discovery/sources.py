"""Источники кандидатов (R15): семь лент за одним интерфейсом `CandidateSource`.

Ни один конструктор не ходит в сеть — клиенты создаются лениво в `fetch()`, поэтому
`default_sources()` безопасен в тестах и при старте worker'а. Отказ ленты не роняет
прогон: `Discovery.scan` ловит исключение и пишет его в `ScanResult.errors`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from lab.contracts import Health
from lab.discovery.config import (
    DiscoveryConfig,
    GithubConfig,
    HyperliquidConfig,
    NftLaunchpadConfig,
    OkxLeadConfig,
    PolymarketConfig,
    SeedConfig,
    SmartMoneyConfig,
    load_discovery,
)
from lab.discovery.types import CandidateSpec, bucket
from lab.feeds.chains.transport import HttpTransport, HttpxTransport
from lab.feeds.quota import NullQuota, QuotaSink

GITHUB_SEARCH = "https://api.github.com/search/repositories"
ROOT = Path(__file__).resolve().parents[3]


class BaseSource:
    """Общее для лент: журнал последней ошибки и `health()` поверх него."""

    id = "source"

    def __init__(self, *, quota: QuotaSink | None = None) -> None:
        self.quota = quota or NullQuota()
        self._error = ""
        self._detail = ""
        self._count = 0

    def fetch(self) -> Sequence[CandidateSpec]:  # pragma: no cover — переопределяют
        raise NotImplementedError

    def health(self) -> Health:
        now = datetime.now(UTC)
        if self._error:
            return Health(status="degraded", detail=f"{self.id}: {self._error}", checked_at=now)
        detail = self._detail or f"{self._count} кандидатов"
        return Health(status="ok", detail=f"{self.id}: {detail}", checked_at=now)

    def _ok(self, specs: list[CandidateSpec], detail: str = "") -> list[CandidateSpec]:
        self._error = ""
        self._detail = detail
        self._count = len(specs)
        return specs


# -- лидеры бирж ------------------------------------------------------------------------


class OkxLeadSource(BaseSource):
    """Публичные lead traders OKX — ключ не нужен (`executors.cex.copy_exchange`)."""

    id = "okx_lead"

    def __init__(
        self,
        client: Any = None,
        *,
        config: OkxLeadConfig | None = None,
        transport: HttpTransport | None = None,
        quota: QuotaSink | None = None,
    ) -> None:
        super().__init__(quota=quota)
        self.config = config or OkxLeadConfig()
        self._client = client
        self._transport = transport

    def _leaders(self) -> Any:
        if self._client is None:
            from lab.executors.cex.copy_exchange import OkxLeadTraders

            self._client = OkxLeadTraders(
                self._transport or HttpxTransport(), quota=self.quota
            )
        return self._client

    def fetch(self) -> list[CandidateSpec]:
        rows = self._leaders().leaders(inst_type=self.config.inst_type, limit=self.config.limit)
        out = []
        for row in rows:
            if row.days and row.days < self.config.min_days:
                continue
            if row.win_rate_pct and row.win_rate_pct < self.config.min_win_rate_pct:
                continue
            out.append(
                CandidateSpec(
                    kind="trader",
                    ref=row.leader_id,
                    venue=row.venue,
                    branch="copy",
                    source=self.id,
                    source_url="https://www.okx.com/copy-trading",
                    note=row.nickname,
                    facts={
                        "win_rate_pct": bucket(row.win_rate_pct, 10),
                        "pnl_pct": bucket(row.pnl_pct, 50),
                        "followers": bucket(row.followers, 100),
                        "days": bucket(row.days, 30),
                    },
                )
            )
        return self._ok(out)


class HyperliquidLeaderSource(BaseSource):
    """Лидерборд Hyperliquid: community-эндпоинт, а когда он молчит — ручной список."""

    id = "hyperliquid"

    def __init__(
        self,
        feed: Any = None,
        *,
        config: HyperliquidConfig | None = None,
        transport: HttpTransport | None = None,
        quota: QuotaSink | None = None,
    ) -> None:
        super().__init__(quota=quota)
        self.config = config or HyperliquidConfig()
        self._feed = feed
        self._transport = transport

    def _leaderboard(self) -> list[dict[str, Any]]:
        if self._feed is None:
            from lab.feeds.chains.hyperliquid_user import HyperliquidUserFeed

            self._feed = HyperliquidUserFeed(
                self._transport or HttpxTransport(), quota=self.quota
            )
        rows = self._feed.leaderboard()
        return [r for r in rows if isinstance(r, dict)]

    def fetch(self) -> list[CandidateSpec]:
        try:
            rows = self._leaderboard()
        except Exception as err:  # noqa: BLE001 — неофициальный эндпоинт, фолбэк обязателен
            return self._manual(f"лидерборд недоступен ({err})")
        if not rows:
            return self._manual("лидерборд пуст")
        out = [spec for row in rows[: self.config.limit] if (spec := self._spec(row)) is not None]
        if not out:
            return self._manual("в ответе нет адресов")
        return self._ok(out)

    def _spec(self, row: Mapping[str, Any]) -> CandidateSpec | None:
        address = str(row.get("ethAddress") or row.get("user") or row.get("address") or "")
        if not address:
            return None
        pnl = _window_value(row.get("windowPerformances"), "month", "pnl")
        return CandidateSpec(
            kind="trader",
            ref=address,
            venue="hyperliquid",
            chain="hyperliquid_user",
            branch="copy",
            source=self.id,
            source_url="https://app.hyperliquid.xyz/leaderboard",
            note=str(row.get("displayName") or ""),
            facts={
                "account_value": bucket(row.get("accountValue"), 10_000),
                "pnl_month": bucket(pnl, 10_000),
            },
        )

    def _manual(self, why: str) -> list[CandidateSpec]:
        out = [
            CandidateSpec(
                kind="trader",
                ref=address,
                venue="hyperliquid",
                chain="hyperliquid_user",
                branch="copy",
                source=self.id,
                source_url="https://app.hyperliquid.xyz/leaderboard",
                note=f"ручной список: {why}",
                priority="user",
                facts={"manual": True},
            )
            for address in self.config.manual
        ]
        self._error = f"{why} — взят ручной список ({len(out)})"
        self._count = len(out)
        return out


class PolymarketLeaderSource(BaseSource):
    """`/v1/leaderboard` Polymarket через `feeds.polymarket` (чтение без ключа)."""

    id = "polymarket"

    def __init__(
        self,
        feed: Any = None,
        *,
        config: PolymarketConfig | None = None,
        transport: HttpTransport | None = None,
        quota: QuotaSink | None = None,
    ) -> None:
        super().__init__(quota=quota)
        self.config = config or PolymarketConfig()
        self._feed = feed
        self._transport = transport

    def _leaders(self) -> Any:
        if self._feed is None:
            from lab.feeds.polymarket import PolymarketFeed

            self._feed = PolymarketFeed(self._transport, quota=self.quota)
        return self._feed

    def fetch(self) -> list[CandidateSpec]:
        rows = self._leaders().leaderboard(
            window=self.config.window, limit=self.config.limit, order_by=self.config.order_by
        )
        out = [
            CandidateSpec(
                kind="wallet",
                ref=row.address,
                venue="polymarket",
                chain="polygon",
                branch="prediction",
                source=self.id,
                source_url="https://polymarket.com/leaderboard",
                note=row.name,
                facts={
                    "rank": bucket(row.rank, 5),
                    "pnl_usd": bucket(row.pnl_usd, 10_000),
                    "volume_usd": bucket(row.volume_usd, 100_000),
                },
            )
            for row in rows
        ]
        return self._ok(out)


# -- смарт-мани и код -------------------------------------------------------------------


class SmartMoneySource(BaseSource):
    """Смарт-мани фиды (Cielo, Dune) — под квотой и только при заполненном ключе."""

    id = "smart_money"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        config: SmartMoneyConfig | None = None,
        quota: QuotaSink | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(quota=quota)
        self.config = config or SmartMoneyConfig()
        self._transport = transport
        self.env = env if env is not None else os.environ

    def _http(self) -> HttpTransport:
        if self._transport is None:
            self._transport = HttpxTransport()
        return self._transport

    def fetch(self) -> list[CandidateSpec]:
        out: list[CandidateSpec] = []
        skipped: list[str] = []
        for provider in self.config.providers:
            key = self.env.get(provider.key_env, "") if provider.key_env else ""
            if provider.key_env and not key:
                skipped.append(f"{provider.id}: нет {provider.key_env}")
                continue
            self.quota.use(provider.id, provider.weight)
            raw = self._http().get(provider.url, headers=_key_headers(provider.id, key))
            for row in _rows(raw):
                address = str(row.get(provider.wallet_field) or "")
                if not address:
                    continue
                out.append(
                    CandidateSpec(
                        kind="wallet",
                        ref=address,
                        venue=provider.id,
                        chain=str(row.get("chain") or provider.chain) or None,
                        branch="meme",
                        source=self.id,
                        source_url=provider.url,
                        note=str(row.get("label") or row.get("name") or ""),
                        facts={
                            "pnl_usd": bucket(row.get("pnl_usd") or row.get("pnl"), 10_000),
                            "provider": provider.id,
                        },
                    )
                )
        specs = self._ok(out)
        if skipped:
            self._error = "; ".join(skipped)
        return specs


class GithubSource(BaseSource):
    """Публичные стратегии на GitHub по ключевым словам (поиск без ключа)."""

    id = "github"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        config: GithubConfig | None = None,
        quota: QuotaSink | None = None,
    ) -> None:
        super().__init__(quota=quota)
        self.config = config or GithubConfig()
        self._transport = transport

    def _http(self) -> HttpTransport:
        if self._transport is None:
            self._transport = HttpxTransport()
        return self._transport

    def fetch(self) -> list[CandidateSpec]:
        out: list[CandidateSpec] = []
        seen: set[str] = set()
        for query in self.config.queries:
            self.quota.use(self.id, 1)
            raw = self._http().get(
                GITHUB_SEARCH,
                params={
                    "q": f"{query} stars:>={self.config.min_stars}",
                    "sort": "stars",
                    "per_page": self.config.limit,
                },
            )
            for item in _items(raw):
                name = str(item.get("full_name") or "")
                if not name or name in seen:
                    continue
                seen.add(name)
                out.append(
                    CandidateSpec(
                        kind="strategy",
                        ref=name,
                        venue="github",
                        branch=self.config.branch,
                        source=self.id,
                        source_url=str(item.get("html_url") or f"https://github.com/{name}"),
                        note=str(item.get("description") or "")[:200],
                        facts={
                            "stars": bucket(item.get("stargazers_count"), 50),
                            "pushed": str(item.get("pushed_at") or "")[:7],
                            "query": query,
                        },
                    )
                )
        return self._ok(out)


# -- NFT и стартовый список -------------------------------------------------------------


class NftLaunchpadSource(BaseSource):
    """Magic Eden launchpad + календари минтов (`feeds.nft.MarketLaunchpad`/`HtmlCalendar`)."""

    id = "nft_launchpad"

    def __init__(
        self,
        feeds: Sequence[Any] | None = None,
        *,
        config: NftLaunchpadConfig | None = None,
        transport: Any = None,
        quota: QuotaSink | None = None,
    ) -> None:
        super().__init__(quota=quota)
        self.config = config or NftLaunchpadConfig()
        self._feeds = list(feeds) if feeds is not None else None
        self._transport = transport

    def _lenta(self) -> list[Any]:
        if self._feeds is None:
            from lab.feeds.nft import HtmlCalendar, MarketLaunchpad, make_market

            feeds: list[Any] = []
            for market in self.config.markets:
                feeds.append(MarketLaunchpad(make_market(market, self._transport)))
            for calendar in self.config.calendars:
                feeds.append(HtmlCalendar(self._transport, id=calendar))
            self._feeds = feeds
        return self._feeds

    def fetch(self) -> list[CandidateSpec]:
        out: list[CandidateSpec] = []
        broken: list[str] = []
        for feed in self._lenta():
            feed_id = getattr(feed, "id", "nft")
            try:
                mints = list(feed.mints())
            except Exception as err:  # noqa: BLE001 — одна лента легла, остальные работают
                broken.append(f"{feed_id}: {err}")
                continue
            for mint in mints[: self.config.limit]:
                out.append(
                    CandidateSpec(
                        kind="mint",
                        ref=mint.collection,
                        venue=mint.market or feed_id,
                        chain=mint.chain or None,
                        branch="nft",
                        source=self.id,
                        source_url=str((mint.meta or {}).get("url", "")),
                        note=mint.creator or "",
                        facts={
                            "starts_at": mint.starts_at.date().isoformat()
                            if mint.starts_at
                            else None,
                            "price": str(mint.price) if mint.price is not None else None,
                            "supply": mint.supply,
                        },
                    )
                )
        specs = self._ok(out)
        if broken:
            self._error = "; ".join(broken)
        return specs


class SeedFileSource(BaseSource):
    """`candidates/seed.md` (формат `candidates-seed/v1`) — стартовый список от таска 13."""

    id = "seed"
    FORMAT = "candidates-seed/v1"

    def __init__(self, path: Path | str | None = None, *, config: SeedConfig | None = None) -> None:
        super().__init__()
        self.config = config or SeedConfig()
        self.path = Path(path) if path else ROOT / self.config.path

    def fetch(self) -> list[CandidateSpec]:
        if not self.path.is_file():
            self._error = f"{self.path} не найден"
            return []
        text = self.path.read_text(encoding="utf-8")
        header = _front_matter(text)
        if header.get("format") != self.FORMAT:
            self._error = f"чужой формат файла: {header.get('format')!r}"
            return []
        out: list[CandidateSpec] = []
        for block in re.findall(r"```yaml\n(.*?)```", text, flags=re.S):
            try:
                raw = yaml.safe_load(block)
            except yaml.YAMLError as err:
                self._error = f"блок не разобран: {err}"
                continue
            if not isinstance(raw, dict) or not raw.get("ref"):
                continue
            out.append(
                CandidateSpec(
                    kind=str(raw.get("kind") or "strategy"),
                    ref=str(raw["ref"]),
                    venue=str(raw.get("venue") or "manual"),
                    chain=str(raw["chain"]) if raw.get("chain") else None,
                    branch=str(raw.get("branch") or ""),
                    source=self.id,
                    source_url=str(raw.get("source_url") or ""),
                    note=str(raw.get("note") or "")[:400],
                    priority=str(raw.get("priority") or "auto"),
                    facts={"seed": str(raw.get("found_at") or "")},
                )
            )
        return self._ok(out, detail=f"{self.path.name}: {len(out)} кандидатов")


# -- сборка -----------------------------------------------------------------------------


def default_sources(
    *,
    config: DiscoveryConfig | None = None,
    quota: QuotaSink | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Any]:
    """Все включённые в `config/discovery.yaml` ленты. Сеть не трогается до `fetch()`."""
    cfg = config or load_discovery()
    s = cfg.sources
    built: list[Any] = [
        OkxLeadSource(config=s.okx_lead, quota=quota),
        HyperliquidLeaderSource(config=s.hyperliquid, quota=quota),
        PolymarketLeaderSource(config=s.polymarket, quota=quota),
        SmartMoneySource(config=s.smart_money, quota=quota, env=env),
        GithubSource(config=s.github, quota=quota),
        NftLaunchpadSource(config=s.nft_launchpad, quota=quota),
        SeedFileSource(config=s.seed),
    ]
    enabled = {
        "okx_lead": s.okx_lead.enabled,
        "hyperliquid": s.hyperliquid.enabled,
        "polymarket": s.polymarket.enabled,
        "smart_money": s.smart_money.enabled,
        "github": s.github.enabled,
        "nft_launchpad": s.nft_launchpad.enabled,
        "seed": s.seed.enabled,
    }
    return [source for source in built if enabled.get(source.id, True)]


def _key_headers(provider_id: str, key: str) -> dict[str, str]:
    if not key:
        return {}
    if provider_id == "dune":
        return {"X-Dune-API-Key": key}
    return {"X-API-KEY": key}


def _rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        for key in ("items", "data", "rows", "result"):
            value = raw.get(key)
            if isinstance(value, dict):
                value = value.get("rows")
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        items = raw.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    return _rows(raw)


def _window_value(windows: Any, window: str, field: str) -> Any:
    """`windowPerformances: [["month", {"pnl": "...", "roi": "..."}], ...]`."""
    if not isinstance(windows, list):
        return None
    for item in windows:
        if isinstance(item, list | tuple) and len(item) == 2 and item[0] == window:
            data = item[1]
            if isinstance(data, dict):
                return data.get(field)
    return None


def _front_matter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    raw = yaml.safe_load(text[3:end]) or {}
    return raw if isinstance(raw, dict) else {}


__all__ = [
    "BaseSource",
    "GithubSource",
    "HyperliquidLeaderSource",
    "NftLaunchpadSource",
    "OkxLeadSource",
    "PolymarketLeaderSource",
    "SeedFileSource",
    "SmartMoneySource",
    "default_sources",
]
