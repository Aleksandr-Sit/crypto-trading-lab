"""Шов R05/R05.1: бэкфилл свечей CEX в Parquet с прогрессом, прерыванием/возобновлением;
Binance — из архивов data.binance.vision с добором хвоста по REST. Сети нет."""

import io
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import ccxt

from lab.contracts import Candle
from lab.data import CandleStore
from lab.data.backfill_cex import BinanceArchive, backfill_venue
from lab.feeds import MemoryFeedsRegistry
from lab.feeds.cex import FakeTransport

PERP = "BTC/USDT:USDT"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _fake(n_hours: int, venue: str = "bybit") -> FakeTransport:
    t = FakeTransport(venue)
    t.seed_ohlcv(PERP, "1h", T0, n_hours, start_price=Decimal("50000"))
    return t


def test_backfill_days_with_progress_and_quota(tmp_path):
    store = CandleStore(tmp_path)
    quota = MemoryFeedsRegistry()
    seen: list[tuple[str, int, int]] = []
    results = backfill_venue(
        store,
        "bybit",
        [PERP],
        "1h",
        days=2,
        transport=_fake(72),
        now=T0 + timedelta(hours=48),
        quota=quota,
        progress=lambda instr, done, total: seen.append((instr, done, total)),
    )
    assert len(results) == 1 and results[0].error is None
    assert results[0].result.rows_written == 48
    assert store.count("bybit", PERP, "1h") == 48
    assert store.read("bybit", PERP, "1h", T0, T0 + timedelta(hours=48))[-1].ts == T0 + timedelta(
        hours=47
    )
    assert seen[-1] == (PERP, 48, 48) and all(d <= t for _, d, t in seen)
    assert quota.calls["bybit"] >= 1


def test_backfill_interrupted_then_resumed_without_duplicates(tmp_path):
    store = CandleStore(tmp_path)
    t = _fake(72)
    now = T0 + timedelta(hours=72)
    t.fail_next = ccxt.NetworkError("bybit: connection lost")
    first = backfill_venue(
        store, "bybit", [PERP], "1h", days=3, transport=t, now=now, chunk=timedelta(days=1)
    )
    assert first[0].error and "connection lost" in first[0].error
    assert first[0].resume_from == T0 and store.count("bybit", PERP, "1h") == 0

    t.fail_next = None
    t.calls.clear()
    # обрыв после первого дня
    t.ohlcv_fail_after = 1
    second = backfill_venue(
        store, "bybit", [PERP], "1h", days=3, transport=t, now=now, chunk=timedelta(days=1)
    )
    assert (
        second[0].resume_from == T0 + timedelta(days=1) and store.count("bybit", PERP, "1h") == 24
    )

    third = backfill_venue(
        store, "bybit", [PERP], "1h", days=3, transport=t, now=now, chunk=timedelta(days=1)
    )
    assert third[0].error is None and third[0].result.resumed_from == T0 + timedelta(days=1)
    assert third[0].result.rows_written == 48 and store.count("bybit", PERP, "1h") == 72

    fourth = backfill_venue(store, "bybit", [PERP], "1h", days=3, transport=t, now=now)
    assert fourth[0].result.skipped is True


def _zip_csv(rows: list[list], header: bool) -> bytes:
    buf = io.StringIO()
    if header:
        buf.write(
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,tb_base,tb_quote,ignore\n"
        )
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("x.csv", buf.getvalue())
    return out.getvalue()


