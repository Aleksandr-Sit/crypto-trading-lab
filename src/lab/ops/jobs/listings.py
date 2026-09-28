"""Задание `listings`: новые листинги Binance → состав шорта листингов (шаг 5 плана трейдеров).

**Зачем.** Шорт листингов торгует событие — появление монеты на споте Binance. Его состав
(`instruments`) и даты (`params.listing_dates`) лежат в записи реестра и заливались разово;
новых листингов стратегия не видит, и проверка вперёд на `paper` была бы пустой.
Задание дописывает их туда, откуда стратегия их читает, — в ту же запись.

**Правило — то же, что в исследовании** (`scripts/listing_effect.py`, D1 в
`docs/research/queue-2026-09-12.md`), иначе вперёд проверялось бы другое правило:

* день листинга — день первой дневной свечи спотовой пары к USDT;
* торгуемо, если бессрочный контракт той же базы запущен НЕ ПОЗЖЕ первого полного дня
  (`onboard <= листинг + 1`): вход — на его закрытии, шортить до запуска перпа нечем;
* перпа ещё нет — пара ждёт, пока первый полный день не кончится (Binance нередко
  запускает перп через несколько часов после спота). Потом — «без перпа», навсегда.
  Токенизированные акции (`COINB`, `AAPLB`: 20 из 23 новых пар июля–сентября 2026) уходят
  сюда же тем же правилом, без особых случаев в коде.

**Что помнится между запусками** (`system_flags`, ключ `listings_seen:binance`): итог по
каждой просмотренной паре. Без этого каждую ночь пришлось бы спрашивать первую свечу
у всех ~500 пар; с ним — только у новых, несколько в неделю.

**Граница первого запуска.** Пары, залистинговавшиеся не позже последней даты состава,
исследование уже рассмотрело и отобрало — их лента не трогает, иначе «торгуемое» по
сегодняшнему списку биржи смешалось бы с отобранным по архиву. Граница запоминается один
раз и дальше не сдвигается: иначе пара, увиденная с опозданием, выпала бы из состава.

**Время — 03:30 по Самаре (23:30 UTC)**, до ночного обновления свечей (04:05) и журнала
вперёд (04:30). Листинг дня L лента видит вечером того же дня, свечи первого полного дня
приезжают в 04:05 после его закрытия, решение о входе попадает в журнал в 04:30.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

log = logging.getLogger(__name__)

LISTINGS_JOB = "listings"
SEEN_FLAG = "listings_seen:binance"
DATES_PARAM = "listing_dates"
VENUE = "binance"
# Перп обязан существовать к ПЕРВОМУ ПОЛНОМУ дню: на его закрытии стратегия входит.
ENTRY_DAYS = 1
# Запас свечей и ставок до дня листинга. У перпа, запущенного раньше спота, без запаса
# первый бар ряда совпал бы с днём листинга, и нельзя было бы отличить «ряд начался»
# от «данные не докачаны».
MARGIN_DAYS = 3

ADDED, PENDING, NO_PERP, OLD = "added", "pending", "no_perp", "old"


@dataclass(frozen=True)
class Added:
    base: str
    instrument: str
    listed: date
    onboard: date
    strategies: tuple[str, ...]
    timeframes: tuple[str, ...] = ("1d",)

    @property
    def entry_day(self) -> date:
        """Первый полный день: на его закрытии стратегия открывает шорт."""
        return self.listed + timedelta(days=ENTRY_DAYS)

    def entry_passed(self, now: datetime) -> bool:
        close = datetime.combine(self.entry_day + timedelta(days=1), time(), tzinfo=UTC)
        return now >= close


@dataclass
class ListingsReport:
    added: list[Added] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    rejected: int = 0
    asked: int = 0
    errors: list[str] = field(default_factory=list)
    note: str | None = None

    def text(self) -> str:
        if self.note:
            return f"Листинги: {self.note}"
        parts = [f"добавлено {len(self.added)}"]
        if self.pending:
            parts.append(f"ждут перпа {len(self.pending)}")
        parts.append(f"отсеяно {self.rejected}, запросов первой свечи {self.asked}")
        if self.errors:
            parts.append(f"ошибок {len(self.errors)}")
        return "Листинги: " + ", ".join(parts)

    def card(self, now: datetime) -> str:
        """Текст тревоги в Telegram: что добавлено и когда стратегия примет решение."""
        lines = []
        for a in self.added:
            when = (
                "вход уже прошёл — только в состав для перемера"
                if a.entry_passed(now)
                else f"шорт — по закрытию {a.entry_day:%d.%m}, решение в журнале ~04:30"
            )
            lines.append(
                f"{a.base}: спот с {a.listed:%d.%m.%Y}, перп с {a.onboard:%d.%m.%Y}; {when}"
            )
        lines += [f"ошибка: {e}" for e in self.errors[:5]]
        return "Новые листинги Binance: " + "; ".join(lines)


def _as_date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _dates(row: Any) -> dict[str, date]:
    raw = (row.params_json or {}).get(DATES_PARAM)
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, date] = {}
    for instrument, value in raw.items():
        stamp = _as_date(value)
        if stamp is not None:
            out[str(instrument)] = stamp
    return out


def listing_records(session: Any) -> list[Any]:
    """Записи, чей состав — даты листингов: у них в параметрах есть `listing_dates`.

    Признак по данным, а не по id: так в ленту попадают и варианты правила
    (`--slug-suffix`), у которых состав тот же. Снятые с учёта не трогаются.
    """
    from lab.db.models import StrategyRow

    return [
        row
        for row in session.query(StrategyRow).order_by(StrategyRow.id).all()
        if row.venue == VENUE and row.status != "retired" and DATES_PARAM in (row.params_json or {})
    ]


def _append(rows: Sequence[Any], instrument: str, listed: date) -> list[str]:
    """Дописать инструмент и дату в каждую запись, где их ещё нет. Возврат — чьи записи."""
    changed: list[str] = []
    for row in rows:
        dates = _dates(row)
        if instrument in dates:
            continue
        dates[instrument] = listed
        encoded = {k: v.isoformat() for k, v in dates.items()}
        # Строкой, как пишет `listing_universe.py`: формат записи не меняется.
        row.params_json = {
            **(row.params_json or {}),
            DATES_PARAM: json.dumps(encoded, indent=1, sort_keys=True),
        }
        instruments = list(row.instruments or [])
        if instrument not in instruments:
            instruments.append(instrument)
            row.instruments = instruments
        changed.append(row.id)
    return changed


def sync_listings(session: Any, source: Any, *, now: datetime) -> ListingsReport:
    """Один проход ленты: новые пары → решение по каждой → дописать торгуемые в записи.

    Отказ списков биржи (`perps`, `spot_pairs`) пробрасывается: без них решать нечего,
    и ничего не меняется. Отказ первой свечи одной пары — пара ждёт следующего прохода.
    """
    from lab.db.models import SystemFlagRow
    from lab.feeds.cex.listings import perp_symbol

    report = ListingsReport()
    rows = listing_records(session)
    if not rows:
        report.note = "нет стратегий с датами листинга — лента не нужна"
        return report

    # Списки биржи — до любой записи: их отказ не должен оставить в базе и следа.
    perps = source.perps()
    pairs = source.spot_pairs()

    flag = session.get(SystemFlagRow, SEEN_FLAG)
    if flag is None:
        flag = SystemFlagRow(key=SEEN_FLAG, value={}, updated_by="worker")
        session.add(flag)
    state = dict(flag.value or {})
    seen: dict[str, dict[str, Any]] = dict(state.get("bases") or {})
    floor = _as_date(state.get("floor")) or max(
        (d for row in rows for d in _dates(row).values()), default=date.min
    )

    today = now.astimezone(UTC).date()
    for base, symbol in sorted(pairs.items()):
        entry = seen.get(base)
        if entry is not None and entry.get("state") != PENDING:
            continue
        listed = _as_date((entry or {}).get("listed"))
        if listed is None:
            try:
                listed = source.first_day(symbol)
            except Exception as err:  # noqa: BLE001 — одна пара не роняет проход
                report.errors.append(f"первая свеча {symbol}: {err}")
                continue
            report.asked += 1
        if listed is None:
            seen[base] = {"state": PENDING, "listed": None}
            report.pending.append(base)
            continue
        if entry is None and listed <= floor:
            seen[base] = {"state": OLD, "listed": listed.isoformat()}
            report.rejected += 1
            continue
        onboard = perps.get(base)
        if onboard is not None and onboard <= listed + timedelta(days=ENTRY_DAYS):
            instrument = perp_symbol(base)
            ids = _append(rows, instrument, listed)
            seen[base] = {
                "state": ADDED,
                "listed": listed.isoformat(),
                "onboard": onboard.isoformat(),
                "at": now.isoformat(),
            }
            if ids:
                frames = sorted({row.timeframe or "1d" for row in rows if row.id in ids})
                report.added.append(
                    Added(base, instrument, listed, onboard, tuple(ids), tuple(frames))
                )
        elif (today - listed).days <= ENTRY_DAYS:
            seen[base] = {"state": PENDING, "listed": listed.isoformat()}
            report.pending.append(base)
        else:
            seen[base] = {"state": NO_PERP, "listed": listed.isoformat()}
            report.rejected += 1

    flag.value = {"floor": floor.isoformat(), "at": now.isoformat(), "bases": seen}
    flag.updated_by = "worker"
    session.flush()
    return report


def fill_data(added: Sequence[Added], *, root: str, now: datetime) -> list[str]:
    """Свечи и ставки фандинга нового инструмента — от дня листинга с запасом.

    Ночное обновление берёт окно в 14 дней; листинг, добавленный позже (первый прогон
    ленты, простой), без этой докачки остался бы в составе без начала ряда, а воскресный
    перемер подставил бы вместо ставок константу — для шорта это доход вместо расхода.
    """
    from lab.data import CandleStore
    from lab.data.backfill_cex import backfill_funding, backfill_venue
    from lab.data.funding import FundingStore

    errors: list[str] = []
    for a in added:
        days = (now.astimezone(UTC).date() - a.listed).days + MARGIN_DAYS
        for tf in a.timeframes:
            try:
                for res in backfill_venue(CandleStore(root), VENUE, [a.instrument], tf, days):
                    if res.error:
                        errors.append(f"свечи {a.instrument} {tf}: {res.error}")
            except Exception as err:  # noqa: BLE001 — один инструмент не роняет остальные
                errors.append(f"свечи {a.instrument} {tf}: {err}")
        try:
            for _, _, err in backfill_funding(FundingStore(root), VENUE, [a.instrument], days):
                if err:
                    errors.append(f"фандинг {a.instrument}: {err}")
        except Exception as err:  # noqa: BLE001
            errors.append(f"фандинг {a.instrument}: {err}")
    return errors


def listings_job(
    session_scope: Callable[[], Any],
    *,
    source: Any = None,
    root: str | None = None,
    alert: Callable[[str, dict[str, Any]], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
):
    """Задание `listings` (см. `config/schedule.yaml`)."""
    from lab.ops.scheduler import Job

    def run() -> ListingsReport:
        from lab.feeds.cex.listings import BinanceListings
        from lab.ops.measure import data_root

        now = (clock or (lambda: datetime.now(UTC)))()
        try:
            with session_scope() as session:
                report = sync_listings(session, source or BinanceListings(), now=now)
        except Exception as err:  # noqa: BLE001 — биржа недоступна: ничего не меняем
            log.warning("Листинги: %s", err)
            report = ListingsReport(errors=[f"списки биржи недоступны: {err}"])
        else:
            # Докачка — после фиксации состава: долгий сетевой вызов не держит транзакцию.
            report.errors += fill_data(report.added, root=root or data_root(), now=now)
        log.info("%s", report.text())
        if alert is not None and (report.added or report.errors):
            alert(
                "alert",
                {"service": LISTINGS_JOB, "detail": report.card(now), "at": now.isoformat()},
            )
        return report

    return Job(
        id=LISTINGS_JOB,
        func=run,
        description="Новые листинги Binance → состав шорта листингов (иначе он их не видит)",
    )


__all__ = [
    "LISTINGS_JOB",
    "SEEN_FLAG",
    "Added",
    "ListingsReport",
    "fill_data",
    "listing_records",
    "listings_job",
    "sync_listings",
]
