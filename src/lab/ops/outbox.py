"""Очередь исходящих сообщений Telegram (R13.5): недоставленное хранится в таблице `outbox`,
повтор — с экспоненциальной задержкой `base_delay · 2^(attempts-1)`, не больше `max_delay`;
после `max_attempts` неудач сообщение помечается `dead` и больше не повторяется.

Транспорт (`Transport`) — единственная точка, где живёт aiogram; в тестах — фейк.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.db.models import OutboxRow

log = logging.getLogger(__name__)


class TransportError(Exception):
    """Telegram недоступен или отверг запрос: сообщение остаётся в очереди."""


class Button(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    data: str  # callback_data, ≤ 64 байт по правилам Telegram


class OutboxMessage(BaseModel):
    """Сообщение в очереди: текст + ряды inline-кнопок. `ref` — внешний ключ
    (например id сигнала), по которому сообщение потом редактируют."""

    model_config = ConfigDict(frozen=True)

    chat_id: str
    kind: str = "alert"
    text: str
    buttons: list[list[Button]] = Field(default_factory=list)
    ref: str | None = None
    parse_mode: str | None = "HTML"

    @property
    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"chat_id", "kind", "ref"})

    @classmethod
    def from_row(cls, row: OutboxRow) -> OutboxMessage:
        return cls(chat_id=row.chat_id, kind=row.kind, ref=row.ref, **row.payload)


class Transport(Protocol):
    async def send(self, msg: OutboxMessage) -> int:
        """Отправить; вернуть message_id. Недоступность → `TransportError`."""

    async def edit(self, chat_id: str, message_id: int, msg: OutboxMessage) -> None: ...


@dataclass
class FlushReport:
    delivered: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class Pending:
    id: int
    attempts: int
    text: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Outbox:
    def __init__(
        self,
        session: Session,
        transport: Transport,
        *,
        base_delay: timedelta = timedelta(seconds=30),
        max_delay: timedelta = timedelta(minutes=30),
        max_attempts: int = 50,
    ) -> None:
        self.s = session
        self.transport = transport
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_attempts = max_attempts

    # -- запись --------------------------------------------------------------------------

    def enqueue(self, msg: OutboxMessage, *, now: datetime | None = None) -> int:
        row = OutboxRow(
            chat_id=msg.chat_id,
            kind=msg.kind,
            ref=msg.ref,
            payload=msg.payload,
            attempts=0,
            created_at=now or _utcnow(),
            next_attempt_at=None,  # готово сразу
        )
        self.s.add(row)
        self.s.flush()
        return row.id

    # -- доставка ------------------------------------------------------------------------

    def _pending_rows(self, now: datetime) -> list[OutboxRow]:
        stmt = (
            select(OutboxRow)
            .where(OutboxRow.delivered_at.is_(None), OutboxRow.dead.is_(False))
            .where((OutboxRow.next_attempt_at.is_(None)) | (OutboxRow.next_attempt_at <= now))
            .order_by(OutboxRow.id)
        )
        return list(self.s.scalars(stmt))

    def pending(self, *, now: datetime | None = None) -> list[Pending]:
        now = now or _utcnow()
        return [
            Pending(id=r.id, attempts=r.attempts, text=r.payload.get("text", ""))
            for r in self._pending_rows(now)
        ]

    def delay_after(self, attempts: int) -> timedelta:
        """Задержка после `attempts`-й неудачи: base · 2^(attempts-1), не больше max_delay."""
        return min(self.base_delay * (2 ** max(attempts - 1, 0)), self.max_delay)

    async def flush(self, *, now: datetime | None = None) -> FlushReport:
        """Попробовать доставить всё, что созрело. Одна неудача не останавливает остальные."""
        now = now or _utcnow()
        report = FlushReport()
        for row in self._pending_rows(now):
            msg = OutboxMessage.from_row(row)
            try:
                row.message_id = await self.transport.send(msg)
            except TransportError as err:
                row.attempts += 1
                row.last_error = str(err)[:500]
                if row.attempts >= self.max_attempts:
                    row.dead = True
                    log.error("outbox #%s: %s попыток, сообщение отброшено", row.id, row.attempts)
                else:
                    row.next_attempt_at = now + self.delay_after(row.attempts)
                report.failed.append(row.id)
            else:
                row.attempts += 1
                row.delivered_at = now
                row.last_error = None
                report.delivered.append(row.id)
            self.s.flush()
        return report

    async def edit(self, *, ref: str, msg: OutboxMessage) -> None:
        """Отредактировать уже доставленное сообщение по `ref` (сигнал протух и т. п.).
        Неизвестный `ref` → `KeyError`; недоставленное — заменяется прямо в очереди."""
        row = self._by_ref(ref)
        if row is None:
            raise KeyError(f"outbox: сообщение с ref={ref!r} не найдено")
        if row.message_id is None:
            row.payload = msg.payload
            self.s.flush()
            return
        await self.transport.edit(row.chat_id, row.message_id, msg)
        row.payload = msg.payload
        self.s.flush()

    # -- справки -------------------------------------------------------------------------

    def _by_ref(self, ref: str) -> OutboxRow | None:
        stmt = select(OutboxRow).where(OutboxRow.ref == ref).order_by(OutboxRow.id.desc())
        return self.s.scalars(stmt).first()

    def get(self, ref: str) -> OutboxMessage | None:
        """Последнее сообщение с этим `ref` (как оно лежит в очереди / было отправлено)."""
        row = self._by_ref(ref)
        return OutboxMessage.from_row(row) if row else None

    def message_id(self, row_id: int) -> int | None:
        return self.s.get_one(OutboxRow, row_id).message_id

    def message_id_for(self, ref: str) -> int | None:
        row = self._by_ref(ref)
        return row.message_id if row else None

    def attempts(self, row_id: int) -> int:
        return self.s.get_one(OutboxRow, row_id).attempts

    def dead(self) -> list[int]:
        stmt = select(OutboxRow.id).where(OutboxRow.dead.is_(True)).order_by(OutboxRow.id)
        return list(self.s.scalars(stmt))
