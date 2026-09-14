"""Сборка сервиса `worker` (решение §11): планировщик, фиды, исполнение, сверка, watchdog.

Здесь сходятся швы, оставленные другими тасками:

- `ops.feeds_registry.FeedsRegistry` — квоты и здоровье источников (и он же кормит веб и бота);
- `ops.portfolio.LivePortfolio` — цифры для `core.risk.RiskEngine`;
- `ops.stop_watch.StopWatch` — `risk.check` + перевод стратегии в `degraded` при пробое стопа;
- `ops.measure.make_measure` — замер: `remeasure` (вс 22:00) и кнопка «В замер» в боте;
- `ops.jobs.jobs(...)` и `lab.discovery` — еженедельные задания;
- `ops.jobs.reconcile`, `ops.backup`, `ops.watchdog`, `ops.funding` — суточная эксплуатация;
- `bot.TraderBot.on_confirm` — подтверждение сигнала оператором превращается в ордер.

Ни один ордер не уходит на площадку без `Allow` от риск-ядра: путь один — `place_signal`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from lab.contracts import OrderIntent
from lab.core.risk import Allow, DbHaltSwitch, RiskEngine
from lab.ops.availability import (
    check_all,
    default_access_checks,
    default_probes,
    format_availability,
)
from lab.ops.backup import backup_job
from lab.ops.feeds_registry import FeedsRegistry
from lab.ops.funding import funding_job
from lab.ops.jobs.reconcile import reconcile_job
from lab.ops.measure import make_measure
from lab.ops.portfolio import LivePortfolio
from lab.ops.reload import ConfigReloader
from lab.ops.scheduler import Job, Scheduler, default_scheduler
from lab.ops.stop_watch import StopWatch
from lab.ops.watchdog import Watchdog, beat

log = logging.getLogger(__name__)

HEARTBEAT_S = 30
CEX_VENUES = ("bybit", "okx", "binance", "hyperliquid")


class Worker:
    """Всё, что делает `lab service worker`. Собирается один раз, живёт до остановки."""

    def __init__(
        self,
        session_scope: Callable[[], Any],
        *,
        env: Mapping[str, str] | None = None,
        bot: Any = None,
        scheduler: Scheduler | None = None,
        executors: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        store: Any = None,
    ) -> None:
        self.scope = session_scope
        self.env = dict(env or {})
        self.bot = bot
        self.clock = clock or (lambda: datetime.now(UTC))
        self.measure = make_measure(session_scope, store=store)
        self.scheduler = scheduler or default_scheduler()
        self.feeds = FeedsRegistry(session_factory=session_scope)
        self.executors = dict(executors or live_executors(quota=self.feeds))
        self.portfolio = LivePortfolio(session_scope, executors=self.executors, clock=self.clock)
        self.risk = RiskEngine(self._strategy_info, self.portfolio, halt=self._halt())
        self.watchdog = Watchdog(session_scope, alert=self.alert)
        self.reloader = ConfigReloader(risk=self.risk, session_factory=session_scope)
        self.stop_watch = StopWatch(self.risk, self._ladder_for, on_breach=self._on_breach)

    # -- зависимости ---------------------------------------------------------------------

    def _halt(self):
        with self.scope() as session:
            return DbHaltSwitch(session)

    def _strategy_info(self, strategy_id: str):
        from lab.core.registry import Registry
        from lab.core.risk import StrategyInfo

        with self.scope() as session:
            try:
                return StrategyInfo.from_strategy(Registry(session).get(strategy_id))
            except Exception:  # noqa: BLE001 — неизвестная стратегия: риск-ядро откажет само
                return None

    def ladder(self, session: Any):
        from lab.core.ladder import Ladder, default_threshold_fn
        from lab.core.risk import DbHaltSwitch as Halt

        notify = self.bot.notify_transition if self.bot is not None else None
        return Ladder(session, threshold=default_threshold_fn(), halt=Halt(session), notify=notify)

    @property
    def _ladder_for(self):
        """`StopWatch` ждёт объект с `breach(...)` — даём ему лестницу на свежей сессии."""
        worker = self

        class _LadderProxy:
            def breach(self, strategy_id: str, reason: str, snapshot=None):
                with worker.scope() as session:
                    return worker.ladder(session).breach(strategy_id, reason, snapshot)

        return _LadderProxy()

    def _on_breach(self, strategy_id: str, transition: Any) -> None:
        if self.bot is not None and transition is not None:
            self.bot.notify_transition(transition)

    def alert(self, kind: str, payload: dict[str, Any]) -> None:
        """Карточка оператору; без бота — только в лог (worker не должен падать из-за телеги)."""
        if self.bot is None:
            log.error("Карточка %s не отправлена (бот не поднят): %s", kind, payload)
            return
        try:
            self.bot.send_card_sync(kind, payload)
        except Exception as err:  # noqa: BLE001
            log.error("Карточка %s не отправлена: %s", kind, err)

    # -- старт ---------------------------------------------------------------------------

    def check_availability(self) -> str:
        """Доступность площадок с текущего IP (R33i) — в баннер, `/status` и `/feeds`."""
        with self.scope() as session:
            rows = check_all(
                default_probes(quota=self.feeds),
                session=session,
                access_checks=default_access_checks(),
            )
        for row in rows:
            self.feeds.mark(
                row.venue, "ok" if row.available else "down", row.detail
            ) if row.venue in self.feeds.config.feeds else None
        return format_availability(rows)

    def restore_orders(self) -> None:
        """R32i.1: при старте активные live-ордера сверяются с площадкой."""
        for venue, executor in self.executors.items():
            restore = getattr(executor, "restore", None)
            if restore is None:
                continue
            try:
                with self.scope() as session:
                    restored = restore(session)
                log.info("Площадка %s: восстановлено ордеров: %s", venue, len(restored or []))
            except Exception as err:  # noqa: BLE001
                log.warning("Площадка %s: восстановление ордеров не удалось: %s", venue, err)

    # -- исполнение ----------------------------------------------------------------------

    def place_signal(self, signal_id: str, *, mode: str = "live") -> Any:
        """`on_confirm` из бота: сигнал → риск-ядро → ордер → карточка `fill`."""
        from lab.core.journal import Journal
        from lab.db.models import SignalRow, StrategyRow
        from lab.executors.cex import client_order_id

        with self.scope() as session:
            signal = session.get(SignalRow, signal_id)
            if signal is None:
                log.error("Сигнал %s не найден", signal_id)
                return None
            strategy = session.get(StrategyRow, signal.strategy_id)
            venue = strategy.venue if strategy is not None else ""
            intent = OrderIntent(
                strategy_id=signal.strategy_id,
                venue=venue,
                instrument=signal.instrument,
                side=signal.side,
                qty=signal.size,
                price=signal.price_ref,
                order_type="market",
                mode=mode,
                signal_id=signal_id,
                client_order_id=client_order_id(signal_id, venue),
            )
            verdict = self.stop_watch.guard(intent)
            if not isinstance(verdict, Allow):
                log.warning("Ордер по сигналу %s отклонён: %s", signal_id, verdict.reason)
                self.alert(
                    "alert",
                    {
                        "service": "risk",
                        "detail": f"Ордер по сигналу {signal_id} отклонён: {verdict.reason}",
                        "at": self.clock().isoformat(),
                    },
                )
                return None
            executor = self.executors.get(venue)
            if executor is None:
                log.error("Нет исполнителя для площадки %s", venue)
                return None
            order = executor.place(intent, mode)
            journal = Journal(session)
            journal.record_order(intent, order_id=order.id, state=order.state)
            fills = list(executor.fills(self.clock() - timedelta(minutes=5)))
            for fill in fills:
                if fill.order_id != order.id:
                    continue
                journal.record_fill(fill, ref_price=getattr(fill, "ref_price", None))
                self.alert(
                    "fill",
                    {
                        "strategy_id": intent.strategy_id,
                        "instrument": intent.instrument,
                        "side": intent.side,
                        "qty": str(fill.qty),
                        "price": str(fill.price),
                        "venue": venue,
                        "costs": str(fill.fee),
                        "signal_id": signal_id,
                    },
                )
            return order

    # -- расписание ----------------------------------------------------------------------

    def jobs(self) -> list[Job]:
        from lab.ops.jobs import SeedReminder
        from lab.ops.jobs import jobs as discovery_jobs
        from lab.ops.jobs.cryptoquant import cryptoquant_job

        out: list[Job] = []
        reminder = SeedReminder(self.scope, bot=self.bot) if self.bot is not None else None
        out.extend(
            discovery_jobs(
                self.scope,
                bot=self.bot,
                measure=self.measure,
                ladder_factory=self.ladder,
                risk=self.risk,
                reminder=reminder,
            )
        )
        out.append(reconcile_job(self.scope, executors=self.executors, alert=self.alert))
        out.append(backup_job(alert=self.alert))
        out.append(self.watchdog.job())
        out.append(self.watchdog.heartbeat_job("worker"))
        out.append(self.feeds.health_job(default_probes(quota=self.feeds)))
        out.append(funding_job(self.scope, executors=self.executors))
        out.append(cryptoquant_job(alert=self.alert))
        out.append(self.portfolio.allocation_job(risk=self.risk))
        out.append(
            Job(
                id="stop_watch",
                func=lambda: self.stop_watch.sweep([]),
                description="Пробой стопа стратегии → degraded",
            )
        )
        return out

    def register_all(self) -> list[str]:
        for job in self.jobs():
            try:
                self.scheduler.register(job)
            except KeyError as err:  # задания нет в schedule.yaml и без cron
                log.warning("Задание не поставлено: %s", err)
        return self.scheduler.jobs()

    def beat(self) -> None:
        with self.scope() as session:
            beat(session, "worker", now=self.clock())

    def run(self, *, once: bool = False, sleep: float = HEARTBEAT_S) -> int:
        print(self.check_availability())
        self.restore_orders()
        self.reloader.reload(by="worker")
        self.reloader.install_sighup(by="sighup")
        jobs = self.register_all()
        self.beat()
        print(f"Сервис worker: заданий {len(jobs)} ({', '.join(jobs)})")
        for level, text in self.feeds.budget_alerts():
            self.alert(
                "alert",
                {"service": "budget", "detail": text, "at": self.clock().isoformat()},
            )
            log.warning("%s: %s", level, text)
        if once:
            return 0
        self.scheduler.start()
        try:
            while True:
                self.beat()
                time.sleep(sleep)
        except KeyboardInterrupt:
            return 0
        finally:
            self.scheduler.shutdown()


def live_executors(*, quota: Any = None, venues: tuple[str, ...] = CEX_VENUES) -> dict[str, Any]:
    """Исполнители площадок в режиме `live`; что не собралось (нет ключей) — пропускаем."""
    from lab.executors.cex import make_executor

    out: dict[str, Any] = {}
    for venue in venues:
        try:
            out[venue] = make_executor(venue, mode="live", quota=quota)
        except Exception as err:  # noqa: BLE001
            log.info("Исполнитель %s не собран: %s", venue, err)
    for name, factory in (("polymarket", _pm_executor), ("robinhood", _rh_executor)):
        try:
            out[name] = factory(quota)
        except Exception as err:  # noqa: BLE001
            log.info("Исполнитель %s не собран: %s", name, err)
    return out


def _pm_executor(quota: Any):
    from lab.executors.polymarket import make_executor

    return make_executor(mode="live", quota=quota)


def _rh_executor(quota: Any):
    from lab.executors.robinhood import RobinhoodExecutor

    return RobinhoodExecutor(mode="live", quota=quota)


__all__ = ["CEX_VENUES", "HEARTBEAT_S", "Worker", "live_executors"]
