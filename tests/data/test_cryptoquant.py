"""Хранилище и клиент CryptoQuant. В сеть не ходим — транспорт фейковый."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.data.cryptoquant import MARKET, CryptoQuantStore, DailyRow
from lab.feeds.chains.fake import FakeHttpTransport
from lab.feeds.cryptoquant import (
    BY_EXCHANGE,
    RETRY_ON_LIMIT,
    CryptoQuantConfig,
    CryptoQuantError,
    CryptoQuantFeed,
    plan_series,
)
from lab.ops.jobs.cryptoquant import collect


def day(n: int) -> datetime:
    return datetime(2026, 9, n, tzinfo=UTC)


def ok(rows: list[dict]) -> dict:
    return {
        "status": {"code": 200, "message": "success"},
        "result": {"window": "day", "data": rows},
    }


def liq(date: str, long_: float, short: float) -> dict:
    return {
        "date": date,
        "long_liquidations": long_,
        "short_liquidations": short,
        "long_liquidations_usd": long_ * 1000,
        "short_liquidations_usd": short * 1000,
    }


# -- хранилище -------------------------------------------------------------------------


def test_write_read_roundtrip(tmp_path):
    store = CryptoQuantStore(tmp_path)
    store.write("btc", "all_exchange", [DailyRow(ts=day(1), long_liq=Decimal("12.5"))])
    rows = store.read("btc", "all_exchange", day(1), day(5))
    assert len(rows) == 1
    assert rows[0].long_liq == Decimal("12.5")
    assert rows[0].taker_buy is None  # не спрашивали — не ноль


def test_write_is_idempotent(tmp_path):
    store = CryptoQuantStore(tmp_path)
    rows = [DailyRow(ts=day(1), long_liq=Decimal(1)), DailyRow(ts=day(2), long_liq=Decimal(2))]
    assert store.write("btc", "all_exchange", rows) == 2
    assert store.write("btc", "all_exchange", rows) == 0
    assert store.count("btc", "all_exchange") == 2


def test_second_pass_does_not_erase_other_columns(tmp_path):
    """Разные точки API приходят разными запросами — второй не должен затирать первый."""
    store = CryptoQuantStore(tmp_path)
    store.write("btc", "all_exchange", [DailyRow(ts=day(1), long_liq=Decimal(7))])
    store.write("btc", "all_exchange", [DailyRow(ts=day(1), funding_rate=Decimal("0.0001"))])
    row = store.read("btc", "all_exchange", day(1), day(2))[0]
    assert row.long_liq == Decimal(7)
    assert row.funding_rate == Decimal("0.0001")


def test_source_correction_wins(tmp_path):
    store = CryptoQuantStore(tmp_path)
    store.write("btc", "binance", [DailyRow(ts=day(1), long_liq=Decimal(1))])
    store.write("btc", "binance", [DailyRow(ts=day(1), long_liq=Decimal(9))])
    assert store.read("btc", "binance", day(1), day(2))[0].long_liq == Decimal(9)


def test_high_precision_value_does_not_break_the_whole_day(tmp_path):
    """Источник отдаёт отношения с машинной точностью; колонка держит 12 знаков.

    Без явного приведения pyarrow отказывается писать таблицу целиком
    (`Rescaling Decimal value would cause data loss`), и один лишний знак у одного
    числа ронял ВЕСЬ суточный проход. Поймано на первом же живом сборе 14.09.2026.
    """
    store = CryptoQuantStore(tmp_path)
    written = store.write(
        "btc",
        "all_exchange",
        [DailyRow(ts=day(1), taker_buy_ratio=Decimal("0.9999999999999999"), long_liq=Decimal(1))],
    )
    assert written == 1
    row = store.read("btc", "all_exchange", day(1), day(2))[0]
    assert row.taker_buy_ratio == Decimal("1.000000000000")
    assert row.long_liq == Decimal(1)


def test_series_lists_what_is_collected(tmp_path):
    store = CryptoQuantStore(tmp_path)
    store.write("btc", "all_exchange", [DailyRow(ts=day(1), long_liq=Decimal(1))])
    store.write("eth", MARKET, [DailyRow(ts=day(1), coinbase_premium_gap=Decimal(2))])
    assert sorted(store.series()) == [("btc", "all_exchange"), ("eth", MARKET)]


def test_month_boundary_splits_partitions(tmp_path):
    store = CryptoQuantStore(tmp_path)
    store.write(
        "btc",
        "all_exchange",
        [
            DailyRow(ts=datetime(2026, 8, 31, tzinfo=UTC), long_liq=Decimal(1)),
            DailyRow(ts=datetime(2026, 9, 1, tzinfo=UTC), long_liq=Decimal(2)),
        ],
    )
    assert len(store.partition_files("btc", "all_exchange")) == 2
    assert store.count("btc", "all_exchange") == 2


# -- клиент ----------------------------------------------------------------------------


def feed(transport, key: str = "k") -> CryptoQuantFeed:
    return CryptoQuantFeed(key, transport=transport, base_url="https://x/v1")


def test_series_parses_rows():
    t = FakeHttpTransport().route("GET", "liquidations", ok([liq("2026-09-02", 2, 3)]))
    rows = feed(t).series(
        "btc",
        "market-data/liquidations",
        BY_EXCHANGE["market-data/liquidations"],
        exchange="binance",
    )
    assert rows[0].ts == day(2)
    assert rows[0].long_liq == Decimal("2")
    assert rows[0].short_liq_usd == Decimal("3000")


def test_key_goes_in_header_and_never_in_params():
    t = FakeHttpTransport().route("GET", "liquidations", ok([]))
    feed(t, "secret").series(
        "btc",
        "market-data/liquidations",
        BY_EXCHANGE["market-data/liquidations"],
        exchange="binance",
    )
    call = t.calls[0]
    assert call.params == {"window": "day", "limit": 30, "exchange": "binance"}
    assert "secret" not in str(call.params)


def test_error_status_becomes_error():
    t = FakeHttpTransport().route(
        "GET", "liquidations", {"status": {"code": 403, "message": "forbidden"}, "result": {}}
    )
    with pytest.raises(CryptoQuantError):
        feed(t).series(
            "btc", "market-data/liquidations", BY_EXCHANGE["market-data/liquidations"], exchange="x"
        )


def test_rate_limit_is_retried(monkeypatch):
    """429 повторяется: окно тарифа в 30 суток не даст вернуться за пропущенным позже."""
    calls = {"n": 0}

    class Limited:
        def get(self, url, *, params=None, headers=None):
            calls["n"] += 1
            if calls["n"] <= RETRY_ON_LIMIT:
                raise RuntimeError(f"{url}: 429")
            return ok([liq("2026-09-02", 1, 1)])

    waits: list[float] = []
    f = CryptoQuantFeed("k", transport=Limited(), base_url="https://x/v1", sleep=waits.append)
    rows = f.series(
        "btc", "market-data/liquidations", BY_EXCHANGE["market-data/liquidations"], exchange="a"
    )
    assert len(rows) == 1
    assert calls["n"] == RETRY_ON_LIMIT + 1
    assert waits, "перед повтором обязана быть пауза"


def test_other_errors_are_not_retried():
    """403 — это про тариф, а не про темп: повторять бессмысленно и вредно."""
    calls = {"n": 0}

    class Forbidden:
        def get(self, url, *, params=None, headers=None):
            calls["n"] += 1
            raise RuntimeError(f"{url}: 403")

    f = CryptoQuantFeed("k", transport=Forbidden(), base_url="https://x/v1", sleep=lambda _: None)
    with pytest.raises(CryptoQuantError):
        f.series(
            "btc", "market-data/liquidations", BY_EXCHANGE["market-data/liquidations"], exchange="a"
        )
    assert calls["n"] == 1


def test_no_key_means_disabled():
    """Без ключа источник не падает, а честно говорит «недоступен» — как все остальные."""
    f = CryptoQuantFeed("", transport=FakeHttpTransport())
    assert not f.enabled
    assert f.health().status == "down"


def test_collect_survives_one_dead_endpoint():
    """Закрытая точка не должна отменять остальные — иначе не собрано ничего."""
    t = FakeHttpTransport()
    t.route("GET", "liquidations", ok([liq("2026-09-02", 1, 1)]))
    t.route("GET", "taker-buy-sell-stats", {"status": {"code": 403, "message": "forbidden"}})
    t.route("GET", "open-interest", ok([{"date": "2026-09-02", "open_interest": 5}]))
    t.route("GET", "funding-rates", ok([{"date": "2026-09-02", "funding_rates": 0.0001}]))
    rows, notes = feed(t).collect("btc", "all_exchange")
    assert len(rows) == 1
    assert rows[0].long_liq == Decimal(1) and rows[0].open_interest == Decimal(5)
    assert len(notes) == 1


def test_market_wide_series_asks_no_exchange():
    t = FakeHttpTransport().route(
        "GET",
        "coinbase-premium-index",
        ok([{"date": "2026-09-02", "coinbase_premium_gap": 1.5, "coinbase_premium_index": 0.01}]),
    )
    rows, notes = feed(t).collect("btc", MARKET)
    assert notes == []
    assert rows[0].coinbase_premium_gap == Decimal("1.5")
    assert "exchange" not in (t.calls[0].params or {})


def test_plan_includes_market_row_per_asset():
    plan = plan_series(CryptoQuantConfig(assets=["btc"], exchanges=["binance"]))
    assert plan == [("btc", "binance"), ("btc", MARKET)]


# -- задание ---------------------------------------------------------------------------


def all_ok(transport: FakeHttpTransport, date: str) -> FakeHttpTransport:
    transport.route("GET", "liquidations", ok([liq(date, 1, 1)]))
    transport.route("GET", "taker-buy-sell-stats", ok([{"date": date, "taker_buy_volume": 10}]))
    transport.route("GET", "open-interest", ok([{"date": date, "open_interest": 5}]))
    transport.route("GET", "funding-rates", ok([{"date": date, "funding_rates": 0.0001}]))
    transport.route(
        "GET", "coinbase-premium-index", ok([{"date": date, "coinbase_premium_gap": 2}])
    )
    return transport


def test_collect_job_writes_only_closed_days(tmp_path):
    """Текущие сутки ещё не закрыты: записанное сегодня значение завтра окажется меньше."""
    today = datetime(2026, 9, 3, 12, tzinfo=UTC)
    t = all_ok(FakeHttpTransport(), "2026-09-03")
    store = CryptoQuantStore(tmp_path)
    result = collect(
        feed=feed(t),
        store=store,
        config=CryptoQuantConfig(assets=["btc"], exchanges=["all_exchange"]),
        now=today,
    )
    assert result.rows == 0
    assert store.count("btc", "all_exchange") == 0

    yesterday = all_ok(FakeHttpTransport(), "2026-09-02")
    result = collect(
        feed=feed(yesterday),
        store=store,
        config=CryptoQuantConfig(assets=["btc"], exchanges=["all_exchange"]),
        now=today,
    )
    assert result.written == 2  # ряд площадки и рыночный ряд
    assert store.count("btc", "all_exchange") == 1


def test_collect_job_without_key_is_not_an_error(tmp_path):
    result = collect(
        feed=CryptoQuantFeed("", transport=FakeHttpTransport()),
        store=CryptoQuantStore(tmp_path),
        config=CryptoQuantConfig(assets=["btc"], exchanges=["all_exchange"]),
    )
    assert not result.ok
    assert "не задан" in (result.error or "")


def test_repeat_run_adds_nothing(tmp_path):
    today = datetime(2026, 9, 3, tzinfo=UTC)
    cfg = CryptoQuantConfig(assets=["btc"], exchanges=["all_exchange"])
    store = CryptoQuantStore(tmp_path)
    for _ in range(2):
        last = collect(
            feed=feed(all_ok(FakeHttpTransport(), "2026-09-02")), store=store, config=cfg, now=today
        )
    assert last.written == 0
    assert store.count("btc", "all_exchange") == 1


def test_window_gap_is_repaired_by_next_run(tmp_path):
    """Простой сервера закрывается сам: окно берётся целиком, а не «со вчера»."""
    store = CryptoQuantStore(tmp_path)
    cfg = CryptoQuantConfig(assets=["btc"], exchanges=["all_exchange"])
    base = datetime(2026, 9, 1, tzinfo=UTC)
    t = FakeHttpTransport()
    rows = [liq(f"2026-09-{n:02d}", 1, 1) for n in range(1, 6)]
    t.route("GET", "liquidations", ok(rows))
    t.route("GET", "taker-buy-sell-stats", {"status": {"code": 403}})
    t.route("GET", "open-interest", {"status": {"code": 403}})
    t.route("GET", "funding-rates", {"status": {"code": 403}})
    t.route("GET", "coinbase-premium-index", {"status": {"code": 403}})
    collect(feed=feed(t), store=store, config=cfg, now=base + timedelta(days=5))
    assert store.count("btc", "all_exchange") == 5
