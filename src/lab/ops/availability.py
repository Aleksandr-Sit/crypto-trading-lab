"""Доступность площадок с текущего IP (R33i, история 34).

При старте worker'а спрашиваем `health()` у каждого источника/исполнителя и
`check_trading_access(...)` у веток, где площадка может быть закрыта (Polymarket,
Robinhood — тикет 11). Результат — таблица «площадка → доступна / нет / гео-блок»
для баннера, `/status` бота и экрана `/feeds`.

Ни один отказ здесь не роняет старт: исключение источника — это строка таблицы,
а не падение процесса.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

GEO_MARKERS = (
    "гео",
    "geo",
    "restricted",
    "unavailable in your",
    "not available in",
    "403",
    "region",
    "регион",
)


@dataclass(frozen=True)
class VenueAvailability:
    venue: str
    available: bool
    geo_blocked: bool
    detail: str
    checked_at: datetime
    kind: str = "venue"

    @property
    def mark(self) -> str:
        if self.available:
            return "доступна"
        return "гео-блок" if self.geo_blocked else "недоступна"

    def line(self) -> str:
        detail = f" — {self.detail}" if self.detail else ""
        return f"{self.venue}: {self.mark}{detail}"


def looks_geo_blocked(detail: str) -> bool:
    low = (detail or "").lower()
    return any(marker in low for marker in GEO_MARKERS)


def check_all(
    probes: Mapping[str, Any],
    *,
    session: Any = None,
    access_checks: Iterable[Callable[[Any], Any]] = (),
    now: datetime | None = None,
) -> list[VenueAvailability]:
    """Опросить `health()` у `probes` и `check_trading_access` у веток из `access_checks`.

    `probes` — `{имя площадки: объект с health()}` (фид или исполнитель).
    `access_checks` — функции `(session) -> BranchMode` из `executors.polymarket|robinhood`.
    """
    at = now or datetime.now(UTC)
    rows: list[VenueAvailability] = []
    for venue, probe in probes.items():
        try:
            health = probe.health()
            status = str(getattr(health, "status", "ok"))
            detail = str(getattr(health, "detail", ""))
            checked = getattr(health, "checked_at", None) or at
            rows.append(
                VenueAvailability(
                    venue=venue,
                    available=status == "ok",
                    geo_blocked=status != "ok" and looks_geo_blocked(detail),
                    detail=detail,
                    checked_at=checked,
                )
            )
        except Exception as err:  # noqa: BLE001 — недоступность площадки не авария процесса
            log.warning("Площадка %s недоступна: %s", venue, err)
            rows.append(
                VenueAvailability(
                    venue=venue,
                    available=False,
                    geo_blocked=looks_geo_blocked(str(err)),
                    detail=str(err),
                    checked_at=at,
                )
            )
    for check in access_checks:
        try:
            mode = check(session)
        except Exception as err:  # noqa: BLE001
            log.warning("Проверка доступа к торговле не удалась: %s", err)
            continue
        rows.append(
            VenueAvailability(
                venue=mode.branch,
                available=not mode.read_only,
                geo_blocked=looks_geo_blocked(mode.reason),
                detail=mode.reason,
                checked_at=getattr(mode, "checked_at", at) or at,
                kind="branch",
            )
        )
    return rows


def format_availability(rows: Iterable[VenueAvailability]) -> str:
    rows = list(rows)
    if not rows:
        return "Площадки не проверялись."
    width = max(len(r.venue) for r in rows)
    lines = ["Доступность с текущего IP:"]
    for r in sorted(rows, key=lambda r: (r.available, r.venue)):
        detail = f"  {r.detail}" if r.detail else ""
        lines.append(f"  {r.venue.ljust(width)}  {r.mark}{detail}")
    return "\n".join(lines)


def default_probes(*, quota: Any = None, venues: Iterable[str] | None = None) -> dict[str, Any]:
    """Фиды площадок для проверки при старте. Всё, что не собралось (нет ключей, нет
    пакета), просто не попадает в таблицу."""
    from lab.feeds.cex import FEEDS, make_feed

    probes: dict[str, Any] = {}
    for venue in venues or FEEDS:
        try:
            probes[venue] = make_feed(venue, quota=quota)
        except Exception as err:  # noqa: BLE001
            log.info("Фид %s не собран: %s", venue, err)
    try:
        from lab.feeds.polymarket import PolymarketFeed

        probes["polymarket"] = PolymarketFeed(quota=quota)
    except Exception as err:  # noqa: BLE001
        log.info("Фид polymarket не собран: %s", err)
    return probes


def default_access_checks() -> list[Callable[[Any], Any]]:
    """Ветки, где площадка может быть закрыта: prediction (Polymarket) и rh (Robinhood)."""
    checks: list[Callable[[Any], Any]] = []
    try:
        from lab.executors.polymarket import check_trading_access as pm_access
        from lab.executors.polymarket import make_executor as pm_executor

        checks.append(lambda s: pm_access(pm_executor(), session=s))
    except Exception as err:  # noqa: BLE001
        log.info("Проверка Polymarket недоступна: %s", err)
    try:
        from lab.executors.robinhood import check_trading_access as rh_access
        from lab.executors.robinhood import make_executor as rh_executor

        checks.append(lambda s: rh_access(rh_executor(), session=s))
    except Exception as err:  # noqa: BLE001
        log.info("Проверка Robinhood недоступна: %s", err)
    return checks


__all__ = [
    "GEO_MARKERS",
    "VenueAvailability",
    "check_all",
    "default_access_checks",
    "default_probes",
    "format_availability",
    "looks_geo_blocked",
]
