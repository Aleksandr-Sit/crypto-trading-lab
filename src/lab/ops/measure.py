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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from lab.contracts import Candle, MeasureMode
from lab.contracts.timeframes import parse_tf
from lab.core.measure.types import PhaseStat, Stability

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
    from lab.config import load_threshold
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
        # С чем сравнивать — зависит от ветки: споту альтернатива биткоин, нейтральным
        # стратегиям — кэш. Единый бенчмарк отбраковывал бы всё, что не растёт вместе с рынком.
        bench_cfg = load_threshold().benchmark
        kind = _benchmark_kind(record.branch, bench_cfg)

        kwargs: dict[str, Any] = dict(
            strategy=strategy,
            branch=record.branch,
            rung=record.rung,
            params=dict(record.params or {}),
            session=session,
            benchmark=_benchmark(
                store,
                record.venue,
                tf,
                window,
                kind,
                risk_free=getattr(bench_cfg, "risk_free_annual_pct", None),
            ),
            extra_metrics={"benchmark_kind": kind},
            funding_history=_funding_history(record, window, root),
            positioning_history=_positioning_history(record, window, root),
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


def _funding_history(
    record: Any, window: tuple[datetime, datetime], root: str | None
) -> dict[str, dict[datetime, Decimal]] | None:
    """Реальные ставки фандинга по инструментам записи — по ИМЕНИ инструмента, не по ветке.

    У спота фандинга нет вовсе, поэтому читаем только для перпов (двоеточие в имени ccxt).
    Ветка при этом ни при чём: спотовая стратегия может держать перп в списке как ИСТОЧНИК
    СИГНАЛА, не торгуя его, — так кросс-моментум смотрит на ставку фандинга, чтобы понять
    фазу рынка. Пока фильтр стоял по ветке, такая стратегия ставок не получала вовсе.

    Истории нет в хранилище — вернём None, и движок начислит по константе из параметров:
    это видно в счётчиках движка, а не выдаётся за настоящие данные.
    """
    from lab.data.funding import FundingStore, rates_lookup
    store = FundingStore(root or data_root())
    out: dict[str, dict[datetime, Decimal]] = {}
    for instrument in record.instruments or []:
        if ":" not in instrument:
            continue  # спот: фандинга нет, читать нечего
        try:
            rows = store.read(record.venue, instrument, window[0], window[1])
        except Exception as err:  # noqa: BLE001 — битое хранилище не роняет замер
            log.info("История фандинга %s %s: %s", record.venue, instrument, err)
            continue
        if rows:
            out[instrument] = rates_lookup(rows)
    return out or None


def _positioning_history(
    record: Any, window: tuple[datetime, datetime], root: str | None
) -> dict[str, dict[datetime, dict[str, Decimal]]] | None:
    """Метрики позиционирования по инструментам записи, сведённые к СУТКАМ.

    Пятиминутный шаг источника для правил слишком шумен: у открытого интереса внутри дня
    ходят проценты, а информативно движение за дни. Ключ — полночь UTC, то есть время
    дневного бара; на других таймфреймах события просто не совпадут и правило смолчит.

    Собрано не для всех инструментов: помесячных файлов у метрик нет, и год истории одного
    символа стоит 365 запросов. Нет данных — None, стратегия не получит события.
    """
    from datetime import datetime as _dt

    from lab.data.positioning import PositioningStore, daily_mean

    store = PositioningStore(root or data_root())
    out: dict[str, dict[datetime, dict[str, Decimal]]] = {}
    fields = (
        "open_interest",
        "open_interest_value",
        "top_accounts_ratio",
        "top_positions_ratio",
        "accounts_ratio",
        "taker_ratio",
    )
    for instrument in record.instruments or []:
        if ":" not in instrument:
            continue  # метрики есть только у срочных контрактов
        try:
            rows = store.read(record.venue, instrument, window[0], window[1])
        except Exception as err:  # noqa: BLE001 — битое хранилище не роняет замер
            log.info("Метрики %s %s: %s", record.venue, instrument, err)
            continue
        if not rows:
            continue
        by_field = {name: daily_mean(rows, name) for name in fields}
        days: dict[datetime, dict[str, Decimal]] = {}
        for day in sorted(by_field["open_interest"]):
            moment = _dt(day.year, day.month, day.day, tzinfo=window[0].tzinfo)
            days[moment] = {name: by_field[name][day] for name in fields if day in by_field[name]}
        out[instrument] = days
    return out or None


def _build_strategy(record: Any):
    """Код правил по id реестра; параметры записи накладываются поверх манифеста кода.

    `params["code_id"]` — те же правила под другой записью: так заводится копия стратегии
    на другой площадке или другой истории (`scripts/register_coded_strategy.py --slug-suffix`).
    Без этой ссылки реестр кода не знает id копии и замер отвечает «нет кода правил».
    """
    from lab.strategies import registry as code_registry

    params = dict(record.params or {})
    code_id = str(params.get("code_id") or record.id)
    # Что именно торгуем — из записи реестра, а не из карточки: `core.measure.run` берёт
    # инструмент и таймфрейм из манифеста стратегии, и без этого копия на другом ряду
    # искала бы инструмент исходной площадки.
    overrides = {
        "slug": record.slug,
        "venue": record.venue,
        "instruments": list(record.instruments),
        "stop": record.stop,
    }
    if record.timeframe:
        overrides["timeframe"] = record.timeframe
    try:
        return code_registry.build(code_id, params=params, overrides=overrides)
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


BENCHMARK_KINDS = ("btc_bh", "btc_dca", "cash", "none")


def _benchmark_kind(branch: str, cfg: Any) -> str:
    """С чем сравнивать ветку — из `config/threshold.yaml`.

    Единый бенчмарк «BTC купил и держи» — неверный судья для системы, которая должна
    работать на всех фазах рынка: стратегия, не зависящая от направления, на бычьем окне
    проиграет биткоину и будет отброшена, хотя именно она нужна в боковике и падении.
    Поэтому у каждой ветки своя альтернатива: спот сравнивается с биткоином (он и правда
    альтернатива), нейтральные ветки — с кэшем («не делать ничего»).
    """
    by_branch = getattr(cfg, "by_branch", {}) or {}
    return str(by_branch.get(str(branch), getattr(cfg, "default", "btc_bh")))


def _dca_pct(store: Any, venue: str, tf: str, window: tuple[datetime, datetime]) -> Decimal | None:
    """Доходность равномерных докупок (DCA) за окно, %.

    Зачем отдельно от B&H: «купил и держи» подразумевает вход одной суммой в конкретный
    день, и её результат сильно зависит от того, куда попал этот день. Владелец пополняет
    счёт по частям, поэтому честная альтернатива его деньгам — равные покупки раз в месяц.

    Считается по первой цене каждого месяца: на каждую покупку тратится одна и та же сумма,
    итог — стоимость накопленного количества по последней цене окна.
    """
    for instrument in BENCHMARK_INSTRUMENTS:
        rows = _monthly_opens(store, venue, instrument, tf, window)
        if len(rows) < 2:
            continue
        qty = Decimal(0)
        spent = Decimal(0)
        for price in rows[:-1]:  # последняя точка — цена оценки, не покупка
            if price <= 0:
                continue
            qty += Decimal(1) / price
            spent += Decimal(1)
        if spent <= 0:
            continue
        return (qty * rows[-1] / spent - 1) * 100
    return None


def _monthly_opens(
    store: Any, venue: str, instrument: str, tf: str, window: tuple[datetime, datetime]
) -> list[Decimal]:
    """Первая цена каждого месяца окна — агрегацией в хранилище, а не чтением всего ряда."""
    if store is None or not hasattr(store, "query"):
        return []
    try:
        rows = store.query(
            "select date_trunc('month', ts) as m, arg_min(open, ts) as price "
            "from {candles} where ts >= ? and ts < ? group by 1 order by 1",
            venue,
            instrument,
            tf,
            params=[window[0], window[1]],
        )
    except Exception as err:  # noqa: BLE001 — битое хранилище не роняет замер
        log.info("Помесячные цены %s %s %s: %s", venue, instrument, tf, err)
        return []
    return [Decimal(str(r["price"])) for r in rows if r.get("price") is not None]
RISK_FREE_ANNUAL_PCT = Decimal("4.0")
"""Ставка «денег без риска» в год, % — то, с чем на самом деле конкурирует нейтральная
стратегия.

Ноль был бы враньём в пользу стратегии: доллар не лежит мёртвым грузом, он приносит
процент в казначейских бумагах или на депозите. С нулевым «кэшем» кэш-энд-керри с +4.40%
годовых формально «обошёл бенчмарк», хотя на деле лишь сравнялся с банковской ставкой,
и порог отвечал `passed` там, где честный ответ — «не хуже, чем ничего не делать».

Значение — ДОПУЩЕНИЕ, а не факт: настоящая ставка менялась от 0.05% в 2021 до 5.4% в 2023.
Переопределяется в `config/threshold.yaml` (`benchmark.risk_free_annual_pct`), и это
осознанно оставлено одним числом: ряд ставок — отдельный источник данных, которого
в лаборатории нет.
"""


def risk_free_pct(
    window: tuple[datetime, datetime], annual_pct: Decimal | None = None
) -> Decimal:
    """Сколько дали бы те же деньги без риска за это окно, %.

    Процент СЛОЖНЫЙ, а не простой: купон казначейских бумаг реинвестируется, и на длинном
    окне разница не косметическая — 4% годовых за 5.5 лет дают 24.1%, а не 22.0%. Простой
    процент занижал бы планку в пользу стратегии, то есть повторял бы ту же ошибку, что
    и ноль, только меньшего размера.
    """
    days = (window[1] - window[0]).total_seconds() / 86400
    if days <= 0:
        return Decimal(0)
    rate = RISK_FREE_ANNUAL_PCT if annual_pct is None else annual_pct
    growth = (1 + float(rate) / 100) ** (days / 365)
    return Decimal(repr((growth - 1) * 100))


def _benchmark(
    store: Any,
    venue: str,
    tf: str,
    window: tuple[datetime, datetime],
    kind: str = "btc_bh",
    risk_free: Decimal | None = None,
) -> list[Candle] | Decimal | None:
    """Альтернатива, с которой сравнивают стратегию. Что именно — зависит от ветки.

    `btc_bh` — купить биткоин в начале окна и держать (свечи краёв окна);
    `btc_dca` — равные докупки раз в месяц: ближе к тому, как деньги приходят на самом деле,
      и не зависит от удачности одной даты входа;
    `cash` — ничего не делать (0%): честная альтернатива для стратегий, не зависящих от
      направления рынка, — их незачем мерить биткоином;
    `none` — сравнения нет, критерий не считается.

    Для `btc_bh` берём ТОЛЬКО края окна: `btc_buy_and_hold_pct` считает
    `последний close / первый open`, остальной ряд не нужен никому — а на минутках это
    второй такой же гигабайт памяти, как у самой стратегии.
    """
    if kind == "none":
        return None
    if kind == "cash":
        return risk_free_pct(window, risk_free)
    if kind == "btc_dca":
        return _dca_pct(store, venue, tf, window)
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
    """`(instrument, tf, from, to) -> candles` поверх фида площадки; отказ → `incomplete`.

    Фид создаётся ОДИН раз на замер и переиспользуется: `core.measure.run` спрашивает окно
    кусками, а сборка клиента ccxt тянет список рынков площадки (мегабайты). С новым клиентом
    на каждый кусок замер на минутках растягивался с минут до десятков минут.
    """
    factory = feed_factory or _default_feed_factory
    cached: list[Any] = []

    def source(instrument: str, tf: str, from_ts: datetime, to_ts: datetime) -> Sequence[Candle]:
        if cached:
            feed = cached[0]
        else:
            try:
                feed = factory(venue)
            except Exception as err:  # noqa: BLE001
                raise ConnectionError(f"фид {venue} не собран: {err}") from err
            cached.append(feed)
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


# Фаза рынка по среднегодовому росту биткоина в окне. Границы — не истина, а рабочая
# разметка: между «рос» и «падал» есть широкая полоса, где стратегии живут иначе.
PHASE_UP_PCT = Decimal(15)
PHASE_DOWN_PCT = Decimal(-15)


def market_phase(btc_cagr: Decimal | None) -> str:
    """рост | падение | боковик | неизвестно — по поведению биткоина, а не бенчмарка ветки.

    Считается всегда по BTC, даже когда ветка сравнивается с кэшем: фаза — свойство рынка,
    а не выбранной альтернативы.
    """
    if btc_cagr is None:
        return "неизвестно"
    if btc_cagr >= PHASE_UP_PCT:
        return "рост"
    if btc_cagr <= PHASE_DOWN_PCT:
        return "падение"
    return "боковик"
def run_stability(
    strategy_id: str,
    *,
    session_scope: Callable[[], Any],
    now: datetime,
    window_days: int,
    step_days: int,
    store: Any = None,
    feed_factory: Callable[[str], Any] | None = None,
    root: str | None = None,
    history_days: int | None = None,
) -> tuple[Stability, list[tuple[datetime, datetime, Any]]]:
    """Прогон по скользящим окнам: в скольких стратегия в плюсе и в скольких обошла бенчмарк.

    Зачем: одиночное окно даёт вердикт, который меняется от сдвига границы, — у
    `pifagor-forever-sma` он гулял от «преимущество» до «разгрома». Каждое окно судится
    в своём режиме рынка, поэтому неповторимая эпоха роста остаётся одним окном из многих.

    Снимки НЕ сохраняются (`session=None`): это оценка, а не решение ступени; писать
    в историю два десятка замеров на каждую проверку — мусор.
    """
    from lab.core.registry import Registry
    from lab.data import CandleStore

    # Хранилище и запись читаются ОДИН раз на весь прогон: окон бывают десятки, и создавать
    # store с нуля на каждое — лишняя работа на ровном месте.
    store = store if store is not None else CandleStore(root or data_root())
    with session_scope() as session:
        record = Registry(session).get(strategy_id)
        venue, tf = record.venue, record.timeframe or "1h"

    span = history_days or (window_days * 4)
    start = now - timedelta(days=span)
    windows: list[tuple[datetime, datetime, Any]] = []
    phases: dict[str, PhaseStat] = {}
    profitable = ahead = compared = 0
    cursor = start
    while cursor + timedelta(days=window_days) <= now:
        upto = cursor + timedelta(days=window_days)
        m = run_measure(
            strategy_id,
            MeasureMode.BACKTEST,
            (cursor, upto),
            session_scope=session_scope,
            store=store,
            feed_factory=feed_factory,
            root=root,
            session=None,
        )
        windows.append((cursor, upto, m))
        mt = getattr(m, "metrics", None)
        if getattr(m, "status", "") == "ok" and mt is not None:
            is_profit = mt.net_pnl_pct > 0
            edge = mt.vs_benchmark_cagr if mt.vs_benchmark_cagr is not None else mt.vs_benchmark
            is_ahead = edge is not None and edge > 0
            profitable += 1 if is_profit else 0
            compared += 1 if edge is not None else 0
            ahead += 1 if is_ahead else 0
            phase = market_phase(_btc_cagr(store, venue, tf, (cursor, upto), mt))
            stat = phases.get(phase, PhaseStat())
            phases[phase] = PhaseStat(
                windows=stat.windows + 1,
                profitable=stat.profitable + (1 if is_profit else 0),
                ahead=stat.ahead + (1 if is_ahead else 0),
            )
        cursor += timedelta(days=step_days)

    stability = Stability(
        windows=len(windows),
        profitable=profitable,
        ahead=ahead,
        compared=compared,
        window_days=window_days,
        step_days=step_days,
        phases=phases,
    )
    return stability, windows


def _btc_cagr(
    store: Any, venue: str, tf: str, window: tuple[datetime, datetime], mt: Any
) -> Decimal | None:
    """Годовой рост биткоина в окне — для разметки фазы рынка.

    Считается ВСЕГДА по биткоину, даже когда ветка сравнивается с кэшем: фаза — свойство
    рынка, а не выбранной альтернативы. Брать её из бенчмарка было ошибкой: у перпов
    бенчмарк «кэш», и все окна помечались «неизвестно» — ровно там, где разбивка по фазам
    нужнее всего, у стратегий, не зависящих от направления.

    Стоит это двух свечей: у краёв окна, тем же путём, что и обычный бенчмарк.
    """
    from lab.core.measure.metrics import annualized_pct, btc_buy_and_hold_pct

    if str(getattr(mt, "benchmark_kind", "")) == "btc_bh" and mt.benchmark_cagr_pct is not None:
        return mt.benchmark_cagr_pct
    edges = _benchmark(store, venue, tf, window, "btc_bh")
    if not isinstance(edges, list) or not edges:
        return None
    return annualized_pct(btc_buy_and_hold_pct(edges), window)


__all__ = [
    "BENCHMARK_INSTRUMENTS",
    "DATA_ROOT_ENV",
    "MeasureUnavailable",
    "data_root",
    "make_measure",
    "run_measure",
    "run_stability",
]
