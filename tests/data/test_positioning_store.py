"""Хранилище метрик позиционирования (11.09.2026).

Открытый интерес — первый источник, не дублирующий премию за плечо: связь его ИЗМЕНЕНИЯ
со ставкой фандинга −0.03. Ставка говорит, сколько платят за плечо; интерес — сколько
его набрали. Проверяется то же, что у ставок: идемпотентная запись, уточнение задним
числом и окно чтения.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.data.positioning import Positioning, PositioningStore, daily_mean

T0 = datetime(2026, 1, 30, tzinfo=UTC)  # ряд переходит через границу месяца


def _row(i: int, oi: str = "1000") -> Positioning:
    return Positioning(
        ts=T0 + timedelta(hours=i),
        open_interest=Decimal(oi),
        open_interest_value=Decimal(oi) * Decimal(60_000),
        top_accounts_ratio=Decimal("1.05"),
        top_positions_ratio=Decimal("1.4"),
        accounts_ratio=Decimal("1.1"),
        taker_ratio=Decimal("0.95"),
    )


def test_write_is_idempotent_and_partitioned_by_month(tmp_path):
    store = PositioningStore(tmp_path)
    rows = [_row(i) for i in range(72)]

    assert store.write("binance", "BTC/USDT:USDT", rows) == 72
    assert store.write("binance", "BTC/USDT:USDT", rows) == 0, "повтор не удваивает"
    files = sorted(p.name for p in store.partition_files("binance", "BTC/USDT:USDT"))
    assert files == ["2026-01.parquet", "2026-02.parquet"]
    assert store.count("binance", "BTC/USDT:USDT") == 72


def test_exchange_may_correct_a_value_after_the_fact(tmp_path):
    """Число строк то же, значение другое — по количеству такую правку не заметить."""
    store = PositioningStore(tmp_path)
    store.write("binance", "BTC/USDT:USDT", [_row(0, "1000")])

    store.write("binance", "BTC/USDT:USDT", [_row(0, "1500")])

    got = store.read("binance", "BTC/USDT:USDT", T0, T0 + timedelta(hours=1))
    assert got[0].open_interest == Decimal(1500)


def test_read_returns_the_window_only(tmp_path):
    store = PositioningStore(tmp_path)
    store.write("binance", "BTC/USDT:USDT", [_row(i) for i in range(10)])

    got = store.read("binance", "BTC/USDT:USDT", T0 + timedelta(hours=2), T0 + timedelta(hours=5))

    assert [r.ts for r in got] == [T0 + timedelta(hours=i) for i in (2, 3, 4)]


def test_daily_mean_smooths_the_five_minute_noise():
    """На пятиминутках у интереса ходят проценты внутри дня — правилу нужна суточная."""
    rows = [_row(0, "1000"), _row(1, "1200"), _row(25, "2000")]

    means = daily_mean(rows, "open_interest")

    assert means[T0.date()] == Decimal(1100)
    assert means[(T0 + timedelta(days=1)).date()] == Decimal(2000)


def test_missing_series_reads_empty_not_raises(tmp_path):
    store = PositioningStore(tmp_path)
    assert store.read("binance", "NOPE/USDT:USDT", T0, T0 + timedelta(days=1)) == []
    assert store.count("binance", "NOPE/USDT:USDT") == 0
    assert store.last_ts("binance", "NOPE/USDT:USDT") is None
