"""Состояние, разделяемое между процессами: рубильник «стоп всё» и журнал изменений конфига.

Память — для тестов и одного процесса; Postgres — для worker/bot/web (таблицы миграции 0003).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.core.risk.types import ConfigChange
from lab.db.base import utcnow
from lab.db.models import ConfigChangeRow, SystemFlagRow

HALT_FLAG = "halted"


class MemoryHaltSwitch:
    def __init__(self, halted: bool = False) -> None:
        self._halted = halted
        self.history: list[tuple[str, str, datetime]] = []

    def is_halted(self) -> bool:
        return self._halted

    def halt(self, by: str) -> None:
        self._halted = True
        self.history.append(("halt", by, utcnow()))

    def resume(self, by: str) -> None:
        self._halted = False
        self.history.append(("resume", by, utcnow()))


class DbHaltSwitch:
    def __init__(self, session: Session) -> None:
        self.s = session

    def _row(self) -> SystemFlagRow | None:
        return self.s.get(SystemFlagRow, HALT_FLAG)

    def is_halted(self) -> bool:
        row = self._row()
        return bool(row is not None and row.value.get("on"))

    def _set(self, on: bool, by: str) -> None:
        row = self._row()
        if row is None:
            row = SystemFlagRow(key=HALT_FLAG, value={})
            self.s.add(row)
        row.value = {"on": on}
        row.updated_by = by
        row.updated_at = utcnow()
        self.s.flush()

    def halt(self, by: str) -> None:
        self._set(True, by)

    def resume(self, by: str) -> None:
        self._set(False, by)


class MemoryConfigLog:
    def __init__(self) -> None:
        self.changes: list[ConfigChange] = []

    def record(self, change: ConfigChange) -> None:
        self.changes.append(change)


class DbConfigLog:
    def __init__(self, session: Session) -> None:
        self.s = session

    def record(self, change: ConfigChange) -> None:
        self.s.add(
            ConfigChangeRow(who=change.who, ts=change.when, path=change.path, diff=change.diff)
        )
        self.s.flush()

    def changes(self, path: str | None = None) -> list[ConfigChange]:
        stmt = select(ConfigChangeRow).order_by(ConfigChangeRow.ts, ConfigChangeRow.id)
        if path is not None:
            stmt = stmt.where(ConfigChangeRow.path == path)
        return [
            ConfigChange(who=r.who, when=r.ts, path=r.path, diff=dict(r.diff))
            for r in self.s.scalars(stmt).all()
        ]


def config_diff(old: dict[str, Any], new: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Плоский diff двух словарей конфига: {"groups.cex.share_pct": {"old": "40", "new": "35"}}."""
    out: dict[str, Any] = {}
    for key in sorted(set(old) | set(new)):
        path = f"{prefix}{key}"
        a, b = old.get(key), new.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            out.update(config_diff(a, b, f"{path}."))
        elif a != b:
            out[path] = {"old": a, "new": b}
    return out