def test_binance_backfill_uses_archives_and_rest_for_current_month(tmp_path):
    urls: list[str] = []
    start = datetime(2025, 12, 1, tzinfo=UTC)
    dec_rows = [
        [int((start + timedelta(hours=i)).timestamp() * 1000), 1, 2, 0.5, 1.5, 10, 0, 0, 0, 0, 0, 0]
        for i in range(31 * 24)
    ]
    # с 2025 спотовые архивы Binance пишут open_time в микросекундах и с заголовком
    spot_rows = [[r[0] * 1000, *r[1:]] for r in dec_rows]

    def fetch(url: str) -> bytes | None:
        urls.append(url)
        if url.endswith("2025-12.zip"):
            return _zip_csv(spot_rows if "/spot/" in url else dec_rows, header="/spot/" in url)
        return None  # текущий месяц в архиве ещё не лежит (404)

    t = _fake(0, venue="binance")
    t.seed_ohlcv(PERP, "1h", T0, 48, start_price=Decimal("50000"))
    t.seed_ohlcv("BTC/USDT", "1h", T0, 48, start_price=Decimal("50000"))
    store = CandleStore(tmp_path)
    now = T0 + timedelta(hours=48)
    results = backfill_venue(
        store,
        "binance",
        [PERP, "BTC/USDT"],
        "1h",
        days=33,
        transport=t,
        now=now,
        archive=BinanceArchive(fetch=fetch),
    )
    assert [r.error for r in results] == [None, None]
    assert store.count("binance", PERP, "1h") == 33 * 24  # декабрь из архива + 2 дня по REST
    assert store.count("binance", "BTC/USDT", "1h") == 33 * 24
    first = store.read("binance", "BTC/USDT", "1h", start, start + timedelta(hours=1))[0]
    assert first.ts == start and first.close == Decimal("1.5")
    assert store.read("binance", PERP, "1h", T0, T0 + timedelta(hours=1))[0].open == Decimal(
        "50000"
    )
    assert (
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2025-12.zip"
        in urls
    )
    assert (
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2025-12.zip"
        in urls
    )
    assert "fetch_ohlcv" in t.calls, "хвост текущего месяца добирается по REST"


def test_truncated_archive_month_is_refetched_from_rest():
    """Обрезанный месячный архив не должен молча оставлять дырку в хранилище.

    Так и было: у SOL и XRP архивы Binance не содержали последних дней февраля и марта 2022,
    код брал их как есть, и пропуск всплыл только через полгода — портфельный замер встал
    на «разрыв данных SOL/USDT:USDT между 25.02 и 01.03».
    """
    month = datetime(2022, 2, 1, tzinfo=UTC)
    full = [
        Candle(
            instrument="SOL/USDT:USDT",
            tf="1d",
            ts=month + timedelta(days=i),
            open=Decimal(100),
            high=Decimal(100),
            low=Decimal(100),
            close=Decimal(100),
            volume=Decimal(1),
        )
        for i in range(28)
    ]
    truncated = full[:25]  # архив без 26–28 февраля

    class _Archive(BinanceArchive):
        def month(self, instrument, tf, month_start):  # noqa: ARG002
            return truncated

    rest_calls: list[tuple[datetime, datetime]] = []

    def rest(instrument, tf, from_ts, to_ts):  # noqa: ARG001
        rest_calls.append((from_ts, to_ts))
        return [c for c in full if from_ts <= c.ts < to_ts]

    source = _Archive(fetch=lambda url: None).source(rest, now=datetime(2022, 6, 1, tzinfo=UTC))
    rows = source("SOL/USDT:USDT", "1d", month, month + timedelta(days=28))

    assert len(rows) == 28, "месяц должен быть добран из REST целиком"
    assert rest_calls, "к REST обязаны обратиться, а не поверить обрезанному архиву"


def test_complete_archive_month_is_not_refetched():
    """Целый месяц из архива берём как есть — лишние запросы к бирже не нужны."""
    month = datetime(2022, 4, 1, tzinfo=UTC)
    rows_in = [
        Candle(
            instrument="SOL/USDT:USDT",
            tf="1d",
            ts=month + timedelta(days=i),
            open=Decimal(100),
            high=Decimal(100),
            low=Decimal(100),
            close=Decimal(100),
            volume=Decimal(1),
        )
        for i in range(30)
    ]

    class _Archive(BinanceArchive):
        def month(self, instrument, tf, month_start):  # noqa: ARG002
            return rows_in

    def rest(instrument, tf, from_ts, to_ts):  # noqa: ARG001
        raise AssertionError("к REST обращаться не должны")

    source = _Archive(fetch=lambda url: None).source(rest, now=datetime(2022, 6, 1, tzinfo=UTC))
    assert len(source("SOL/USDT:USDT", "1d", month, month + timedelta(days=30))) == 30
