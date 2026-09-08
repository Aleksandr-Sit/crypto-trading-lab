"""Бэкфилл свечей CEX за N дней в Parquet (истории 27–28): прогресс, прерывание, возобновление.

Поверх `lab.data.backfill` (состояние в `.backfill.json`, идемпотентная запись): источник —
`CexFeed.candles` через ccxt с уважением лимитов (`enableRateLimit` + `FeedsRegistry.use`).
Binance — полные месяцы из архивов data.binance.vision (`BinanceArchive`), хвост текущего
месяца — по REST. Сетевые функции подменяемы: `transport`/`feed` и `archive.fetch`.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from lab.contracts import Candle
from lab.contracts.timeframes import parse_tf
from lab.data.backfill import BackfillInterrupted, BackfillResult, Source, backfill
from lab.data.funding import FundingRate
from lab.data.store import CandleStore
from lab.feeds.cex.feed import CexFeed, make_feed
from lab.feeds.cex.transport import Transport
from lab.feeds.quota import FeedsRegistry

log = logging.getLogger(__name__)

Fetch = Callable[[str], bytes | None]
Progress = Callable[[str, int, int], None]

ARCHIVE_BASE = "https://data.binance.vision/data"
ARCHIVE_FEED_ID = "binance-archive"


@dataclass(frozen=True)
class SymbolResult:
    venue: str
    instrument: str
    tf: str
    result: BackfillResult | None = None
    error: str | None = None
    resume_from: datetime | None = None
    rows_written: int = 0


def _http_fetch(url: str, timeout: float = 60.0) -> bytes | None:
    """Скачать архив; 404 (месяц ещё не выложен) → None."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — фиксированный https-хост
            return resp.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def _month_start(ts: datetime) -> datetime:
    return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(ts: datetime) -> datetime:
    return (ts.replace(day=28) + timedelta(days=4)).replace(day=1)


def _to_dt(raw: str) -> datetime:
    value = int(raw)
    if value > 10**14:  # микросекунды (спотовые архивы с 2025-01)
        value //= 1000
    return datetime.fromtimestamp(value / 1000, tz=UTC)


class BinanceArchive:
    """Месячные klines-архивы Binance: spot и USDⓈ-M futures. Кэш распакованных месяцев в памяти."""

    def __init__(self, fetch: Fetch | None = None, *, quota: FeedsRegistry | None = None) -> None:
        self.fetch = fetch or _http_fetch
        self.quota = quota
        self._months: dict[tuple[str, str, datetime], list[Candle] | None] = {}

    @staticmethod
    def url(instrument: str, tf: str, month: datetime) -> str:
        base, quote = instrument.split("/")
        quote = quote.split(":")[0]
        symbol = f"{base}{quote}".upper()
        kind = "futures/um" if ":" in instrument else "spot"
        return f"{ARCHIVE_BASE}/{kind}/monthly/klines/{symbol}/{tf}/{symbol}-{tf}-{month:%Y-%m}.zip"

    def month(self, instrument: str, tf: str, month: datetime) -> list[Candle] | None:
        key = (instrument, tf, month)
        if key not in self._months:
            if self.quota is not None:
                self.quota.use(ARCHIVE_FEED_ID, 1)
            data = self.fetch(self.url(instrument, tf, month))
            self._months[key] = None if data is None else self._parse(instrument, tf, data)
        return self._months[key]

    @staticmethod
    def _parse(instrument: str, tf: str, data: bytes) -> list[Candle]:
        out: list[Candle] = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.endswith(".csv"))
            text = z.read(name).decode("utf-8")
        for row in csv.reader(io.StringIO(text)):
            if not row or not row[0].strip().lstrip("-").isdigit():
                continue  # заголовок (в новых архивах) или пустая строка
            out.append(
                Candle(
                    instrument=instrument,
                    tf=tf,
                    ts=_to_dt(row[0]),
                    open=Decimal(row[1]),
                    high=Decimal(row[2]),
                    low=Decimal(row[3]),
                    close=Decimal(row[4]),
                    volume=Decimal(row[5]),
                )
            )
        return out

    def source(self, rest: Source, *, now: datetime | None = None) -> Source:
        """Источник для `data.backfill`: полные прошедшие месяцы — из архива, остальное — `rest`."""
        now = now or _utcnow()

        def fetch(instrument: str, tf: str, from_ts: datetime, to_ts: datetime) -> Sequence[Candle]:
            out: list[Candle] = []
            cursor = from_ts
            while cursor < to_ts:
                month = _month_start(cursor)
                month_end = _next_month(month)
                upto = min(month_end, to_ts)
                rows = self.month(instrument, tf, month) if month_end <= now else None
                if rows is not None and not _covers(rows, tf, cursor, upto):
                    # Архив бывает обрезан с любого конца: у SOL и XRP февраль 2022
                    # заканчивался 25-м, а апрель начинался с 3-го. Дырка молча уезжала
                    # в хранилище, и портфельный замер потом вставал на «разрыв данных».
                    # Не покрывает запрошенный отрезок — берём его из REST.
                    rows = None
                if rows is None:
                    out.extend(rest(instrument, tf, cursor, upto))
                else:
                    out.extend(c for c in rows if cursor <= c.ts < upto)
                cursor = upto
            return out

        return fetch


