"""Coinalyze: клиент, сбор и разделение источников в хранилище. В сеть не ходим."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.data.daily_market import CoinalyzeStore, CryptoQuantStore, DailyRow
from lab.feeds.coinalyze import CoinalyzeConfig, CoinalyzeError, CoinalyzeFeed
from lab.ops.jobs.coinalyze import AGGREGATE, collect

DAY = 1757808000  # 2025-09-14 00:00 UTC


class FakeTransport:
    """Отдаёт заготовленные ответы по подстроке пути; считает вызовы."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object]] = []
        self.calls: list[str] = []

    def route(self, match: str, value: object) -> FakeTransport:
        self.routes.append((match, value))
        return self

    def get_json(self, url: str, *, headers: dict[str, str] | None = None):
        self.calls.append(url)
        for match, value in self.routes:
            if match in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise RuntimeError(f"маршрут не задан: {url}")


def history(*points: tuple[int, float, float]) -> list[dict]:
    rows = [{"t": t, "l": long_, "s": short} for t, long_, short in points]
    return [{"symbol": "X", "history": rows}]


def oi(*points: tuple[int, float]) -> list[dict]:
    return [{"symbol": "X", "history": [{"t": t, "c": value} for t, value in points]}]


def feed(transport, key: str = "k", **kw) -> CoinalyzeFeed:
    return CoinalyzeFeed(key, transport=transport, base_url="https://x/v1", **kw)


# -- разделение источников ---------------------------------------------------------------


def test_sources_do_not_mix(tmp_path):
    """Одна метрика из двух источников не должна складываться в один ряд.

    У них разные площадки в агрегате и разные правила округления: сложенные вместе,
    они дали бы скачок на стыке, который потом не объяснить.
    """
    ts = datetime(2026, 9, 1, tzinfo=UTC)
    CoinalyzeStore(tmp_path).write("btc", AGGREGATE, [DailyRow(ts=ts, long_liq_usd=Decimal(1))])
    CryptoQuantStore(tmp_path).write("btc", AGGREGATE, [DailyRow(ts=ts, long_liq_usd=Decimal(9))])
    assert CoinalyzeStore(tmp_path).read("btc", AGGREGATE, ts, ts + timedelta(days=1))[
        0
    ].long_liq_usd == Decimal(1)
    assert CryptoQuantStore(tmp_path).read("btc", AGGREGATE, ts, ts + timedelta(days=1))[
        0
    ].long_liq_usd == Decimal(9)
    assert CoinalyzeStore(tmp_path).series() == [("btc", AGGREGATE)]


# -- клиент ------------------------------------------------------------------------------


def test_key_goes_in_header_not_in_url():
    t = FakeTransport().route("liquidation-history", history((DAY, 1.0, 2.0)))
    captured: dict[str, object] = {}

    class Spy(FakeTransport):
        def get_json(self, url, *, headers=None):
            captured["headers"] = headers
            captured["url"] = url
            return history((DAY, 1.0, 2.0))

    spy = Spy()
    feed(spy, "secret").series("BTCUSDT_PERP.A", "liquidation-history", {"l": "long_liq_usd"}, 10)
    assert captured["headers"] == {"api_key": "secret"}
    assert "secret" not in str(captured["url"])
    assert t.calls == []


def test_series_parses_and_floors_to_day():
    t = FakeTransport().route("liquidation-history", history((DAY + 3600, 5.0, 7.0)))
    rows = feed(t).series(
        "X", "liquidation-history", {"l": "long_liq_usd", "s": "short_liq_usd"}, 10
    )
    assert rows[0].ts.hour == 0
    assert rows[0].long_liq_usd == Decimal("5.0")
    assert rows[0].short_liq_usd == Decimal("7.0")


def test_no_key_means_disabled():
    f = CoinalyzeFeed("", transport=FakeTransport())
    assert not f.enabled
    assert f.health().status == "down"


def test_unauthorized_becomes_error():
    t = FakeTransport().route("exchanges", RuntimeError("HTTP Error 401: Unauthorized"))
    assert feed(t).health().status == "down"


def test_collect_sums_across_exchanges():
    """Агрегата источник не отдаёт — суммируем сами, иначе получим одну биржу из трёх."""
    t = FakeTransport()
    t.route("liquidation-history", history((DAY, 10.0, 1.0)))
    t.route("open-interest-history", oi((DAY, 100.0)))
    rows, notes = feed(t).collect(["A.1", "A.2", "A.3"])
    assert notes == []
    assert len(rows) == 1
    assert rows[0].long_liq_usd == Decimal(30)  # три площадки по 10
    assert rows[0].open_interest == Decimal(300)


def test_one_dead_exchange_does_not_kill_the_pass():
    t = FakeTransport()
    t.route("open-interest-history", oi((DAY, 100.0)))
    t.route("liquidation-history", RuntimeError("HTTP Error 500"))
    rows, notes = feed(t).collect(["A.1"])
    assert len(notes) == 1
    assert rows[0].open_interest == Decimal(100)
    assert rows[0].long_liq_usd is None


def test_unexpected_payload_is_an_error():
    t = FakeTransport().route("liquidation-history", {"message": "nope"})
    with pytest.raises(CoinalyzeError):
        feed(t).series("X", "liquidation-history", {"l": "long_liq_usd"}, 10)


# -- задание -----------------------------------------------------------------------------


def ready(transport: FakeTransport) -> FakeTransport:
    transport.route("liquidation-history", history((DAY, 10.0, 1.0)))
    transport.route("open-interest-history", oi((DAY, 100.0)))
    return transport


CONFIG = CoinalyzeConfig(markets={"BTC": ["A.1"]}, pause_s=0.0)
TOMORROW = datetime(2025, 9, 15, tzinfo=UTC)


def test_job_writes_closed_days(tmp_path):
    store = CoinalyzeStore(tmp_path)
    result = collect(feed=feed(ready(FakeTransport())), store=store, config=CONFIG, now=TOMORROW)
    assert result.ok and result.written == 1
    assert store.count("BTC", AGGREGATE) == 1


def test_job_skips_the_unfinished_day(tmp_path):
    """Текущие сутки ещё не закрыты: записанное сейчас значение завтра окажется меньше."""
    store = CoinalyzeStore(tmp_path)
    today = datetime(2025, 9, 14, 12, tzinfo=UTC)
    result = collect(feed=feed(ready(FakeTransport())), store=store, config=CONFIG, now=today)
    assert result.rows == 0
    assert store.count("BTC", AGGREGATE) == 0


def test_job_is_idempotent(tmp_path):
    store = CoinalyzeStore(tmp_path)
    for _ in range(2):
        last = collect(
            feed=feed(ready(FakeTransport())), store=store, config=CONFIG, now=TOMORROW
        )
    assert last.written == 0
    assert store.count("BTC", AGGREGATE) == 1


def test_job_without_key_is_not_an_error(tmp_path):
    result = collect(
        feed=CoinalyzeFeed("", transport=FakeTransport()),
        store=CoinalyzeStore(tmp_path),
        config=CONFIG,
    )
    assert not result.ok
    assert "не задан" in (result.error or "")
