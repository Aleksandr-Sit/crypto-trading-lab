"""ops.outbox: недоставленное хранится, повтор с экспоненциальной задержкой (R13.5)."""

from datetime import UTC, datetime, timedelta

import pytest

from lab.ops.outbox import Outbox, OutboxMessage, TransportError

T0 = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


class FakeTelegram:
    """Фейковый транспорт: падает `fail_times` раз, потом доставляет."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.sent: list[OutboxMessage] = []
        self.edited: list[tuple[int, str]] = []

    async def send(self, msg: OutboxMessage) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransportError("telegram недоступен")
        self.sent.append(msg)
        return 1000 + len(self.sent)

    async def edit(self, chat_id: str, message_id: int, msg: OutboxMessage) -> None:
        self.edited.append((message_id, msg.text))


def msg(text: str = "привет", **extra) -> OutboxMessage:
    return OutboxMessage(chat_id="42", kind="alert", text=text, **extra)


async def test_delivered_message_gets_message_id_and_delivered_at(session) -> None:
    tg = FakeTelegram()
    box = Outbox(session, tg)
    row_id = box.enqueue(msg("раз"))
    report = await box.flush(now=T0)
    assert report.delivered == [row_id] and report.failed == []
    assert box.message_id(row_id) == 1001
    assert box.pending(now=T0 + timedelta(days=1)) == []
    assert tg.sent[0].text == "раз"


async def test_failed_delivery_is_kept_and_retried_with_backoff(session) -> None:
    tg = FakeTelegram(fail_times=2)
    box = Outbox(session, tg, base_delay=timedelta(seconds=30))
    row_id = box.enqueue(msg("два"))

    first = await box.flush(now=T0)
    assert first.failed == [row_id] and tg.sent == []
    # после первой неудачи — ждём 30 с: через 10 с не повторяем
    assert [m.id for m in box.pending(now=T0 + timedelta(seconds=10))] == []
    second = await box.flush(now=T0 + timedelta(seconds=30))
    assert second.failed == [row_id]
    # после второй — ждём уже 60 с (30 · 2^1)
    assert [m.id for m in box.pending(now=T0 + timedelta(seconds=59))] == []
    assert [m.id for m in box.pending(now=T0 + timedelta(seconds=90))] == [row_id]
    third = await box.flush(now=T0 + timedelta(seconds=90))
    assert third.delivered == [row_id]
    assert [m.text for m in tg.sent] == ["два"]
    assert box.attempts(row_id) == 3


async def test_backoff_is_capped_and_message_dropped_after_max_attempts(session) -> None:
    tg = FakeTelegram(fail_times=100)
    box = Outbox(
        session, tg, base_delay=timedelta(seconds=30), max_delay=timedelta(minutes=5),
        max_attempts=4,
    )
    row_id = box.enqueue(msg("три"))
    now = T0
    for _ in range(4):
        now += timedelta(hours=1)
        await box.flush(now=now)
    assert box.attempts(row_id) == 4
    assert box.pending(now=now + timedelta(days=1)) == []  # больше не повторяем
    assert box.dead() == [row_id]


async def test_edit_goes_through_transport_by_stored_message_id(session) -> None:
    tg = FakeTelegram()
    box = Outbox(session, tg)
    row_id = box.enqueue(msg("сигнал", ref="sig-1"))
    await box.flush(now=T0)
    await box.edit(ref="sig-1", msg=msg("сигнал — просрочен"))
    assert tg.edited == [(box.message_id(row_id), "сигнал — просрочен")]


async def test_edit_unknown_ref_raises(session) -> None:
    box = Outbox(session, FakeTelegram())
    with pytest.raises(KeyError):
        await box.edit(ref="нет-такого", msg=msg("x"))
