"""Ежедневная резервная копия базы и конфигов (R25.3, история 36; расписание — 03:00).

Один архив `BACKUP_DIR/lab-YYYY-MM-DD.tar.gz`: дамп Postgres (`pg_dump`, plain SQL) плюс
каталог `config/`. Ротация — старше `keep_days` суток удаляется. Восстановление —
`scripts/restore.sh <архив>` (проверяет доступность базы и разворачивает дамп).

Ошибка бэкапа не роняет worker: возвращается результат с `error`, а карточка `alert`
отправляется вызывающим заданием.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BACKUP_JOB = "backup"
DEFAULT_KEEP_DAYS = 14
PREFIX = "lab-"
SUFFIX = ".tar.gz"


@dataclass
class BackupResult:
    path: Path | None
    contents: list[str] = field(default_factory=list)
    rotated: list[Path] = field(default_factory=list)
    size_bytes: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def libpq_dsn(url: str) -> str:
    """SQLAlchemy-URL → строка подключения для pg_dump (`postgresql://…`)."""
    if "+" in url.split("://", 1)[0]:
        scheme, rest = url.split("://", 1)
        url = f"{scheme.split('+', 1)[0]}://{rest}"
    return url


def _date_of(path: Path) -> datetime | None:
    stem = path.name[len(PREFIX) : -len(SUFFIX)]
    try:
        return datetime.strptime(stem, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def rotate(
    dest: Path, *, keep_days: int = DEFAULT_KEEP_DAYS, now: datetime | None = None
) -> list[Path]:
    """Удалить копии старше `keep_days` суток. Возвращает удалённые файлы."""
    at = now or datetime.now(UTC)
    edge = at - timedelta(days=keep_days)
    removed: list[Path] = []
    for path in sorted(Path(dest).glob(f"{PREFIX}*{SUFFIX}")):
        made = _date_of(path)
        if made is not None and made < edge:
            path.unlink()
            removed.append(path)
    return removed


def backup(
    *,
    dest: Path | str | None = None,
    database_url: str | None = None,
    config_dir: Path | str | None = None,
    keep_days: int = DEFAULT_KEEP_DAYS,
    now: datetime | None = None,
    run: Callable[..., Any] = subprocess.run,
    env: dict[str, str] | None = None,
) -> BackupResult:
    """Дамп базы + конфиги в один архив с датой; затем ротация."""
    at = now or datetime.now(UTC)
    environ = env if env is not None else dict(os.environ)
    dest_path = Path(dest or environ.get("BACKUP_DIR") or "backups")
    dest_path.mkdir(parents=True, exist_ok=True)
    archive = dest_path / f"{PREFIX}{at:%Y-%m-%d}{SUFFIX}"

    from lab.config import CONFIG_DIR
    from lab.db import database_url as db_url

    configs = Path(config_dir) if config_dir else CONFIG_DIR
    url = database_url or environ.get("DATABASE_URL") or db_url()

    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "db.sql"
        cmd = ["pg_dump", "--no-owner", "--format=plain", "-f", str(dump), libpq_dsn(url)]
        try:
            run(cmd, check=True)
        except Exception as err:  # noqa: BLE001 — падение pg_dump не роняет worker
            log.error("Бэкап: pg_dump не отработал: %s", err)
            return BackupResult(path=None, error=f"pg_dump: {err}")
        if not dump.exists():
            return BackupResult(path=None, error="pg_dump не создал файл дампа")
        names: list[str] = []
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(dump, arcname="db.sql")
            names.append("db.sql")
            for path in sorted(Path(configs).glob("*")):
                if path.is_file():
                    tar.add(path, arcname=f"config/{path.name}")
                    names.append(f"config/{path.name}")

    removed = rotate(dest_path, keep_days=keep_days, now=at)
    size = archive.stat().st_size
    log.info("Бэкап: %s (%s байт), удалено старых копий: %s", archive, size, len(removed))
    return BackupResult(path=archive, contents=names, rotated=removed, size_bytes=size)


def backup_job(
    *,
    dest: Path | str | None = None,
    keep_days: int | None = None,
    alert: Callable[[str, dict[str, Any]], Any] | None = None,
):
    """Задание `backup` (03:00 по `schedule.yaml`); ошибка → карточка `alert`."""
    from lab.ops.scheduler import Job

    keep = keep_days or int(os.environ.get("BACKUP_KEEP_DAYS") or DEFAULT_KEEP_DAYS)

    def run_backup() -> BackupResult:
        result = backup(dest=dest, keep_days=keep)
        if not result.ok and alert is not None:
            alert(
                "alert",
                {
                    "service": "backup",
                    "detail": f"Резервная копия не сделана: {result.error}",
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        return result

    return Job(id=BACKUP_JOB, func=run_backup, description="Резервная копия базы и конфигов")


def disk_free_mb(path: Path | str) -> int:
    return shutil.disk_usage(str(path)).free // (1024 * 1024)


__all__ = [
    "BACKUP_JOB",
    "DEFAULT_KEEP_DAYS",
    "BackupResult",
    "backup",
    "backup_job",
    "disk_free_mb",
    "libpq_dsn",
    "rotate",
]
