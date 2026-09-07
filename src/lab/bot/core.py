"""Логика бота Trader без aiogram: команды, кнопки, карточки, утренний отчёт.

Всё, что приходит не от `admin_id`, молча игнорируется (R13.6). Необратимые команды
(`/halt`, `/resume`, `/retire`) — двойное подтверждение: первое нажатие даёт карточку
`confirm` с токеном, второе (в течение `confirm_ttl`) выполняет. Кнопки сигналов пишут
исход в `signals.outcome` через `core.journal.Journal.set_outcome`. Все исходящие идут
через `ops.outbox` — недоставленное повторяется.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from lab.bot.cards import CARD_KINDS, Card, render_card, strip_buttons
from lab.bot.report import FeedsStatus, build_morning_report, pending_signal_cards
from lab.config import load_limits
from lab.contracts import Rung, SignalOutcome
from lab.core.journal import Journal
from lab.core.ladder import Ladder, LadderError, Transition
from lab.core.registry import Registry, StrategyNotFound
from lab.executors.access import status_lines as branch_status_lines
from lab.ops.outbox import Button, Outbox, OutboxMessage, Transport
from lab.ops.scheduler import Job, Scheduler

log = logging.getLogger(__name__)

HELP = (
    "<b>Команды Trader</b>\n"
    "/status — состояние: стоп всё, стратегии по ступеням, ожидающие решения\n"
    "/halt — остановить всё (подтверждение)\n"
    "/resume — снять остановку (подтверждение)\n"
    "/promote &lt;id&gt; — поднять semi → auto\n"
    "/retire &lt;id&gt; &lt;причина&gt; — в архив (подтверждение)\n"
    "/limits — лимиты веток из config/limits.yaml\n"
    "/queue — очередь кандидатов\n"
    "/report — утренний отчёт сейчас"
)

DecisionHook = Callable[[str, str], Awaitable[None] | None]
ConfirmHook = Callable[[str], Awaitable[None] | None]


class Reply(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    buttons: list[list[Button]] = Field(default_factory=list)


class CallbackReply(BaseModel):
    """`answer` — всплывашка; `text`/`buttons` — чем заменить сообщение под кнопкой
    (None — не трогать)."""

    model_config = ConfigDict(frozen=True)

    answer: str
    text: str | None = None
    buttons: list[list[Button]] = Field(default_factory=list)


@dataclass
class _PendingAction:
    action: str
    run: Callable[[Session], str]
    expires_at: datetime
    args: dict[str, Any] = field(default_factory=dict)


async def _maybe_await(value: Awaitable[None] | None) -> None:
    if value is not None:
        await value


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TraderBot:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        admin_id: int,
        transport: Transport,
        ladder_factory: Callable[[Session], Ladder],
        chat_id: str | None = None,
        feeds_status: FeedsStatus | None = None,
        on_confirm: ConfirmHook | None = None,
        on_candidate: DecisionHook | None = None,
        on_rebalance: DecisionHook | None = None,
        on_allowlist: DecisionHook | None = None,
        clock: Callable[[], datetime] = _utcnow,
        confirm_ttl: timedelta = timedelta(seconds=60),
        close_sessions: bool = True,
        outbox_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.admin_id = int(admin_id)
        self.chat_id = str(chat_id or admin_id)
        self.transport = transport
        self._sessions = session_factory
        self._close = close_sessions
        self._ladder_factory = ladder_factory
        self._feeds = feeds_status
        self._on_confirm = on_confirm
        self._hooks: dict[str, DecisionHook | None] = {
            "cand": on_candidate,
            "rebal": on_rebalance,
            "allow": on_allowlist,
        }
        self.now = clock
        self.confirm_ttl = confirm_ttl
        self._outbox_options = dict(outbox_options or {})
        self._pending: dict[str, _PendingAction] = {}
        self._cards: dict[str, Card] = {}

    # -- инфраструктура ----------------------------------------------------------------

    @contextmanager
    def _session(self) -> Iterator[Session]:
        s = self._sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            if self._close:
                s.close()

    def _outbox(self, s: Session) -> Outbox:
        return Outbox(s, self.transport, **self._outbox_options)

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) == self.admin_id

    # -- карточки (send_card) ----------------------------------------------------------

    async def send_card(self, kind: str, payload: Mapping[str, Any]) -> int:
        """Поставить карточку в outbox и попытаться доставить сразу. Возвращает id в outbox."""
        card = render_card(kind, payload)
        if card.ref:
            self._cards[card.ref] = card
        with self._session() as s:
            box = self._outbox(s)
            row_id = box.enqueue(self._message(card), now=self.now())
            await box.flush(now=self.now())
        return row_id

    def _message(self, card: Card) -> OutboxMessage:
        return OutboxMessage(
            chat_id=self.chat_id, kind=card.kind, text=card.text, buttons=card.buttons, ref=card.ref
        )

    async def _replace_card(self, s: Session, ref: str, suffix: str) -> Card | None:
        """Снять кнопки с карточки по `ref` и приписать итог; вернуть новую карточку."""
        card = self._cards.get(ref)
        if card is None:  # карточку слал другой процесс или было перезапуск — берём из outbox
            stored = self._outbox(s).get(ref)
            if stored is None:
                return None
            card = Card(kind=stored.kind, text=stored.text, buttons=stored.buttons, ref=ref)
        new = strip_buttons(card, suffix)
        self._cards[ref] = new
        try:
            await self._outbox(s).edit(ref=ref, msg=self._message(new))
        except KeyError:
            log.info("карточка %s не в outbox — редактировать нечего", ref)
        except Exception as err:  # noqa: BLE001 — Telegram недоступен: исход уже записан
            log.warning("не удалось отредактировать %s: %s", ref, err)
        return new

    async def flush(self, now: datetime | None = None) -> None:
        with self._session() as s:
            await self._outbox(s).flush(now=now or self.now())

    def send_card_sync(self, kind: str, payload: Mapping[str, Any]) -> int:
        """Для синхронных мест (например `Ladder(notify=...)`): только поставить в outbox."""
        card = render_card(kind, payload)
        if card.ref:
            self._cards[card.ref] = card
        with self._session() as s:
            return self._outbox(s).enqueue(self._message(card), now=self.now())

    def notify_transition(self, t: Transition) -> int:
        """Для `Ladder(notify=bot.notify_transition)`: карточка `transition` в outbox (R02.4)."""
        return self.send_card_sync("transition", t.model_dump(mode="json"))

    async def alert_service_down(self, service: str, *, silent_for_s: int, detail: str = "") -> int:
        """Интерфейс для watchdog (R32i.2, тикет 14): сервис молчит `silent_for_s` секунд."""
        return await self.send_card(
            "alert",
            {"service": service, "silent_for_s": silent_for_s, "detail": detail, "at": self.now()},
        )

    # -- задания планировщика -----------------------------------------------------------

    def jobs(
        self,
        scheduler: Scheduler,
        *,
        run: Callable[[Coroutine[Any, Any, Any]], Any] = asyncio.run,
    ) -> list[Job]:
        """Зарегистрировать задания бота: утренний отчёт (09:00 Самара по `schedule.yaml`),
        протухание сигналов и повтор outbox — минутный тик. `run` — как исполнять корутину
        из потока APScheduler (в сервисе бота — `run_coroutine_threadsafe` в цикл aiogram)."""
        jobs = [
            Job("morning_report", lambda: run(self.send_morning_report())),
            Job("expire_signals", lambda: run(self.expire_pending())),
            Job("outbox_flush", lambda: run(self.flush())),
        ]
        return [scheduler.register(j) for j in jobs]

    # -- команды -----------------------------------------------------------------------

    async def handle_command(self, user_id: int | None, text: str) -> Reply | None:
        if not self.is_admin(user_id):
            log.debug("команда от чужого id проигнорирована")
            return None
        parts = (text or "").strip().split()
        if not parts or not parts[0].startswith("/"):
            return Reply(text=HELP)
        cmd, args = parts[0].lstrip("/").split("@")[0].lower(), parts[1:]
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            return Reply(text=f"Не знаю команду /{cmd}.\n\n{HELP}")
        try:
            return await handler(args)
        except (LadderError, StrategyNotFound) as err:
            return Reply(text=f"Отказ: {err}")

    async def cmd_start(self, args: list[str]) -> Reply:
        return Reply(text=HELP)

    cmd_help = cmd_start

    async def cmd_status(self, args: list[str]) -> Reply:
        with self._session() as s:
            ladder = self._ladder_factory(s)
            rows = Registry(s).list()
            pending = pending_signal_cards(s)
            queued = len(self._outbox(s).pending(now=self.now()))
            # ветки, где торговля закрыта для нашего IP/аккаунта (таск 11: Polymarket, Robinhood)
            closed_branches = branch_status_lines(s)
        self._arm_seed_reminder()  # таск 14 (G10): напоминание «дополни список» взводится один раз
        halted = "ДА" if ladder.halted else "нет"
        by_rung: dict[str, list[str]] = {}
        for r in rows:
            by_rung.setdefault(f"{r.rung}", []).append(f"{r.id} [{r.status}]")
        lines = [f"<b>Состояние</b> · стоп всё: {halted}", f"стратегий: {len(rows)}"]
        for rung in Rung:
            ids = by_rung.get(rung.value)
            if ids:
                lines.append(f"• {rung.value}: " + ", ".join(f"<code>{i}</code>" for i in ids))
        lines.extend(closed_branches)
        lines.append(f"ждут решения: {len(pending)} · в очереди на отправку: {queued}")
        return Reply(text="\n".join(lines))

    def _arm_seed_reminder(self) -> None:
        """G10: первый заход в `/status` взводит одноразовое напоминание о списке кандидатов."""
        try:
            from lab.ops.jobs import SeedReminder

            SeedReminder(self._session, bot=self).arm()
        except Exception:  # noqa: BLE001 — напоминание не должно ломать команду
            log.debug("SeedReminder не взведён", exc_info=True)

    async def cmd_halt(self, args: list[str]) -> Reply:
        def run(s: Session) -> str:
            self._ladder_factory(s).halt_all(by="operator")
            return "Остановлено всё: ни один ордер не пройдёт до /resume."

        return self._ask_confirm("halt", "Остановить всё? Ордера перестанут проходить.", run)

    async def cmd_resume(self, args: list[str]) -> Reply:
        def run(s: Session) -> str:
            self._ladder_factory(s).resume_all(by="operator")
            return "Остановка снята: торговля возобновлена."

        return self._ask_confirm("resume", "Снять «стоп всё»? Торговля возобновится.", run)

    async def cmd_retire(self, args: list[str]) -> Reply:
        if len(args) < 2:
            return Reply(text="Использование: /retire <id> <причина>")
        sid, reason = args[0], " ".join(args[1:])
        with self._session() as s:
            Registry(s).get(sid)  # StrategyNotFound → «Отказ: …»

        def run(s: Session) -> str:
            r = Registry(s).retire(sid, reason=reason)
            return f"Стратегия <code>{r.id}</code> отправлена в архив: {reason}"

        return self._ask_confirm("retire", f"В архив {sid}? Причина: {reason}", run)

    async def cmd_promote(self, args: list[str]) -> Reply:
        if len(args) != 1:
            return Reply(text="Использование: /promote <id> — только semi → auto")
        sid = args[0]
        with self._session() as s:
            strategy = Registry(s).get(sid)
            if strategy.rung != Rung.SEMI:
                return Reply(
                    text=(
                        f"Отказ: /promote поднимает только semi → auto, а <code>{sid}</code> "
                        f"на ступени {strategy.rung}. Выше по порогу её поднимет планировщик."
                    )
                )
            t = self._ladder_factory(s).promote(sid, by="operator", reason="команда /promote")
        return Reply(text=f"<code>{sid}</code>: {t.from_rung} → {t.to_rung} (оператор).")

    async def cmd_limits(self, args: list[str]) -> Reply:
        limits = load_limits()
        lines = [f"<b>Лимиты</b> · потолок реального капитала: {limits.real_capital_cap_usd} USD"]
        for name, g in limits.groups.items():
            stop = g.stop.model_dump(mode="json")
            lines.append(
                f"• {name} ({', '.join(str(b) for b in g.branches)}): доля {g.share_pct}%, "
                f"сделка ≤ {g.max_trade_pct}% от {g.max_trade_base}, плечо ≤ {g.max_leverage}, "
                f"стоп {stop}"
            )
        return Reply(text="\n".join(lines))

    async def cmd_queue(self, args: list[str]) -> Reply:
        with self._session() as s:
            cands = Registry(s).candidates(decision="pending")
        if not cands:
            return Reply(
                text=(
                    "Очередь кандидатов пуста: добавь кандидата через CLI "
                    "или дождись поиска (пн 06:00)."
                )
            )
        lines = ["<b>Очередь кандидатов</b>"] + [
            f"• #{c.id} {c.kind}: {c.ref} · с {c.discovered_at:%d.%m}" for c in cands
        ]
        return Reply(text="\n".join(lines))

    async def cmd_report(self, args: list[str]) -> Reply:
        return Reply(text=self.morning_report())

    # -- подтверждение вторым нажатием ---------------------------------------------------

    def _ask_confirm(self, action: str, question: str, run: Callable[[Session], str]) -> Reply:
        token = secrets.token_urlsafe(8)
        self._pending[token] = _PendingAction(action, run, self.now() + self.confirm_ttl)
        card = render_card("confirm", {"token": token, "action": action, "text": question})
        return Reply(text=card.text, buttons=card.buttons)

    def _take_pending(self, token: str) -> _PendingAction | str:
        act = self._pending.pop(token, None)
        if act is None:
            return "Запрос не найден или уже выполнен."
        if self.now() > act.expires_at:
            return "Запрос устарел — повтори команду."
        return act

    # -- кнопки ------------------------------------------------------------------------

    async def handle_callback(self, user_id: int | None, data: str) -> CallbackReply | None:
        if not self.is_admin(user_id):
            return None
        parts = (data or "").split(":")
        kind = parts[0]
        if kind in ("confirm", "cancel") and len(parts) == 2:
            return await self._cb_confirm(kind, parts[1])
        if kind == "sig" and len(parts) == 3:
            return await self._cb_signal(parts[1], parts[2])
        if kind in self._hooks and len(parts) == 3:
            return await self._cb_decision(kind, parts[1], parts[2])
        return CallbackReply(answer="Неизвестная кнопка.")

    async def _cb_confirm(self, kind: str, token: str) -> CallbackReply:
        got = self._take_pending(token)
        if isinstance(got, str):
            return CallbackReply(answer=got, text=got)
        if kind == "cancel":
            return CallbackReply(answer="Отменено.", text=f"Отменено: {got.action}.")
        with self._session() as s:
            result = got.run(s)
        return CallbackReply(answer=result[:180], text=result)

    async def _cb_signal(self, signal_id: str, action: str) -> CallbackReply:
        outcome = {
            "executed": SignalOutcome.EXECUTED,
            "confirm": SignalOutcome.EXECUTED,
            "skipped": SignalOutcome.SKIPPED,
            "reject": SignalOutcome.SKIPPED,
        }.get(action)
        if outcome is None:
            return CallbackReply(answer="Неизвестное действие.")
        now = self.now()
        with self._session() as s:
            journal = Journal(s)
            rec = journal.signal(signal_id)
            if rec.outcome != SignalOutcome.PENDING:
                note = f"Сигнал уже закрыт: {rec.outcome}."
                return CallbackReply(answer=note)
            journal.set_outcome(signal_id, outcome, now)
            if action == "confirm" and self._on_confirm is not None:
                await _maybe_await(self._on_confirm(signal_id))
            suffix = {
                "executed": "✅ Исполнен оператором",
                "confirm": "✅ Подтверждён — передан на исполнение",
                "skipped": "⏭ Пропущен",
                "reject": "⏭ Отклонён",
            }[action]
            card = await self._replace_card(s, signal_id, f"{suffix} · {now:%H:%M} UTC")
        answer = suffix.lstrip("✅⏭ ").strip()
        return CallbackReply(answer=answer, text=card.text if card else None)

    async def _cb_decision(self, kind: str, ref_id: str, decision: str) -> CallbackReply:
        hook = self._hooks[kind]
        label = {"cand": "кандидат", "rebal": "перелив", "allow": "allowlist"}[kind]
        if hook is not None:
            await _maybe_await(hook(ref_id, decision))
            suffix = f"Решение записано: {decision}"
        else:
            suffix = f"Решение принято ({decision}), обработчик пока не подключён — н/д"
        with self._session() as s:
            card = await self._replace_card(s, f"{kind}:{ref_id}", suffix)
        return CallbackReply(
            answer=f"{label} #{ref_id}: {decision}", text=card.text if card else None
        )

    # -- таймауты сигналов (R03.1) -----------------------------------------------------

    async def expire_pending(self, now: datetime | None = None) -> list[str]:
        """Просроченные сигналы → `expired` (через лестницу) и правка карточек."""
        now = now or self.now()
        with self._session() as s:
            expired = self._ladder_factory(s).expire_signals(now)
            for sid in expired:
                await self._replace_card(s, sid, f"⏱ Просрочен: ответа не было · {now:%H:%M} UTC")
        return expired

    # -- утренний отчёт (R13) ----------------------------------------------------------

    def morning_report(self) -> str:
        with self._session() as s:
            return build_morning_report(
                s, ladder=self._ladder_factory(s), feeds=self._feeds, now=self.now()
            ).text

    async def send_morning_report(self) -> int:
        """Одно сообщение + карточки ожидающих решений (пересылаются заново)."""
        with self._session() as s:
            report = build_morning_report(
                s, ladder=self._ladder_factory(s), feeds=self._feeds, now=self.now()
            )
            box = self._outbox(s)
            row_id = box.enqueue(
                OutboxMessage(chat_id=self.chat_id, kind="report", text=report.text),
                now=self.now(),
            )
            for kind, payload in report.pending_cards:
                card = render_card(kind, payload)
                if card.ref:
                    self._cards[card.ref] = card
                box.enqueue(self._message(card), now=self.now())
            await box.flush(now=self.now())
        return row_id


__all__ = ["CARD_KINDS", "CallbackReply", "Reply", "TraderBot"]
