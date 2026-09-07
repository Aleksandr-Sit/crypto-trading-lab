"""Горячая перезагрузка конфигов (R30i.4, решение §16).

`ConfigReloader.reload(by)` сравнивает содержимое `config/*.yaml` со снимком, зовёт
`core.risk.reload(by)` (лимиты и раскладка) и пишет изменение в журнал `config_changes`:
кто, когда, какой файл, отпечатки до и после. Первый вызов только снимает состояние —
изменением считается разница со снимком, а не сам факт запуска.

Сломанный конфиг не применяется и не роняет процесс: `applied=False` и текст ошибки —
работает старая конфигурация.

Сигнал: `install_sighup(reloader)` вешает SIGHUP на перезагрузку (в контейнере —
`docker compose kill -s HUP worker`); из бота/CLI — `reloader.reload(by="operator")`.
"""

from __future__ import annotations

import hashlib
import logging
import signal
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab.core.risk import ConfigChange

log = logging.getLogger(__name__)

RELOAD_PATTERN = "*.yaml"


@dataclass
class ReloadReport:
    applied: bool
    changed: list[str] = field(default_factory=list)
    error: str | None = None
    changes: list[ConfigChange] = field(default_factory=list)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


class ConfigReloader:
    def __init__(
        self,
        risk: Any = None,
        *,
        config_dir: Path | str | None = None,
        session_factory: Callable[[], Any] | None = None,
        sink: Callable[[ConfigChange], Any] | None = None,
        files: Iterable[str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        from lab.config import CONFIG_DIR

        self.risk = risk
        self.dir = Path(config_dir or CONFIG_DIR)
        self._sf = session_factory  # фабрика контекста сессии для журнала config_changes
        self._sink = sink
        self._files = tuple(files) if files else None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._snapshot: dict[str, str] | None = None

    def _current(self) -> dict[str, str]:
        paths = (
            [self.dir / name for name in self._files]
            if self._files
            else sorted(self.dir.glob(RELOAD_PATTERN))
        )
        return {p.name: _digest(p) for p in paths if p.exists()}

    def reload(self, by: str = "system") -> ReloadReport:
        now = self._current()
        before = self._snapshot
        self._snapshot = now
        changed = (
            []
            if before is None
            else sorted(name for name in now | (before or {}) if now.get(name) != before.get(name))
        )
        changes = [
            ConfigChange(
                who=by,
                when=self._clock(),
                path=str(self.dir / name),
                diff={"before": (before or {}).get(name, ""), "after": now.get(name, "")},
            )
            for name in changed
        ]
        error = None
        if self.risk is not None:
            try:
                result = self.risk.reload(by)
                error = getattr(result, "error", None)
            except Exception as err:  # noqa: BLE001 — сломанный конфиг не роняет процесс
                error = str(err)
                log.error("Перезагрузка конфигов отклонена: %s", err)
        if error is None:
            for change in changes:
                self._record(change)
            if changed:
                log.info("Конфиги перезагружены (%s): %s", by, ", ".join(changed))
        return ReloadReport(
            applied=error is None, changed=changed, error=error, changes=changes
        )

    def _record(self, change: ConfigChange) -> None:
        if self._sink is not None:
            self._sink(change)
        if self._sf is None:
            return
        from lab.core.risk import DbConfigLog

        with self._sf() as session:
            DbConfigLog(session).record(change)

    def install_sighup(self, by: str = "sighup") -> None:
        """SIGHUP → перезагрузка конфигов (только в главном потоке процесса)."""

        def handler(_signum, _frame) -> None:
            report = self.reload(by=by)
            if not report.applied:
                log.error("SIGHUP: конфиги не применены — %s", report.error)

        try:
            signal.signal(signal.SIGHUP, handler)
        except (ValueError, AttributeError, OSError) as err:  # не главный поток / нет SIGHUP
            log.info("SIGHUP не установлен: %s", err)


__all__ = ["ConfigChange", "ConfigReloader", "ReloadReport"]
