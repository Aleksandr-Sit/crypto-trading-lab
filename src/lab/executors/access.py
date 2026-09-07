"""Режим ветки: торгуем или «только замер» (Истории 84, 86).

Площадка может быть закрыта не для системы вообще, а для этого IP или этого аккаунта
(Polymarket — гео-блок, Robinhood — только клиенты US с пройденным KYC). Такая ветка не
выключается: она продолжает мерить и слать сигналы, но ордера на площадку не уходят.

Флаг живёт в `system_flags` (та же таблица, что у стоп-крана), чтобы его видели все процессы:
проверка делается при старте в worker, а показывает её `/status` бота и веб-экран.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.db.models import SystemFlagRow

FLAG_PREFIX = "branch_mode:"


@dataclass(frozen=True)
class BranchMode:
    branch: str
    read_only: bool
    reason: str
    checked_at: datetime

    @property
    def label(self) -> str:
        return "только замер" if self.read_only else "торговля"

    def status_line(self) -> str:
        return f"• ветка {self.branch}: {self.label}" + (f" — {self.reason}" if self.reason else "")


def _key(branch: str) -> str:
    return f"{FLAG_PREFIX}{branch}"


def _mode(branch: str, row: SystemFlagRow) -> BranchMode:
    value = row.value or {}
    checked = value.get("checked_at")
    return BranchMode(
        branch=branch,
        read_only=bool(value.get("read_only")),
        reason=str(value.get("reason", "")),
        checked_at=datetime.fromisoformat(checked) if checked else row.updated_at,
    )


def set_branch_mode(
    session: Session,
    branch: str,
    *,
    read_only: bool,
    reason: str = "",
    by: str = "system",
    now: datetime | None = None,
) -> BranchMode:
    at = now or datetime.now(UTC)
    row = session.get(SystemFlagRow, _key(branch))
    if row is None:
        row = SystemFlagRow(key=_key(branch), value={})
        session.add(row)
    row.value = {"read_only": read_only, "reason": reason, "checked_at": at.isoformat()}
    row.updated_by = by
    row.updated_at = at
    session.flush()
    return _mode(branch, row)


def branch_mode(session: Session, branch: str) -> BranchMode | None:
    row = session.get(SystemFlagRow, _key(branch))
    return _mode(branch, row) if row is not None else None


def branch_modes(session: Session) -> list[BranchMode]:
    rows = session.scalars(
        select(SystemFlagRow).where(SystemFlagRow.key.like(f"{FLAG_PREFIX}%"))
    ).all()
    modes = [_mode(row.key[len(FLAG_PREFIX) :], row) for row in rows]
    return sorted(modes, key=lambda m: m.branch)


def is_read_only(session: Session, branch: str) -> bool:
    mode = branch_mode(session, branch)
    return bool(mode and mode.read_only)


def status_lines(session: Session) -> list[str]:
    """Строки для `/status` бота: показываем только ветки, где торговля закрыта."""
    return [m.status_line() for m in branch_modes(session) if m.read_only]


__all__ = [
    "FLAG_PREFIX",
    "BranchMode",
    "branch_mode",
    "branch_modes",
    "is_read_only",
    "set_branch_mode",
    "status_lines",
]
