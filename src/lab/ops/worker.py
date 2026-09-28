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
from decimal import Decimal
from typing import Any

from lab.contracts import Costs, OrderIntent
from lab.core.costs import CostModel, default_model, fee_in_quote, is_spot
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
# Спотовая продажа больше лотов стратегии на эту долю и меньше — комиссия в монете или
# округление объёма биржей (у бирж лаборатории комиссия до 0.1%, у Bitstamp в модели 0.4%).
# Больше — стратегия и журнал разошлись, это тревога оператору.
SPOT_SELL_SLACK = Decimal("0.01")


def _closes(side: str) -> str:
    """Какие лоты журнала закрывает ордер этой стороны: покупка — шорт, продажа — лонг."""
    return "short" if side == "buy" else "long"


def _legs(
    size: Decimal, held: Decimal, signal_id: str, *, open_rest: bool = True
) -> list[tuple[Decimal, bool, str]]:
    """Сигнал → ордера `(объём, reduce_only, ключ client id)`: закрытие не больше позиции
    стратегии, остаток — открывающий. Первый ордер берёт client id от самого сигнала, как
    до разбивки: повтор после обрыва найдёт его на площадке и не задвоит.
    `open_rest=False` — остаток не отправляется: так продажа на споте не открывает шорт."""
    close = min(size, held) if held > 0 else Decimal(0)
    legs: list[tuple[Decimal, bool, str]] = []
    if close > 0:
        legs.append((close, True, signal_id))
    if size > close and open_rest:
        legs.append((size - close, False, f"{signal_id}:open" if legs else signal_id))
    return legs


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

    def stop_probes(self) -> list[OrderIntent]:
        """Пробы для задания `stop_watch`: по одной на стратегию денежной ступени, которая
        ещё торгует. Без них стоп замечался только на следующем ордере стратегии — а правило,
        держащее позицию неделями, не увидело бы падения внутри неё вовсе (пробел 5,
        28.09.2026). Проба до площадки не доходит: `StopWatch.sweep` гонит её только через
        `risk.check`, где стоп-правила стоят раньше цены и размера."""
        from sqlalchemy import select

        from lab.contracts import RUNG_ORDER, Rung, Status
        from lab.db.models import StrategyRow

        live = [r.value for r in RUNG_ORDER[RUNG_ORDER.index(Rung.MICRO) :]]
        idle = (Status.CANDIDATE.value, Status.DEGRADED.value, Status.RETIRED.value)
        with self.scope() as session:
            rows = session.scalars(
                select(StrategyRow).where(
                    StrategyRow.rung.in_(live), StrategyRow.status.not_in(idle)
                )
            ).all()
        stamp = self.clock().strftime("%Y%m%dT%H%M%S")
        return [
            OrderIntent(
                strategy_id=row.id,
                venue=row.venue,
                instrument=(row.instruments or ["?"])[0],
                side="buy",
                qty=Decimal("0.00000001"),
                order_type="market",
                mode="live",
                signal_id=f"stop-probe:{row.id}",
                client_order_id=f"stop-probe:{row.id}:{stamp}",
            )
            for row in rows
        ]

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

    def place_signal(self, signal_id: str, *, mode: str = "live") -> list[Any]:
        """`on_confirm` из бота: сигнал → риск-ядро → ордера → карточка `fill`.

        Закрывающая часть сигнала помечается `reduce_only` по открытым лотам САМОЙ стратегии
        в журнале (того же режима), а не по слову стратегии: общего признака «закрыть»
        у стратегий нет, а ошибка в правиле не должна открывать позицию в обход лимитов.
        Закрытие риск-ядро пропускает всегда — и в `degraded`, и после пробоя стопа.
        Сигнал больше позиции — переворот, как в замере: закрывающий ордер, потом остаток
        отдельным открывающим с полной проверкой; его отказ закрытие не отменяет.
        **На споте продажа шорт не открывает** — продаётся не больше лотов стратегии: лот
        журнала за вычетом комиссии в монете меньше того, что стратегия считает купленным
        (`core.costs.base_moved`). Остаток больше `SPOT_SELL_SLACK` — тревога оператору,
        продаётся всё равно то, что есть (решение владельца 28.09.2026).
        Возвращает отправленные ордера (пусто — ничего не ушло).
        """
        from lab.core.journal import Journal
        from lab.db.models import SignalRow, StrategyRow
        from lab.executors.cex import client_order_id

        placed: list[Any] = []
        with self.scope() as session:
            signal = session.get(SignalRow, signal_id)
            if signal is None:
                log.error("Сигнал %s не найден", signal_id)
                return placed
            strategy = session.get(StrategyRow, signal.strategy_id)
            venue = strategy.venue if strategy is not None else ""
            journal = Journal(session)
            held = journal.open_qty(
                signal.strategy_id, signal.instrument, _closes(signal.side), mode=mode
            )
            spot_sell = signal.side == "sell" and is_spot(signal.instrument)
            legs = _legs(signal.size, held, signal_id, open_rest=not spot_sell)
            if spot_sell and signal.size > held:
                self._spot_shortfall(signal, held)
            elif not legs:
                log.error("Сигнал %s: объём %s — ордер не собрать", signal_id, signal.size)
            for qty, reduce_only, key in legs:
                intent = OrderIntent(
                    strategy_id=signal.strategy_id,
                    venue=venue,
                    instrument=signal.instrument,
                    side=signal.side,
                    qty=qty,
                    price=signal.price_ref,
                    order_type="market",
                    reduce_only=reduce_only,
                    mode=mode,
                    signal_id=signal_id,
                    client_order_id=client_order_id(key, venue),
                )
                order = self._place_leg(journal, intent, closed_before=bool(placed))
                if order is None:
                    break
                placed.append(order)
        return placed

    def _journal_alert(self, detail: str) -> None:
        log.warning(detail)
        at = self.clock().isoformat()
        self.alert("alert", {"service": "journal", "detail": detail, "at": at})

    def _spot_shortfall(self, signal: Any, held: Decimal) -> None:
        """Спотовая продажа больше лотов стратегии: остаток не отправлен. В пределах
        `SPOT_SELL_SLACK` — комиссия в монете или округление биржи, строка в лог; больше —
        стратегия и журнал разошлись (потерян филл, ошибка правила): тревога оператору."""
        rest = signal.size - held
        line = (
            f"Сигнал {signal.id}: продажа {signal.size} {signal.instrument}, у стратегии "
            f"в журнале {held} — продаю {held}, остаток {rest} не отправлен (на споте "
            "продажа шорт не открывает)"
        )
        if rest <= signal.size * SPOT_SELL_SLACK:
            log.info("%s — комиссия в монете или округление биржи", line)
            return
        self._journal_alert(f"{line}. Стратегия и журнал разошлись: сверь журнал с балансом биржи")

    def _fill_costs(self, executor: Any, intent: OrderIntent, fill: Any) -> Costs:
        """Издержки филла в валюте котировки (`core.costs.fee_in_quote`). Комиссия в третьей
        монете (BNB со скидкой Binance) — оценка по тарифу площадки, тейкер без скидки: цены
        монеты на момент филла нет, а скидка комиссию только уменьшает — ошибка идёт против
        стратегии, а не в её пользу. Филл записывается всегда: без него лот потерян."""
        fee = fee_in_quote(fill, intent.instrument)
        if fee is not None:
            return Costs(fee=fee)
        model = getattr(executor, "costs", None)
        model = model if isinstance(model, CostModel) else default_model()
        priced = intent.model_copy(update={"qty": fill.qty, "price": fill.price})
        try:
            fee = model.estimate(intent.venue, priced, perp=not is_spot(intent.instrument)).fee
            booked = f"в журнале оценка по тарифу {fee}"
        except (KeyError, ValueError) as err:  # тарифа площадки нет — сказать, а не молчать
            fee = Decimal(0)
            booked = f"оценки по тарифу нет ({err}), в журнале издержки 0"
        detail = (
            f"Филл {fill.id} ({intent.instrument}): комиссия {fill.fee} {fill.fee_asset} — "
            f"не котировка и не база, {booked}. Выключи на бирже оплату комиссий "
            f"в {fill.fee_asset}"
        )
        self._journal_alert(detail)
        return Costs(fee=fee)

    def _place_leg(self, journal: Any, intent: OrderIntent, *, closed_before: bool) -> Any:
        """Один ордер сигнала: риск-ядро → площадка → журнал → карточка `fill`."""
        signal_id, venue = intent.signal_id, intent.venue
        verdict = self.stop_watch.guard(intent)
        if not isinstance(verdict, Allow):
            detail = f"Ордер по сигналу {signal_id} отклонён: {verdict.reason}"
            if closed_before:
                detail += " — закрывающий ордер отправлен, отклонён только остаток переворота"
            log.warning(detail)
            self.alert(
                "alert", {"service": "risk", "detail": detail, "at": self.clock().isoformat()}
            )
            return None
        executor = self.executors.get(venue)
        if executor is None:
            log.error("Нет исполнителя для площадки %s", venue)
            return None
        order = executor.place(intent, intent.mode)
        journal.record_order(intent, order_id=order.id, state=order.state)
        fills = list(executor.fills(self.clock() - timedelta(minutes=5)))
        for fill in fills:
            if fill.order_id != order.id:
                continue
            costs = self._fill_costs(executor, intent, fill)
            journal.record_fill(fill, costs=costs, ref_price=getattr(fill, "ref_price", None))
            self.alert(
                "fill",
                {
                    "strategy_id": intent.strategy_id,
                    "instrument": intent.instrument,
                    "side": intent.side,
                    "qty": str(fill.qty),
                    "price": str(fill.price),
                    "venue": venue,
                    "costs": str(costs.fee),  # в котировке; сырая комиссия — в журнале филлов
                    "signal_id": signal_id,
                },
            )
        return order

    # -- расписание ----------------------------------------------------------------------

    def jobs(self) -> list[Job]:
        from lab.ops.jobs import SeedReminder
        from lab.ops.jobs import jobs as discovery_jobs
        from lab.ops.jobs.coinalyze import coinalyze_job
        from lab.ops.jobs.cryptoquant import cryptoquant_job
        from lab.ops.jobs.data_refresh import data_refresh_job
        from lab.ops.jobs.forward import forward_job
        from lab.ops.jobs.listings import listings_job

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
        out.append(coinalyze_job(alert=self.alert))
        # Порядок в списке роли не играет (время задаёт cron), но смысловая связь
        # такая: сперва состав (новые листинги), потом свежие свечи, потом прогон
        # стратегий по ним.
        out.append(listings_job(self.scope, alert=self.alert))
        out.append(data_refresh_job(self.scope, alert=self.alert))
        out.append(forward_job(self.scope, alert=self.alert))
        out.append(self.portfolio.allocation_job(risk=self.risk))
        out.append(
            Job(
                id="stop_watch",
                func=lambda: self.stop_watch.sweep(self.stop_probes()),
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
