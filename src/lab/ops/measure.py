"""Обёртка над `core.measure.run` — то, чем система меряет сама (R11, R12, R14).

Движок замера ничего не знает про базу, хранилище и фиды: ему дают стратегию, свечи и
бенчмарк. Эта обёртка их и собирает, одна на всех вызывающих:

- `ops.worker.Worker.measure` → задание `remeasure` (вс 22:00) и `discovery.jobs(measure=...)`;
- кнопка «В замер» в боте → `discovery.candidate_hook(measure=...)`;
- `lab measure run <strategy_id>` — то же самое руками.

Откуда что берётся:
- стратегия — `strategies.registry.build(id, params)` (код правил), параметры — из записи
  реестра базы, чтобы снимок повторял то, что реально стоит на ступени;
- свечи — `data.CandleStore` (`LAB_DATA_ROOT`, по умолчанию `data/`), а если хранилище пусто
  — фид площадки; отказ фида превращается в `incomplete` с причиной, а не в исключение;
- бенчмарк — BTC той же площадки и того же таймфрейма из хранилища; нет BTC — метрики
  относительно бенчмарка честно помечаются `NotApplicable`;
- форвард и микро меряются по закрытым сделкам журнала, а не по симуляции.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from lab.contracts import Candle, MeasureMode
from lab.contracts.timeframes import parse_tf

log = logging.getLogger(__name__)

DATA_ROOT_ENV = "LAB_DATA_ROOT"
DEFAULT_DATA_ROOT = "data"
# Имена BTC на разных площадках — берём первое, по которому в хранилище есть ряд.
BENCHMARK_INSTRUMENTS: tuple[str, ...] = ("BTC/USDT", "BTC/USD", "BTCUSDT", "BTC/USDC", "BTC")

# Сколько баров пробовать с каждого края окна, чтобы найти первую и последнюю свечу
# бенчмарка: ряд может начинаться позже левой границы или обрываться раньше правой.
EDGE_BARS = 500
FROM_JOURNAL = (MeasureMode.MICRO, MeasureMode.FORWARD)


class MeasureUnavailable(RuntimeError):
    """Мерить нечем: у стратегии нет кода правил, а ветка требует симуляции."""


def data_root() -> str:
    return os.environ.get(DATA_ROOT_ENV, "").strip() or DEFAULT_DATA_ROOT


def make_measure(
    session_scope: Callable[[], Any],
    *,
    store: Any = None,
    feed_factory: Callable[[str], Any] | None = None,
    root: str | None = None,
) -> Callable[..., Any]:
    """Замыкание `measure(strategy_id=, mode=, window=)` — сигнатура заданий и хуков."""

    def measure(*, strategy_id: str, mode: str | MeasureMode, window, **extra: Any):
        return run_measure(
            strategy_id,
            mode,
            window,
            session_scope=session_scope,
            store=store,
            feed_factory=feed_factory,
            root=root,
            **extra,
        )

    return measure


def run_measure(
    strategy_id: str,
    mode: str | MeasureMode,
    window: tuple[datetime, datetime],
    *,
    session_scope: Callable[[], Any],
    store: Any = None,
    feed_factory: Callable[[str], Any] | None = None,
    root: str | None = None,
    **extra: Any,
):
    """Один замер стратегии из реестра. Возврат — `core.measure.Measurement`."""
    from lab.core.measure import run as core_run
    from lab.core.registry import Registry
    from lab.data import CandleStore

    store = store if store is not None else CandleStore(root or data_root())
    with session_scope() as session:
        record = Registry(session).get(strategy_id)  # StrategyNotFound — наверх, как есть
        instrument = record.instruments[0] if record.instruments else "*"
        tf = record.timeframe or "1h"
        strategy = _build_strategy(record)
        mode = _mode_for(MeasureMode(mode), strategy, record)

        kwargs: dict[str, Any] = dict(
            strategy=strategy,
            branch=record.branch,
            rung=record.rung,
            params=dict(record.params or {}),
            session=session,
            benchmark=_benchmark(store, record.venue, tf, window),
        )
        if mode in FROM_JOURNAL:
            kwargs["trades"] = _journal_trades(session, strategy_id, window)
        else:
            if _has_candles(store, record.venue, instrument, tf):
                kwargs["source"] = _store_source(store, record.venue)
            else:
                kwargs["source"] = _feed_source(record.venue, feed_factory)
        kwargs.update(extra)
        return core_run(strategy_id, mode, window, **kwargs)


# -- стратегия ---------------------------------------------------------------------------


def _build_strategy(record: Any):
    """Код правил по id реестра; параметры записи накладываются поверх манифеста кода."""
    from lab.strategies import registry as code_registry

    try:
        return code_registry.build(record.id, params=dict(record.params or {}))
    except code_registry.UnknownStrategy:
        return None


def _mode_for(mode: MeasureMode, strategy: Any, record: Any) -> MeasureMode:
    """Без кода правил симулировать нечего: форвард-ветку меряем по сделкам журнала."""
    if strategy is not None:
        return mode
    from lab.core.measure import measure_plan

    if measure_plan(record.branch).can_backtest and mode not in FROM_JOURNAL:
        raise MeasureUnavailable(
            f"нет кода правил для {record.id}: ветка {record.branch} бэктестится, "
            "но `strategies.registry.build` её не знает — заведи пресет или карточку"
        )
    if mode not in FROM_JOURNAL:
        log.info(
            "Стратегия %s без кода правил (ветка %s только форвард): режим %s → forward",
            record.id,
            record.branch,
            mode,
        )
        return MeasureMode.FORWARD
    return mode


# -- данные ------------------------------------------------------------------------------


def _from_store(
    store: Any, venue: str, instrument: str, tf: str, window: tuple[datetime, datetime]
) -> list[Candle]:
    if store is None or instrument == "*":
        return []
    try:
        return list(store.read(venue, instrument, tf, window[0], window[1]))
    except Exception as err:  # noqa: BLE001 — пустое/битое хранилище не роняет замер
        log.info("Хранилище свечей %s %s %s: %s", venue, instrument, tf, err)
        return []


def _store_source(store: Any, venue: str) -> Any:
    """Источник свечей из хранилища с сигнатурой `Feed.candles`.

    Отдаём именно источник, а не готовый список: `core.measure.run` спрашивает окно
    кусками и держит в памяти только текущий кусок. Год минутного ряда списком —
    это больше гигабайта и OOM, а по кускам — десятки мегабайт.
    """

    def source(instrument: str, tf: str, from_ts: datetime, to_ts: datetime) -> list[Candle]:
        return _from_store(store, venue, instrument, tf, (from_ts, to_ts))

    return source


def _has_candles(store: Any, venue: str, instrument: str, tf: str) -> bool:
    """Есть ли ряд в хранилище вообще — без чтения самого ряда."""
    if store is None or instrument == "*":
        return False
    try:
        return bool(store.count(venue, instrument, tf))
    except Exception as err:  # noqa: BLE001 — пустое/битое хранилище не роняет замер
        log.info("Хранилище свечей %s %s %s: %s", venue, instrument, tf, err)
        return False


def _benchmark(
    store: Any, venue: str, tf: str, window: tuple[datetime, datetime]
) -> list[Candle] | None:
    """BTC той же площадки и таймфрейма — база сравнения «лучше ли, чем просто держать BTC».

    Берём ТОЛЬКО края окна: `btc_buy_and_hold_pct` считает `последний close / первый open`,
    остальной ряд не используется никем — а на минутках это второй такой же гигабайт памяти,
    как у самой стратегии. В отпечаток данных попадают те же две свечи: они и определяют
    вклад бенчмарка в результат.
    """
    for instrument in BENCHMARK_INSTRUMENTS:
        if not _has_candles(store, venue, instrument, tf):
            continue
        edges = _edge_candles(store, venue, instrument, tf, window)
        if edges:
            return edges
    return None


def _edge_candles(
    store: Any, venue: str, instrument: str, tf: str, window: tuple[datetime, datetime]
) -> list[Candle]:
    """Первая и последняя свеча окна, без чтения середины.

    Границы спрашиваем у хранилища запросом (min/max ts), а не «первые 500 баров»: ряд
    бенчмарка может начинаться сильно позже левой границы окна или обрываться задолго
    до правой — тогда поиск по краям вернул бы пусто, и сравнение с BTC потерялось бы
    на ровном месте. Хранилище без `query` (фейки в тестах) читается как раньше, целиком.
    """
    step = parse_tf(tf)
    edges = _edge_ts(store, venue, instrument, tf, window)
    if edges is None:
        rows = _from_store(store, venue, instrument, tf, window)
        return [rows[0], rows[-1]] if rows else []
    first_ts, last_ts = edges
    first = _from_store(store, venue, instrument, tf, (first_ts, first_ts + step))
    last = _from_store(store, venue, instrument, tf, (last_ts, last_ts + step))
    if not first or not last:
        return []
    return [first[0], last[-1]]


def _edge_ts(
    store: Any, venue: str, instrument: str, tf: str, window: tuple[datetime, datetime]
) -> tuple[datetime, datetime] | None:
    """Время первой и последней свечи в окне; None — если хранилище не умеет запросы."""
    if not hasattr(store, "query"):
        return None
    try:
        rows = store.query(
            "select min(ts) as first_ts, max(ts) as last_ts from {candles} "
            "where ts >= ? and ts < ?",
            venue,
            instrument,
            tf,
            params=[window[0], window[1]],
        )
    except Exception as err:  # noqa: BLE001 — битое хранилище не роняет замер
        log.info("Границы ряда %s %s %s: %s", venue, instrument, tf, err)
        return None
    if not rows or rows[0].get("first_ts") is None:
        return None
    return rows[0]["first_ts"], rows[0]["last_ts"]


def _feed_source(venue: str, feed_factory: Callable[[str], Any] | None):
    """`(instrument, tf, from, to) -> candles` поверх фида площадки; отказ → `incomplete`."""
    factory = feed_factory or _default_feed_factory

    def source(instrument: str, tf: str, from_ts: datetime, to_ts: datetime) -> Sequence[Candle]:
        try:
            feed = factory(venue)
        except Exception as err:  # noqa: BLE001
            raise ConnectionError(f"фид {venue} не собран: {err}") from err
        if feed is None:
            raise ConnectionError(
                f"нет свечей {venue} {instrument} {tf} в хранилище и фид недоступен — "
                "сначала `lab data backfill`"
            )
        try:
            return list(feed.candles(instrument, tf, from_ts, to_ts))
        except (OSError, ConnectionError, TimeoutError):
            raise
        except Exception as err:  # noqa: BLE001 — любой отказ источника = нет данных
            raise ConnectionError(f"фид {venue} не отдал свечи: {err}") from err

    return source


def _default_feed_factory(venue: str):
    from lab.feeds.cex.feed import FEEDS, make_feed

    if venue not in FEEDS:
        return None
    return make_feed(venue)


def _journal_trades(session: Any, strategy_id: str, window: tuple[datetime, datetime]) -> list:
    """Закрытые сделки окна → `ClosedTrade` для форвард/микро-замера."""
    from lab.core.journal import Journal
    from lab.core.measure.types import ClosedTrade

    out: list[ClosedTrade] = []
    for rec in Journal(session).closed_trades(strategy_id):
        if rec.closed_at is None or rec.exit_price is None:
            continue
        if not (window[0] <= rec.closed_at <= window[1]):
            continue
        out.append(
            ClosedTrade(
                instrument=rec.instrument,
                side=rec.side,
                qty=rec.qty,
                entry_price=rec.entry_price,
                exit_price=rec.exit_price,
                opened_at=rec.opened_at,
                closed_at=rec.closed_at,
                pnl_gross=rec.pnl_gross,
                costs=rec.costs,
            )
        )
    return out


__all__ = [
    "BENCHMARK_INSTRUMENTS",
    "DATA_ROOT_ENV",
    "MeasureUnavailable",
    "data_root",
    "make_measure",
    "run_measure",
]
