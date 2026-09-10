"""Parquet-хранилище свечей (решение §2): партиции по месяцу, идемпотентная запись,
чтение DuckDB, бэкфилл с прогрессом и возобновлением после обрыва."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.data import BackfillInterrupted, CandleStore, backfill
from tests.fixtures.synthetic import synthetic_candles

START = datetime(2026, 1, 30, tzinfo=UTC)  # ряд переходит через границу месяца


@pytest.fixture
def store(tmp_path) -> CandleStore:
    return CandleStore(tmp_path / "parquet")


def test_write_is_idempotent_and_partitioned_by_month(store: CandleStore):
    candles = synthetic_candles(72, "noise", seed=3, tf="1h", start=START)
    first = store.write("bybit", "SYN/USD", "1h", candles)
    assert first.rows_written == 72
    assert sorted(first.partitions) == ["2026-01", "2026-02"]
    files = sorted(p.name for p in store.partition_files("bybit", "SYN/USD", "1h"))
    assert files == ["2026-01.parquet", "2026-02.parquet"]

    # Повторная запись тех же и пересекающихся свечей не удваивает строки.
    again = store.write("bybit", "SYN/USD", "1h", candles[24:])
    assert again.rows_written == 0
    assert store.count("bybit", "SYN/USD", "1h") == 72

    back = store.read("bybit", "SYN/USD", "1h", START, START + timedelta(hours=72))
    assert len(back) == 72
    assert back[0] == candles[0] and back[-1] == candles[-1]
    assert isinstance(back[0].close, Decimal)  # деньги не стали float по дороге
    assert [c.ts for c in back] == sorted(c.ts for c in back)

    window = store.read(
        "bybit", "SYN/USD", "1h", START + timedelta(hours=40), START + timedelta(hours=44)
    )
    assert [c.ts for c in window] == [START + timedelta(hours=h) for h in (40, 41, 42, 43)]
    assert store.read("bybit", "SYN/USD", "1h", START - timedelta(days=30), START) == []


def test_read_via_duckdb_sql(store: CandleStore):
    candles = synthetic_candles(10, "flat", seed=1, tf="1h", start=START)
    store.write("okx", "ETH/USDT", "1h", candles)
    rows = store.query(
        "select count(*) as n, min(ts) as t0 from {candles}", "okx", "ETH/USDT", "1h"
    )
    assert rows[0]["n"] == 10
    assert rows[0]["t0"].replace(tzinfo=UTC) == START


class FlakyFeed:
    """Источник, который отдаёт свечи кусками и падает на k-м запросе."""

    def __init__(self, candles, fail_at: int | None) -> None:
        self.candles = candles
        self.fail_at = fail_at
        self.calls = 0

    def candles_range(self, instrument, tf, from_ts, to_ts):
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise ConnectionError("rate limit")
        return [c for c in self.candles if from_ts <= c.ts < to_ts]


def test_backfill_resumes_after_interruption(store: CandleStore):
    candles = synthetic_candles(96, "noise", seed=5, tf="1h", start=START)
    end = START + timedelta(hours=96)
    seen: list[tuple[int, int]] = []
    feed = FlakyFeed(candles, fail_at=3)
    with pytest.raises(BackfillInterrupted) as err:
        backfill(
            store,
            feed.candles_range,
            "bybit",
            "SYN/USD",
            "1h",
            START,
            end,
            chunk=timedelta(hours=24),
            progress=lambda done, total: seen.append((done, total)),
        )
    assert "rate limit" in str(err.value)
    assert store.count("bybit", "SYN/USD", "1h") == 48  # два куска легли до обрыва
    assert seen[-1] == (48, 96)
    assert err.value.resume_from == START + timedelta(hours=48)

    # Возобновление: начинает с сохранённого прогресса, не перекачивает первые куски.
    feed2 = FlakyFeed(candles, fail_at=None)
    result = backfill(
        store, feed2.candles_range, "bybit", "SYN/USD", "1h", START, end, chunk=timedelta(hours=24)
    )
    assert feed2.calls == 2
    assert result.rows_written == 48 and result.resumed_from == START + timedelta(hours=48)
    assert store.count("bybit", "SYN/USD", "1h") == 96
    # Повтор полностью идемпотентен.
    result = backfill(
        store, feed2.candles_range, "bybit", "SYN/USD", "1h", START, end, chunk=timedelta(hours=24)
    )
    assert result.rows_written == 0 and result.skipped is True


def test_duckdb_appetite_is_capped(tmp_path, monkeypatch):
    """DuckDB берёт 80% памяти КОНТЕЙНЕРА — и не оставляет её рабочему процессу.

    В контейнере с лимитом 1200 МБ движок запроса ставил себе 960 МБ. Три замера
    минутных стратегий подряд получили SIGKILL от cgroup ровно на 946 МБ, и каждый
    завершился МОЛЧА, без единой строки вывода: снаружи это выглядит как «замер ничего
    не сказал», а не как отказ.
    """
    import duckdb

    from lab.data.store import DUCKDB_MEMORY_ENV, DUCKDB_THREADS_ENV, CandleStore, _tame

    store = CandleStore(tmp_path / "parquet")
    store.write(
        "bybit", "SYN/USD", "1h", synthetic_candles(3, "noise", seed=1, tf="1h", start=START)
    )
    store.query("select count(*) as n from {candles}", "bybit", "SYN/USD", "1h")

    con = duckdb.connect()
    try:
        _tame(con)
        limit = con.execute(
            "select value from duckdb_settings() where name = 'memory_limit'"
        ).fetchone()[0]
        threads = con.execute(
            "select value from duckdb_settings() where name = 'threads'"
        ).fetchone()[0]
    finally:
        con.close()

    # DuckDB печатает лимит в мебибайтах: 256 МБ — это 244.1 MiB, а не «256».
    assert float(str(limit).split()[0]) < 300, f"лимит памяти не применён: {limit}"
    assert str(threads) == "2"

    monkeypatch.setenv(DUCKDB_MEMORY_ENV, "128MB")
    monkeypatch.setenv(DUCKDB_THREADS_ENV, "1")
    assert CandleStore._duckdb_limits() == ("128MB", 1), "окружение должно переопределять"