def _covers(rows: Sequence[Candle], tf: str, cursor: datetime, upto: datetime) -> bool:
    """Покрывает ли месяц архива запрошенный отрезок целиком и без пропусков.

    Проверять надо оба конца, а не только хвост: у SOL и XRP архив за февраль 2022 обрывался
    на 25-м, а за апрель — начинался с 3-го. Проверка одного хвоста закрыла первую дырку
    и оставила вторую.

    Ложное срабатывание на первом месяце листинга (инструмента ещё не было) не страшно:
    REST вернёт ровно то же самое, цена ошибки — один лишний запрос.
    """
    if not rows:
        return False
    step = parse_tf(tf)
    if rows[0].ts > cursor or rows[-1].ts + step < upto:
        return False
    return all(b.ts - a.ts == step for a, b in zip(rows, rows[1:], strict=False))


def _utcnow() -> datetime:
    return datetime.now(UTC)


def backfill_venue(
    store: CandleStore,
    venue: str,
    symbols: Sequence[str],
    tf: str,
    days: int,
    *,
    feed: CexFeed | None = None,
    transport: Transport | None = None,
    quota: FeedsRegistry | None = None,
    archive: BinanceArchive | None = None,
    now: datetime | None = None,
    chunk: timedelta | None = None,
    progress: Progress | None = None,
) -> list[SymbolResult]:
    """Свечи `symbols` за последние `days` дней → `store`. Обрыв на символе не роняет остальные:
    результат по символу несёт `error` и `resume_from`; повторный вызов продолжает с той же
    точки."""
    feed = feed or make_feed(venue, transport, quota=quota)
    now = (now or _utcnow()).astimezone(UTC)
    step = parse_tf(tf)
    to_ts = datetime.fromtimestamp(
        (now.timestamp() // step.total_seconds()) * step.total_seconds(), tz=UTC
    )
    from_ts = to_ts - timedelta(days=days)
    # Шаг качания считается В СВЕЧАХ, а не в сутках. Архив Binance лежит помесячными
    # файлами, и для 1h это удачно — один файл ≈ 744 свечи. Для 1d тот же файл даёт 31
    # строку за HTTP-запрос: 80 запросов на пару, минута на символ, полсуток на вселенную
    # из 735 пар. Поэтому на таймфреймах от суток архив не используется вовсе — обычный
    # REST отдаёт `page_limit` свечей за раз (у Binance 1000), то есть три запроса на пару.
    page = int(getattr(feed, "page_limit", 0) or 1000)
    if venue == "binance" and step < timedelta(days=1):
        archive = archive or BinanceArchive(quota=quota)
        source = archive.source(feed.source(), now=now)
        chunk = chunk or timedelta(days=31)
    else:
        source = feed.source()
        chunk = chunk or step * page
    results: list[SymbolResult] = []
    for instrument in symbols:
        cb = (lambda done, total, _i=instrument: progress(_i, done, total)) if progress else None
        try:
            res = backfill(
                store, source, venue, instrument, tf, from_ts, to_ts, chunk=chunk, progress=cb
            )
            results.append(
                SymbolResult(venue, instrument, tf, result=res, rows_written=res.rows_written)
            )
        except BackfillInterrupted as err:
            log.warning("бэкфилл %s %s %s прерван: %s", venue, instrument, tf, err.reason)
            results.append(
                SymbolResult(
                    venue,
                    instrument,
                    tf,
                    error=err.reason,
                    resume_from=err.resume_from,
                    rows_written=err.rows_written,
                )
            )
    return results


__all__ = ["ARCHIVE_FEED_ID", "BinanceArchive", "SymbolResult", "backfill_venue"]


def backfill_funding(
    store: Any,
    venue: str,
    symbols: Sequence[str],
    days: int,
    *,
    feed: CexFeed | None = None,
    transport: Transport | None = None,
    quota: FeedsRegistry | None = None,
    now: datetime | None = None,
    progress: Callable[[str, int], None] | None = None,
) -> list[tuple[str, int, str | None]]:
    """История ставок фандинга по инструментам за последние `days` дней.

    Отдельно от свечей: у фандинга своя сетка (раз в 8 часов) и одно число вместо OHLCV.
    Обрыв на одном символе не роняет остальные — результат несёт причину, как у свечей.
    """
    feed = feed or make_feed(venue, transport, quota=quota)
    now = (now or _utcnow()).astimezone(UTC)
    from_ts = now - timedelta(days=days)
    out: list[tuple[str, int, str | None]] = []
    for symbol in symbols:
        try:
            rows = feed.funding_history(symbol, from_ts, now)
        except Exception as err:  # noqa: BLE001 — один символ не роняет прогон
            log.warning("Фандинг %s %s: %s", venue, symbol, err)
            out.append((symbol, 0, f"{type(err).__name__}: {err}"))
            continue
        written = store.write(venue, symbol, [FundingRate(ts=ts, rate=rate) for ts, rate in rows])
        out.append((symbol, written, None))
        if progress:
            progress(symbol, written)
    return out
