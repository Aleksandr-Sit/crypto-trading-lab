"""Хранилище ставок фандинга (08.09.2026).

Зачем оно вообще: у стратегий, не зависящих от направления рынка (фандинг-арбитраж, базис,
нейтральные сетки), ВЕСЬ доход — в этой ставке. Пока в симуляторе стояла константа
из параметра, такие стратегии мерить было бессмысленно.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.data.funding import FundingRate, FundingStore, rates_lookup

T0 = datetime(2026, 1, 1, tzinfo=UTC)
EIGHT = timedelta(hours=8)


def _rates(n: int, start: datetime = T0, rate: str = "0.0001") -> list[FundingRate]:
    return [FundingRate(ts=start + EIGHT * i, rate=Decimal(rate)) for i in range(n)]


def test_write_and_read_roundtrip(tmp_path):
    store = FundingStore(tmp_path)

    assert store.write("binance", "BTC/USDT:USDT", _rates(9)) == 9
    rows = store.read("binance", "BTC/USDT:USDT", T0, T0 + timedelta(days=3))

    assert len(rows) == 9
    assert rows[0].ts == T0
    assert rows[0].rate == Decimal("0.0001")
    assert store.count("binance", "BTC/USDT:USDT") == 9


def test_write_is_idempotent(tmp_path):
    """Повторная запись тех же выплат ничего не задваивает — как и у свечей."""
    store = FundingStore(tmp_path)
    store.write("binance", "BTC/USDT:USDT", _rates(6))

    assert store.write("binance", "BTC/USDT:USDT", _rates(6)) == 0
    assert store.count("binance", "BTC/USDT:USDT") == 6


def test_later_write_wins_for_the_same_moment(tmp_path):
    """Площадка уточнила ставку — берём последнюю запись, а не первую."""
    store = FundingStore(tmp_path)
    store.write("binance", "BTC/USDT:USDT", [FundingRate(ts=T0, rate=Decimal("0.0001"))])
    store.write("binance", "BTC/USDT:USDT", [FundingRate(ts=T0, rate=Decimal("0.0005"))])

    rows = store.read("binance", "BTC/USDT:USDT", T0, T0 + EIGHT)
    assert len(rows) == 1
    assert rows[0].rate == Decimal("0.0005")


def test_months_are_separate_partitions(tmp_path):
    """Раскладка по месяцам — та же, что у свечей: длинная история не лежит одним файлом."""
    store = FundingStore(tmp_path)
    store.write("binance", "BTC/USDT:USDT", _rates(200))  # больше двух месяцев по 8 часов

    files = store.partition_files("binance", "BTC/USDT:USDT")
    assert len(files) >= 2
    assert {f.stem for f in files} >= {"2026-01", "2026-02"}


def test_window_read_excludes_the_right_edge(tmp_path):
    store = FundingStore(tmp_path)
    store.write("binance", "BTC/USDT:USDT", _rates(6))

    rows = store.read("binance", "BTC/USDT:USDT", T0, T0 + EIGHT * 3)
    assert [r.ts for r in rows] == [T0, T0 + EIGHT, T0 + EIGHT * 2]


def test_empty_store_answers_without_error(tmp_path):
    """Пустое хранилище — это «истории нет», а не отказ: замер пойдёт по константе."""
    store = FundingStore(tmp_path)

    assert store.read("binance", "BTC/USDT:USDT", T0, T0 + EIGHT) == []
    assert store.count("binance", "BTC/USDT:USDT") == 0
    assert store.last_ts("binance", "BTC/USDT:USDT") is None


def test_lookup_is_keyed_by_moment(tmp_path):
    lookup = rates_lookup(_rates(3))
    assert lookup[T0 + EIGHT] == Decimal("0.0001")
    assert (T0 + EIGHT * 5) not in lookup
