"""Реестр источников (R25.1, R25.2, A01): квоты, прогноз, фолбэк, деградация, бюджет."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.ops.feeds_registry import (
    FeedsRegistry,
    FeedUnavailable,
    QuotaExceeded,
    load_feeds,
)

CONFIG = """
budget_month_usd: 50
warn_pct: 80
feeds:
  primary:
    name: Первый
    kind: cex
    quota_limit: 100
    quota_period: hour
    fallback: backup
  backup:
    name: Запасной
    kind: cex
    quota_limit: 1000
    quota_period: hour
  paid:
    name: Платный
    kind: chain
    quota_limit: null
    quota_period: month
    cost_month: 30
    cost_per_call: 0.5
"""

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def tick(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "feeds.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return load_feeds(path)


@pytest.fixture
def registry(config):
    return FeedsRegistry(config=config, clock=Clock())


def test_quota_counts_and_resets_with_period(registry):
    registry.use("primary", 10)
    registry.use("primary", 5)
    row = {s.id: s for s in registry.status()}["primary"]
    assert (row.quota_used, row.quota_limit, row.quota_period) == (15, 100, "hour")

    registry.clock.tick(hours=1)
    assert {s.id: s for s in registry.status()}["primary"].quota_used == 0


def test_forecast_of_exhaustion_at_current_rate(registry):
    registry.clock.tick(minutes=1)
    registry.use("primary", 50)  # половина часовой квоты за минуту
    row = {s.id: s for s in registry.status()}["primary"]
    # ещё 50 при том же темпе — ещё минута
    assert row.exhausted_at == registry.clock.now + timedelta(minutes=1)
    assert {s.id: s for s in registry.status()}["backup"].exhausted_at is None


def test_exceeded_quota_switches_to_fallback(registry):
    registry.use("primary", 100)
    assert registry.acquire("primary") == "backup"
    assert {s.id: s for s in registry.status()}["backup"].quota_used == 1
    with pytest.raises(QuotaExceeded):
        registry.use("primary")


def test_no_fallback_and_no_quota_raises(registry):
    registry.use("backup", 1000)
    with pytest.raises(QuotaExceeded):
        registry.acquire("backup")


def test_failed_source_degrades_but_process_lives(registry):
    def broken(instrument, tf, from_ts, to_ts):
        raise TimeoutError("шлюз молчит")

    source = registry.source("primary", broken)
    with pytest.raises(FeedUnavailable) as err:
        source("BTC/USDT", "1h", T0, T0)
    # measure.run ловит ConnectionError → замер уходит в incomplete, а не падает
    assert isinstance(err.value, ConnectionError)
    row = {s.id: s for s in registry.status()}["primary"]
    assert row.health == "down" and "шлюз молчит" in row.health_detail
    registry.use("backup")  # процесс жив, остальные источники работают


def test_source_falls_back_when_primary_fails(registry):
    def broken(*a):
        raise TimeoutError("нет ответа")

    source = registry.source("primary", broken, fallback=lambda *a: ["свеча"])
    assert source("BTC/USDT", "1h", T0, T0) == ["свеча"]
    assert {s.id: s for s in registry.status()}["primary"].health == "down"


def test_budget_sums_subscriptions_and_pay_per_use(registry):
    registry.use("paid", 1)
    registry.use("paid", 1)  # 2 вызова × $0.5
    budget = registry.budget()
    assert budget.month_limit_usd == Decimal(50)
    assert budget.spent_usd == Decimal("31")


def test_budget_warns_at_80_and_at_100(registry):
    registry.spend("paid", Decimal(10))  # 30 подписки + 10 = 40 = 80%
    assert [level for level, _ in registry.budget_alerts()] == ["warn"]
    assert registry.budget_alerts() == []  # одно предупреждение на уровень
    registry.spend("paid", Decimal(10))  # 50 = 100%
    levels = [level for level, _ in registry.budget_alerts()]
    assert levels == ["limit"]


def test_health_check_marks_down_and_records_check_time(registry):
    class Feed:
        def __init__(self, status):
            self.status = status

        def health(self):
            from lab.contracts import Health

            return Health(status=self.status, detail="гео-блок", checked_at=T0)

    registry.health_check({"primary": Feed("down"), "backup": Feed("ok")})
    rows = {s.id: s for s in registry.status()}
    assert rows["primary"].health == "down" and rows["backup"].health == "ok"
    assert "гео-блок" in rows["primary"].health_detail


def test_project_config_covers_used_feeds():
    cfg = load_feeds()
    for feed_id in ("bybit", "okx", "binance", "hyperliquid", "solana", "evm", "polymarket"):
        assert feed_id in cfg.feeds
    assert cfg.budget_month_usd == Decimal(50)


def test_status_row_also_reads_as_bot_feed_status(registry):
    """Один объект годится и вебу (`web.feeds_source`), и утреннему отчёту (`bot.report`)."""
    registry.use("primary", 3)
    row = {s.id: s for s in registry.status()}["primary"]
    assert row.feed_id == "primary" and row.detail == row.health_detail
