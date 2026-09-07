"""Эксплуатация (R33i, R25.3, R32i.2, R30i.4, R31i.1): доступность, бэкап, watchdog,
перезагрузка конфигов, суточная сверка."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from lab.contracts import Fill, Health
from lab.executors.access import BranchMode
from lab.ops.availability import check_all, format_availability
from lab.ops.backup import backup, rotate
from lab.ops.jobs.reconcile import reconcile_all
from lab.ops.reload import ConfigReloader
from lab.ops.watchdog import Watchdog, beat

NOW = datetime(2026, 9, 7, 3, 0, tzinfo=UTC)


class FakeFeed:
    def __init__(self, status: str, detail: str = "") -> None:
        self._h = Health(status=status, detail=detail, checked_at=NOW)

    def health(self) -> Health:
        return self._h


class Boom:
    def health(self):
        raise ConnectionError("нет маршрута")


@pytest.fixture
def scope(session):
    @contextmanager
    def _scope():
        yield session

    return _scope


# -- доступность площадок (R33i) ---------------------------------------------------------


def test_check_all_marks_geo_block_and_survives_failure():
    rows = check_all(
        {
            "bybit": FakeFeed("down", "гео-блок: 403 restricted location"),
            "binance": FakeFeed("ok"),
            "okx": Boom(),
        },
        now=NOW,
    )
    by = {r.venue: r for r in rows}
    assert by["bybit"].available is False and by["bybit"].geo_blocked is True
    assert by["binance"].available is True
    assert by["okx"].available is False and "нет маршрута" in by["okx"].detail
    table = format_availability(rows)
    assert "bybit" in table and "гео-блок" in table


def test_check_all_records_branch_mode_from_trading_access(scope, session):
    def access(session_):
        return BranchMode(
            branch="prediction", read_only=True, reason="торговля закрыта", checked_at=NOW
        )

    rows = check_all({}, session=session, access_checks=[access], now=NOW)
    closed = [r for r in rows if r.venue == "prediction"]
    assert closed and closed[0].available is False and "торговля закрыта" in closed[0].detail


# -- watchdog (R32i.2) -------------------------------------------------------------------


def test_watchdog_alerts_after_five_minutes_of_silence(scope, session):
    cards: list[tuple[str, dict]] = []
    dog = Watchdog(scope, alert=lambda kind, payload: cards.append((kind, payload)))
    beat(session, "worker", now=NOW)
    session.flush()

    assert dog.check(now=NOW + timedelta(minutes=4)) == []
    dead = dog.check(now=NOW + timedelta(minutes=6))
    assert [s.service for s in dead] == ["worker"]
    assert cards and cards[0][0] == "alert"
    assert cards[0][1]["service"] == "worker" and cards[0][1]["silent_for_s"] >= 300

    dog.check(now=NOW + timedelta(minutes=7))
    assert len(cards) == 1  # одно предупреждение на один простой

    beat(session, "worker", now=NOW + timedelta(minutes=8))
    session.flush()
    assert dog.check(now=NOW + timedelta(minutes=9)) == []


# -- бэкап (R25.3) -----------------------------------------------------------------------


def test_backup_packs_dump_and_configs_and_rotates(tmp_path):
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)
        Path(cmd[cmd.index("-f") + 1]).write_text("-- dump --", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "limits.yaml").write_text("a: 1", encoding="utf-8")
    dest = tmp_path / "backups"
    old = dest / "lab-2026-08-01.tar.gz"
    dest.mkdir()
    old.write_bytes(b"old")

    res = backup(
        dest=dest,
        database_url="postgresql+psycopg://lab:lab@db:5432/lab",
        config_dir=config_dir,
        keep_days=7,
        now=NOW,
        run=run,
    )
    assert res.path.name == "lab-2026-09-07.tar.gz" and res.path.exists()
    assert any("pg_dump" in c[0] for c in calls)
    assert "config/limits.yaml" in res.contents and "db.sql" in res.contents
    assert old in res.rotated and not old.exists()


def test_rotate_keeps_recent(tmp_path):
    for day in (1, 5, 7):
        (tmp_path / f"lab-2026-09-0{day}.tar.gz").write_bytes(b"x")
    removed = rotate(tmp_path, keep_days=3, now=NOW)
    left = sorted(p.name for p in tmp_path.glob("lab-*.tar.gz"))
    assert left == ["lab-2026-09-05.tar.gz", "lab-2026-09-07.tar.gz"]
    assert [p.name for p in removed] == ["lab-2026-09-01.tar.gz"]


def test_restore_script_is_executable_and_checks_dump():
    script = Path(__file__).resolve().parents[2] / "scripts" / "restore.sh"
    text = script.read_text(encoding="utf-8")
    assert script.stat().st_mode & 0o111  # исполняемый
    assert "psql" in text and "pg_isready" in text


# -- горячая перезагрузка конфигов (R30i.4) ----------------------------------------------


class FakeRisk:
    def __init__(self) -> None:
        self.reloads: list[str] = []

    def reload(self, by: str = "system"):
        self.reloads.append(by)
        from lab.core.risk import ReloadResult

        return ReloadResult(applied=True)


def test_reload_records_changed_files_and_calls_risk(tmp_path):
    cfg = tmp_path / "limits.yaml"
    cfg.write_text("bank_usd: 1000", encoding="utf-8")
    changes = []
    risk = FakeRisk()
    reloader = ConfigReloader(risk=risk, config_dir=tmp_path, sink=changes.append)

    assert reloader.reload(by="operator").changed == []  # первый вызов — снимок
    cfg.write_text("bank_usd: 2000", encoding="utf-8")
    report = reloader.reload(by="operator")

    assert report.changed == ["limits.yaml"] and report.applied
    assert risk.reloads == ["operator", "operator"]
    assert changes[-1].path.endswith("limits.yaml") and changes[-1].who == "operator"


def test_reload_survives_broken_config(tmp_path):
    (tmp_path / "limits.yaml").write_text("bank_usd: 1000", encoding="utf-8")
    reloader = ConfigReloader(risk=None, config_dir=tmp_path)
    reloader.reload()
    (tmp_path / "limits.yaml").write_text("bank_usd: [", encoding="utf-8")

    class Broken:
        def reload(self, by="system"):
            raise ValueError("не разобрать limits.yaml")

    reloader.risk = Broken()
    report = reloader.reload(by="operator")
    assert report.applied is False and "limits.yaml" in report.error


# -- суточная сверка (R31i.1) ------------------------------------------------------------


class FakeExecutor:
    def __init__(self, fills):
        self._fills = fills

    def fills(self, since):
        return list(self._fills)


def test_reconcile_job_sends_alert_on_mismatch(scope):
    theirs = Fill(
        id="f-1",
        order_id="o-1",
        price=Decimal(100),
        qty=Decimal(1),
        fee=Decimal("0.1"),
        fee_asset="USDT",
        ts=NOW,
    )
    cards: list[tuple[str, dict]] = []
    reports = reconcile_all(
        scope,
        executors={"bybit": FakeExecutor([theirs])},
        alert=lambda kind, payload: cards.append((kind, payload)),
        now=NOW + timedelta(hours=1),
    )
    assert [r.ok for r in reports] == [False]
    assert cards and cards[0][0] == "alert"
    assert "bybit" in cards[0][1]["detail"]


def test_reconcile_job_is_quiet_when_venues_agree(scope):
    cards = []
    reports = reconcile_all(
        scope,
        executors={"bybit": FakeExecutor([])},
        alert=lambda kind, payload: cards.append((kind, payload)),
        now=NOW,
    )
    assert [r.ok for r in reports] == [True] and cards == []
