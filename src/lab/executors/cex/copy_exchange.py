"""Биржевой копитрейд Bybit/OKX — контрольная группа на малом размере (История 61).

Зачем контроль: если своя копия ончейн проигрывает биржевому копитрейду того же периода,
дело не в лидере, а в нашем исполнении.

Здесь же выбор исполнителя для любой копии (`executor_for`): в сетях DEX-исполнителя
ещё нет (тикет 09), поэтому там копия идёт в `paper`, а `live` — явный отказ.

Что дают площадки на 09.2026:
- OKX: публичные эндпоинты `copytrading/public-lead-traders` и `public-current-subpositions`
  — без ключа и без подписки;
- Bybit: лидерборда в API нет, он только в вебе. Поэтому здесь честная заглушка:
  `leaders()` отказывает и объясняет, id лидера вводится руками через `manual()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from lab.contracts import Executor, ModeLiteral
from lab.executors import registry
from lab.executors.fake import FakeExecutor
from lab.feeds import NullQuota, QuotaSink
from lab.strategies.copy.strategy import CopyStrategy, copy_manifest, slug_of

OKX_BASE = "https://www.okx.com"
BYBIT_WEB = "https://www.bybit.com/copyTrade/"


CHAIN_VENUES = frozenset({"solana", "evm", "ethereum", "base", "bnb", "ton"})


def executor_for(
    venue: str, *, mode: ModeLiteral = "paper", mark_price: Decimal = Decimal("1")
) -> Executor:
    """Исполнитель для копии. Сети до тикета 09 — бумажный стенд, `live` — отказ."""
    if venue in registry.all():
        factory = registry.get(venue)
        try:
            return factory(mode=mode)  # type: ignore[call-arg]
        except TypeError:
            return factory()
    if venue in CHAIN_VENUES:
        if mode == "live":
            raise NotImplementedError(
                f"{venue}: DEX-исполнителя ещё нет (тикет 09) — копирование в сетях "
                "работает только в paper"
            )
        return FakeExecutor(mark_price=mark_price)
    raise KeyError(f"исполнитель {venue!r} не зарегистрирован")


class ManualEntryRequired(RuntimeError):
    """Данных лидеров в API площадки нет — id вводится руками."""


@dataclass(frozen=True)
class LeadTrader:
    leader_id: str
    venue: str
    nickname: str = ""
    pnl_pct: Decimal = Decimal(0)
    win_rate_pct: Decimal = Decimal(0)
    aum_usd: Decimal = Decimal(0)
    followers: int = 0
    days: int = 0
    manual: bool = False


def _pct(value: Any) -> Decimal:
    """Доли площадки (`0.42`) — в проценты, как во всех наших метриках."""
    return (Decimal(str(value or 0)) * 100).normalize()


class OkxLeadTraders:
    """Публичные лидеры OKX: ключ не нужен, поэтому это ещё и источник кандидатов."""

    venue = "okx"
    feed_id = "okx"

    def __init__(self, transport, *, base: str = OKX_BASE, quota: QuotaSink | None = None) -> None:
        self.transport = transport
        self.base = base
        self.quota = quota or NullQuota()

    def leaders(self, *, inst_type: str = "SWAP", limit: int = 20) -> list[LeadTrader]:
        self.quota.use(self.feed_id, 1)
        raw = self.transport.get(
            f"{self.base}/api/v5/copytrading/public-lead-traders",
            params={"instType": inst_type, "limit": limit},
        )
        rows = (raw or {}).get("data") or []
        if rows and isinstance(rows[0], dict) and "ranks" in rows[0]:
            rows = rows[0]["ranks"]
        return [
            LeadTrader(
                leader_id=str(row.get("uniqueCode", "")),
                venue=self.venue,
                nickname=str(row.get("nickName", "")),
                pnl_pct=_pct(row.get("pnlRatio")),
                win_rate_pct=_pct(row.get("winRatio")),
                aum_usd=Decimal(str(row.get("aum", 0) or 0)),
                followers=int(row.get("copyTraderNum", 0) or 0),
                days=int(row.get("leadDays", 0) or 0),
            )
            for row in rows
        ]

    def positions(self, leader_id: str, *, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        """Открытые позиции лидера — вход для событий копии."""
        self.quota.use(self.feed_id, 1)
        raw = self.transport.get(
            f"{self.base}/api/v5/copytrading/public-current-subpositions",
            params={"instType": inst_type, "uniqueCode": leader_id},
        )
        return list((raw or {}).get("data") or [])


class BybitLeaderboard:
    """Заглушка: лидерборд Bybit доступен только в вебе, id лидера вводит оператор."""

    venue = "bybit"
    web_url = BYBIT_WEB

    def leaders(self) -> list[LeadTrader]:
        raise ManualEntryRequired(
            "Bybit: лидерборд копитрейда есть только в вебе "
            f"({self.web_url}) — открой его и введи id лидера вручную (`manual(<id>)`)"
        )

    def manual(self, leader_id: str, *, nickname: str = "") -> LeadTrader:
        if not leader_id.strip():
            raise ManualEntryRequired("Bybit: нужен id лидера с веб-страницы копитрейда")
        return LeadTrader(
            leader_id=leader_id.strip(), venue=self.venue, nickname=nickname, manual=True
        )


LEADER_SOURCES = {"okx": OkxLeadTraders, "bybit": BybitLeaderboard}


def make_copy_exchange_strategy(
    venue: str,
    leader_id: str,
    *,
    instruments: list[str] | None = None,
    clock=None,
    **params: Any,
) -> CopyStrategy:
    """`copy-exchange-<venue>-<leader>` — контрольная группа: тот же код, малый размер."""
    if venue not in LEADER_SOURCES:
        raise KeyError(f"биржевой копитрейд есть только для {', '.join(LEADER_SOURCES)}")
    extra = {"control_group": True, "venue_leader_id": leader_id, "max_trade_usd": "50"}
    extra.update(params)
    manifest = copy_manifest(
        source_kind="exchange",
        slug=f"{venue}-{slug_of(leader_id)}",
        venue=venue,
        leader=leader_id,
        chain="exchange",
        instruments=instruments or ["BTC/USDT:USDT"],
        params=extra,
        description=f"Контрольная группа: биржевой копитрейд {venue}, лидер {leader_id}",
    )
    return CopyStrategy(manifest, clock=clock)


__all__ = [
    "BYBIT_WEB",
    "CHAIN_VENUES",
    "LEADER_SOURCES",
    "OKX_BASE",
    "BybitLeaderboard",
    "LeadTrader",
    "ManualEntryRequired",
    "OkxLeadTraders",
    "executor_for",
    "make_copy_exchange_strategy",
]
