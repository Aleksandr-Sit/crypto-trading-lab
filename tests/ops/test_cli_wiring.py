"""CLI-связка (R12, R13, R32i): замер руками, `.env` доезжает до сервисов.

Швы: `lab measure run|show` (единственный ручной вход в `core.measure.run`),
`cmd_service_web` (логин/пароль из `.env`, а не только из окружения).
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lab import cli
from lab.core.registry import Registry
from lab.data import CandleStore
from lab.strategies import registry as code_registry
from tests.fixtures.synthetic import synthetic_candles

STRATEGY_ID = "cex-spot-indicator-pifagor-forever-sma-v0"
NOW = datetime(2026, 9, 7, tzinfo=UTC)


@pytest.fixture
def scope(session):
    @contextmanager
    def _scope():
        yield session

    return _scope


@pytest.fixture
def cli_scope(monkeypatch, scope):
    monkeypatch.setattr(cli, "_scope", lambda: scope)
    return scope


def _seed_store(tmp_path: Path) -> Path:
    store = CandleStore(tmp_path / "data")
    store.write(
        "binance",
        "BTC/USDT",
        "1d",
        synthetic_candles(
            420,
            "trend",
            drift_pct="0.3",
            tf="1d",
            start=NOW - timedelta(days=430),
            instrument="BTC/USDT",
        ),
    )
    return tmp_path / "data"


def test_measure_run_writes_a_snapshot_and_prints_verdict(session, cli_scope, tmp_path, capsys):
    Registry(session).add(code_registry.manifest(STRATEGY_ID))
    root = _seed_store(tmp_path)

    code = cli.main(["measure", "run", STRATEGY_ID, "--days", "400", "--root", str(root)])

    out = capsys.readouterr().out
    assert code == 0
    assert STRATEGY_ID in out and "backtest" in out
    from lab.core.measure import history

    assert history(session, STRATEGY_ID), "снимок замера не сохранён"


def test_measure_run_names_unknown_strategy(session, cli_scope, capsys):
    code = cli.main(["measure", "run", "cex-spot-indicator-нет-такой"])
    err = capsys.readouterr().err
    assert code == 2 and "нет-такой" in err


def test_measure_show_lists_snapshots(session, cli_scope, tmp_path, capsys):
    Registry(session).add(code_registry.manifest(STRATEGY_ID))
    root = _seed_store(tmp_path)
    cli.main(["measure", "run", STRATEGY_ID, "--days", "400", "--root", str(root)])
    capsys.readouterr()

    code = cli.main(["measure", "show", STRATEGY_ID])

    out = capsys.readouterr().out
    assert code == 0 and "backtest" in out


def test_measure_show_says_nothing_measured_yet(session, cli_scope, capsys):
    Registry(session).add(code_registry.manifest(STRATEGY_ID))
    code = cli.main(["measure", "show", STRATEGY_ID])
    out = capsys.readouterr().out
    assert code == 0 and "не мерил" in out


def test_service_web_takes_credentials_from_dotenv(monkeypatch, tmp_path, capsys):
    """README обещает WEB_USER/WEB_PASSWORD в `.env` — сейчас читается только окружение."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("WEB_USER=ops        # логин\nWEB_PASSWORD=s3cret # пароль\n", "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_USER", raising=False)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "_scope", lambda: None)
    monkeypatch.setattr(cli, "_feeds_registry", lambda scope: None)

    code = cli.main(["service", "web", "--once"])

    out = capsys.readouterr().out
    assert code == 0
    assert "ops" in out


def test_service_web_generates_password_when_dotenv_has_none(monkeypatch, tmp_path, capsys):
    """`cp .env.example .env` без правок: веб поднимается с разовым паролем, а не отказом."""
    (tmp_path / ".env").write_text(
        "WEB_USER=            # логин\nWEB_PASSWORD=  # пароль\n", "utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_USER", raising=False)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "_scope", lambda: None)
    monkeypatch.setattr(cli, "_feeds_registry", lambda scope: None)

    code = cli.main(["service", "web", "--once"])

    out = capsys.readouterr().out
    assert code == 0
    assert "разовый пароль" in out
