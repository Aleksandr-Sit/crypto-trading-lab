"""Конфиги читаются pydantic-моделями; числа — из спецификации; ошибка — с именем поля."""

from decimal import Decimal
from pathlib import Path

import pytest

from lab.config import (
    ConfigError,
    LimitsConfig,
    ScheduleConfig,
    ThresholdConfig,
    load_config,
    venues_report,
)
from lab.contracts import KeyRights

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def test_limits_match_spec_table():
    limits = load_config(CONFIG_DIR / "limits.yaml", LimitsConfig)
    shares = {name: g.share_pct for name, g in limits.groups.items()}
    assert shares == {
        "cex": Decimal(40),
        "copy": Decimal(25),
        "meme": Decimal(20),
        "nft": Decimal(10),
        "prediction": Decimal(5),
    }
    assert sum(shares.values()) == Decimal(100)
    assert limits.group_of("cex-perp") == "cex"
    assert limits.group_of("dex-perp") == "cex"
    assert limits.group_of("rh") == "prediction"
    cex = limits.groups["cex"]
    assert cex.max_trade_pct == Decimal(2) and cex.max_trade_base == "bank"
    assert cex.max_leverage == Decimal(5)
    assert cex.stop.loss_pct == Decimal(5) and cex.stop.period == "day"
    meme = limits.groups["meme"]
    assert meme.max_trade_pct == Decimal(10) and meme.max_trade_base == "branch"
    assert meme.max_leverage == Decimal(1)
    assert meme.stop.loss_pct == Decimal(20)
    nft = limits.groups["nft"]
    assert nft.stop.loss_pct == Decimal(30) and nft.stop.period == "week"
    assert limits.real_capital_cap_usd == Decimal(1000)


def test_threshold_matches_rule_v12():
    th = load_config(CONFIG_DIR / "threshold.yaml", ThresholdConfig)
    assert th.min_trades == 30
    assert th.min_ev_after_costs == Decimal(0)
    assert th.benchmark == "BTC-buy-and-hold"
    assert th.max_dd_pct["meme"] == Decimal(20)


def test_schedule_matches_decision_11():
    sch = load_config(CONFIG_DIR / "schedule.yaml", ScheduleConfig)
    assert sch.timezone == "Europe/Samara"
    assert sch.jobs["morning_report"].cron == "0 9 * * *"
    assert sch.jobs["discovery"].cron == "0 6 * * mon"
    assert sch.jobs["remeasure"].cron == "0 22 * * sun"
    assert sch.jobs["backup"].cron == "0 3 * * *"


def test_invalid_config_names_field(tmp_path: Path):
    bad = tmp_path / "limits.yaml"
    bad.write_text(
        "real_capital_cap_usd: 1000\n"
        "groups:\n"
        "  cex:\n"
        "    branches: [cex-spot]\n"
        "    share_pct: 140\n"
        "    max_trade_pct: 2\n"
        "    max_trade_base: bank\n"
        "    max_leverage: 5\n"
        "    stop: {loss_pct: 5, period: day}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as err:
        load_config(bad, LimitsConfig)
    text = str(err.value)
    assert "limits.yaml" in text
    assert "share_pct" in text


def test_missing_config_file_is_clear_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="не найден"):
        load_config(tmp_path / "nope.yaml", ThresholdConfig)


def test_venues_report_from_env_names():
    env = {
        "BYBIT_API_KEY": "k",
        "BYBIT_API_SECRET": "s",
        "OKX_API_KEY": "k",  # без секрета и passphrase → не подключена
        "HYPERLIQUID_PRIVATE_KEY": "",
    }
    report = {row.venue: row for row in venues_report(env)}
    assert report["bybit"].connected is True
    assert report["okx"].connected is False
    assert report["hyperliquid"].connected is False
    assert report["binance"].connected is False
    assert "ключ пуст" in report["binance"].label
    assert report["bybit"].label == "подключена"


def test_venue_with_withdraw_rights_is_rejected():
    env = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}
    rights = {"bybit": KeyRights(trade=True, withdraw=True)}
    report = {row.venue: row for row in venues_report(env, rights=rights)}
    assert report["bybit"].connected is False
    assert "вывод" in report["bybit"].label


def test_database_url_is_read_from_dotenv_like_migrations(tmp_path: Path):
    from lab.db.engine import DatabaseUrlMissing, database_url

    dotenv = tmp_path / ".env"
    dotenv.write_text("DATABASE_URL=postgresql+psycopg://u:p@dbhost:5432/lab\n", encoding="utf-8")
    assert database_url(dotenv=dotenv, environ={}) == "postgresql+psycopg://u:p@dbhost:5432/lab"
    # переменная окружения важнее .env
    assert database_url(dotenv=dotenv, environ={"DATABASE_URL": "postgresql+psycopg://x@h/y"}) == (
        "postgresql+psycopg://x@h/y"
    )
    # ни там, ни там — понятная ошибка, а не молчаливый localhost
    with pytest.raises(DatabaseUrlMissing, match="DATABASE_URL"):
        database_url(dotenv=tmp_path / "absent.env", environ={})
