"""Реестр источников (решение §12, R25.1–R25.3, A01).

Одна и та же вещь для трёх потребителей:

- фиды и исполнители зовут `use(feed_id, n)` перед каждым сетевым вызовом
  (интерфейс `feeds.quota.FeedsRegistry` из тикета 04 — подставляется параметром `quota=`);
- веб читает `status()` / `budget()` (протокол `web.feeds_source.FeedsStatusSource`);
- утренний отчёт бота читает `status()` (протокол `bot.report.FeedsStatus`) — строки
  отвечают на оба набора имён (`id`/`feed_id`, `health_detail`/`detail`).

Квота — лимит на период (`config/feeds.yaml`), счётчик обнуляется на границе периода.
Исчерпание — не исключение наружу, а переход на запасной источник (`fallback`); если
запасного нет — `QuotaExceeded`. Отказ источника — деградация: `source()` помечает фид
`down`, берёт запасной, а без него поднимает `FeedUnavailable` (наследник
`ConnectionError`, который `core.measure.run` превращает в замер `incomplete`).
Процесс при этом жив: отказ одного источника не мешает остальным.

Счётчики переживают перезапуск, если дан `session_factory`: таблица `feed_usage`
(миграция 0011), каталог и здоровье — таблица `feeds` из схемы спецификации.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from lab.config import CONFIG_DIR, load_config
from lab.db.base import Base, utcnow
from lab.db.models import FeedRow
from lab.web.feeds_source import Budget, FeedStatus

log = logging.getLogger(__name__)

PERIODS = ("second", "minute", "hour", "day", "month")
DEFAULT_BUDGET_USD = Decimal(50)
HEALTH_JOB = "feeds_health"
HEALTH_CRON = "*/10 * * * *"


# -- конфиг ------------------------------------------------------------------------------


class FeedSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    kind: str = ""
    quota_limit: int | None = None
    quota_period: str = "minute"
    cost_month: Decimal = Decimal(0)  # подписка, USD/мес
    cost_per_call: Decimal = Decimal(0)  # pay-per-use, USD за вызов
    priority: int = 100
    fallback: str | None = None
    note: str = ""


class FeedsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    budget_month_usd: Decimal = DEFAULT_BUDGET_USD
    warn_pct: int = 80
    feeds: dict[str, FeedSpec] = Field(default_factory=dict)


def load_feeds(path: Path | None = None) -> FeedsConfig:
    return load_config(path or CONFIG_DIR / "feeds.yaml", FeedsConfig)


# -- ошибки ------------------------------------------------------------------------------


class QuotaExceeded(RuntimeError):
    """Квота источника исчерпана, запасного нет."""

    def __init__(self, feed_id: str, limit: int, period: str) -> None:
        super().__init__(f"квота источника {feed_id} исчерпана ({limit}/{period}), запасного нет")
        self.feed_id = feed_id


class FeedUnavailable(ConnectionError):
    """Источник отказал и запасного нет. Наследник `ConnectionError`: `core.measure.run`
    ловит его и помечает замер `incomplete`, вместо того чтобы уронить процесс."""

    def __init__(self, feed_id: str, cause: BaseException | str) -> None:
        super().__init__(f"источник {feed_id} недоступен: {cause}")
        self.feed_id = feed_id


# -- строка состояния --------------------------------------------------------------------


class OpsFeedStatus(FeedStatus):
    """Строка `/feeds` (`web.feeds_source.FeedStatus`) + имена из `bot.report.FeedStatus`."""

    calls: int = 0
    priority: int = 100
    fallback_id: str | None = None

    @property
    def feed_id(self) -> str:
        return self.id

    @property
    def detail(self) -> str:
        return self.health_detail


# -- хранение ----------------------------------------------------------------------------


class FeedUsageRow(Base):
    """Счётчик квоты и трат по источнику (миграция 0011): переживает перезапуск."""

    __tablename__ = "feed_usage"

    feed_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[int] = mapped_column(Integer, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=Decimal(0))
    month: Mapped[str] = mapped_column(String(7), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


@dataclass
class _Usage:
    period_start: datetime | None = None
    used: int = 0
    calls: int = 0
    spent_usd: Decimal = Decimal(0)
    month: str = ""
    health: str = "ok"
    detail: str = ""
    checked_at: datetime | None = None
    dirty: bool = field(default=False, repr=False)


def period_start(now: datetime, period: str) -> datetime:
    now = now.astimezone(UTC)
    if period == "second":
        return now.replace(microsecond=0)
    if period == "minute":
        return now.replace(second=0, microsecond=0)
    if period == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"неизвестный период квоты: {period!r}")


def period_end(start: datetime, period: str) -> datetime:
    if period == "month":
        return (start + timedelta(days=32)).replace(day=1)
    step = {
        "second": timedelta(seconds=1),
        "minute": timedelta(minutes=1),
        "hour": timedelta(hours=1),
        "day": timedelta(days=1),
    }[period]
    return start + step


# -- реестр ------------------------------------------------------------------------------


class FeedsRegistry:
    def __init__(
        self,
        config: FeedsConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        session_factory: Callable[[], Any] | None = None,
        budget_limit_usd: Decimal | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config or load_feeds()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._sf = session_factory
        self._usage: dict[str, _Usage] = {}
        self._alerted: set[tuple[str, str]] = set()
        self.budget_limit_usd = budget_limit_usd or _budget_from_env(env, self.config)
        if self._sf is not None:
            self.load()

    # -- квота ---------------------------------------------------------------------------

    def spec(self, feed_id: str) -> FeedSpec:
        spec = self.config.feeds.get(feed_id)
        return spec if spec is not None else FeedSpec(name=feed_id)

    def _now(self) -> datetime:
        return self.clock()

    def _slot(self, feed_id: str) -> _Usage:
        spec = self.spec(feed_id)
        u = self._usage.setdefault(feed_id, _Usage())
        start = period_start(self._now(), spec.quota_period)
        if u.period_start != start:
            u.period_start, u.used, u.calls = start, 0, 0
        month = start.strftime("%Y-%m")
        if u.month != month:
            u.month, u.spent_usd = month, Decimal(0)
        return u

    def use(self, feed_id: str, n: int = 1) -> None:
        """Учесть сетевой вызов весом `n`. Квота исчерпана → `QuotaExceeded` (шов из тикета 04
        менять нельзя: фиды ждут именно `use`; выбор запасного делает `acquire`)."""
        spec = self.spec(feed_id)
        u = self._slot(feed_id)
        if spec.quota_limit is not None and u.used + n > spec.quota_limit:
            raise QuotaExceeded(feed_id, spec.quota_limit, spec.quota_period)
        u.used += n
        u.calls += 1
        u.spent_usd += spec.cost_per_call
        u.dirty = True

    def acquire(self, feed_id: str, n: int = 1, *, _seen: tuple[str, ...] = ()) -> str:
        """Учесть вызов, а при исчерпании квоты — уйти на запасной источник.
        Возвращает id источника, которым нужно ходить."""
        try:
            self.use(feed_id, n)
        except QuotaExceeded:
            fallback = self.spec(feed_id).fallback
            if fallback is None or fallback in _seen:
                raise
            log.warning("Квота %s исчерпана — беру запасной %s", feed_id, fallback)
            return self.acquire(fallback, n, _seen=(*_seen, feed_id))
        return feed_id

    def fallback_of(self, feed_id: str) -> str | None:
        return self.spec(feed_id).fallback

    # -- деградация ----------------------------------------------------------------------

    def mark(self, feed_id: str, health: str, detail: str = "") -> None:
        u = self._slot(feed_id)
        u.health, u.detail, u.checked_at, u.dirty = health, detail, self._now(), True
        if health != "ok":
            log.warning("Источник %s: %s — %s", feed_id, health, detail)

    def source(
        self,
        feed_id: str,
        fn: Callable[..., Any],
        *,
        weight: int = 1,
        fallback: Callable[..., Any] | None = None,
        default: Any = None,
        skip: bool = False,
    ) -> Callable[..., Any]:
        """Обернуть вызов источника: квота, здоровье, деградация.

        Отказ или исчерпанная квота → `fallback`, если он дан; иначе `default` при
        `skip=True` (пропуск тика), иначе `FeedUnavailable`. Процесс не падает.
        """

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                self.use(feed_id, weight)
            except QuotaExceeded as err:
                return self._degrade(feed_id, err, fallback, default, skip, args, kwargs)
            try:
                result = fn(*args, **kwargs)
            except Exception as err:  # отказ источника — не наша авария
                self.mark(feed_id, "down", str(err))
                return self._degrade(feed_id, err, fallback, default, skip, args, kwargs)
            self.mark(feed_id, "ok", "")
            return result

        return wrapped

    def _degrade(
        self,
        feed_id: str,
        err: BaseException,
        fallback: Callable[..., Any] | None,
        default: Any,
        skip: bool,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if fallback is not None:
            log.info("Источник %s отказал (%s) — беру запасной", feed_id, err)
            return fallback(*args, **kwargs)
        if skip:
            log.info("Источник %s отказал (%s) — пропускаю тик", feed_id, err)
            return default
        raise FeedUnavailable(feed_id, err) from err

    # -- здоровье ------------------------------------------------------------------------

    def health_check(self, feeds: Mapping[str, Any]) -> list[OpsFeedStatus]:
        """Задание `feeds_health`: опросить `health()` каждого источника."""
        for feed_id, feed in feeds.items():
            try:
                health = feed.health()
                self.mark(
                    feed_id,
                    str(getattr(health, "status", "ok")),
                    getattr(health, "detail", ""),
                )
            except Exception as err:
                self.mark(feed_id, "down", str(err))
        self.persist()
        return self.status()

    def health_job(self, feeds: Mapping[str, Any], *, cron: str = HEALTH_CRON):
        from lab.ops.scheduler import Job

        return Job(
            id=HEALTH_JOB,
            func=lambda: self.health_check(feeds),
            cron=cron,
            description="Health-check источников и снимок квот (R25.1)",
        )

    # -- отчёты --------------------------------------------------------------------------

    def forecast(self, feed_id: str) -> datetime | None:
        """Когда квота кончится при текущем темпе. None — не грозит до конца периода."""
        spec = self.spec(feed_id)
        u = self._slot(feed_id)
        if not spec.quota_limit or u.used <= 0 or u.period_start is None:
            return None
        now = self._now().astimezone(UTC)
        elapsed = (now - u.period_start).total_seconds()
        if elapsed <= 0:
            return None
        left = spec.quota_limit - u.used
        if left <= 0:
            return now
        seconds = left * elapsed / u.used
        at = now + timedelta(seconds=seconds)
        return at if at < period_end(u.period_start, spec.quota_period) else None

    def status(self) -> list[OpsFeedStatus]:
        rows: list[OpsFeedStatus] = []
        for feed_id, spec in sorted(
            self.config.feeds.items(), key=lambda kv: (kv[1].priority, kv[0])
        ):
            u = self._slot(feed_id)
            rows.append(
                OpsFeedStatus(
                    id=feed_id,
                    name=spec.name or feed_id,
                    kind=spec.kind,
                    health=u.health,  # type: ignore[arg-type]
                    health_detail=u.detail,
                    quota_used=u.used,
                    quota_limit=spec.quota_limit,
                    quota_period=spec.quota_period,
                    exhausted_at=self.forecast(feed_id),
                    cost_month=spec.cost_month + u.spent_usd,
                    calls=u.calls,
                    priority=spec.priority,
                    fallback_id=spec.fallback,
                )
            )
        self.persist()
        return rows

    def spend(self, feed_id: str, usd: Decimal) -> None:
        """Разовая трата платного сервиса (pay-per-use сверх подписки)."""
        u = self._slot(feed_id)
        u.spent_usd += Decimal(usd)
        u.dirty = True

    def budget(self) -> Budget:
        subs = sum((s.cost_month for s in self.config.feeds.values()), Decimal(0))
        payg = sum((self._slot(f).spent_usd for f in self.config.feeds), Decimal(0))
        now = self._now().astimezone(UTC)
        start = period_start(now, "month")
        days = (period_end(start, "month") - start).days
        elapsed_days = max((now - start).total_seconds() / 86400, 1 / 24)
        forecast = subs + payg * Decimal(str(round(min(days / elapsed_days, 100), 4)))
        return Budget(
            month_limit_usd=self.budget_limit_usd,
            spent_usd=subs + payg,
            forecast_usd=max(forecast, subs + payg),
        )

    def budget_alerts(self) -> list[tuple[str, str]]:
        """Новые предупреждения бюджетомера (A01): 80% и 100% от лимита, по разу за месяц."""
        b = self.budget()
        month = period_start(self._now(), "month").strftime("%Y-%m")
        limit = b.month_limit_usd or Decimal(1)
        pct = b.spent_usd * 100 / limit
        out: list[tuple[str, str]] = []
        for level, edge in (("warn", Decimal(self.config.warn_pct)), ("limit", Decimal(100))):
            if pct >= edge and (month, level) not in self._alerted:
                self._alerted.add((month, level))
                head = "Бюджет источников" if level == "warn" else "Бюджет источников исчерпан"
                out.append(
                    (
                        level,
                        f"{head}: потрачено {b.spent_usd:.2f} из {limit:.2f} USD"
                        f" ({int(pct)}% лимита), прогноз на месяц {b.forecast_usd:.2f}",
                    )
                )
        return out

    # -- база ----------------------------------------------------------------------------

    def load(self) -> None:
        if self._sf is None:
            return
        with self._sf() as session:
            for row in session.query(FeedUsageRow).all():
                self._usage[row.feed_id] = _Usage(
                    period_start=row.period_start,
                    used=row.used,
                    calls=row.calls,
                    spent_usd=row.spent_usd,
                    month=row.month,
                )
            for row in session.query(FeedRow).all():
                u = self._usage.setdefault(row.id, _Usage())
                u.health, u.detail, u.checked_at = row.health, row.health_detail, row.checked_at

    def persist(self) -> None:
        """Снимок счётчиков и здоровья в базу (каталог — таблица `feeds` из схемы)."""
        if self._sf is None:
            return
        with self._sf() as session:
            for feed_id, spec in self.config.feeds.items():
                u = self._usage.get(feed_id)
                if u is None:
                    continue
                row = session.get(FeedUsageRow, feed_id)
                if row is None:
                    row = FeedUsageRow(feed_id=feed_id)
                    session.add(row)
                row.period_start = u.period_start or self._now()
                row.used, row.calls = u.used, u.calls
                row.spent_usd, row.month = u.spent_usd, u.month
                feed = session.get(FeedRow, feed_id)
                if feed is None:
                    feed = FeedRow(id=feed_id, name=spec.name or feed_id, kind=spec.kind)
                    session.add(feed)
                feed.name, feed.kind = spec.name or feed_id, spec.kind
                feed.quota_limit, feed.quota_period = spec.quota_limit, spec.quota_period
                feed.quota_used, feed.priority = u.used, spec.priority
                feed.health, feed.health_detail = u.health, u.detail
                feed.checked_at = u.checked_at
                feed.cost_month = spec.cost_month + u.spent_usd
                u.dirty = False


def _budget_from_env(env: Mapping[str, str] | None, config: FeedsConfig) -> Decimal:
    import os

    raw = (env or os.environ).get("BUDGET_MONTH_USD") or ""
    try:
        return Decimal(raw) if raw.strip() else config.budget_month_usd
    except Exception:
        return config.budget_month_usd


__all__ = [
    "Budget",
    "FeedSpec",
    "FeedStatus",
    "FeedUnavailable",
    "FeedUsageRow",
    "FeedsConfig",
    "FeedsRegistry",
    "HEALTH_CRON",
    "HEALTH_JOB",
    "OpsFeedStatus",
    "QuotaExceeded",
    "load_feeds",
]
