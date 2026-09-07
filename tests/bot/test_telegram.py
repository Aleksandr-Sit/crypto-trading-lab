"""aiogram-адаптер: апдейты Telegram → TraderBot; сеть подменена фейковой сессией aiogram."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from lab.bot.telegram import TelegramTransport, build_dispatcher
from lab.ops.outbox import OutboxMessage
from tests.bot.conftest import ADMIN

STRANGER = 777
NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


class FakeSession(BaseSession):
    """Записывает вызовы Bot API и отвечает правдоподобными объектами; в сеть не ходит."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.down = False

    async def close(self) -> None:  # pragma: no cover - нечего закрывать
        pass

    async def stream_content(self, *a: Any, **k: Any):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None):
        if self.down:
            raise ConnectionError("telegram недоступен")
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls),
                date=NOW,
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, EditMessageText):
            return True
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(f"неожиданный вызов {method}")


def user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name="u")


def message_update(uid: int, text: str, upd_id: int = 1) -> Update:
    return Update(
        update_id=upd_id,
        message=Message(
            message_id=upd_id, date=NOW, chat=Chat(id=uid, type="private"), from_user=user(uid),
            text=text,
        ),
    )


def callback_update(uid: int, data: str, upd_id: int = 2) -> Update:
    msg = Message(message_id=50, date=NOW, chat=Chat(id=uid, type="private"), text="карточка")
    return Update(
        update_id=upd_id,
        callback_query=CallbackQuery(
            id="cb1", from_user=user(uid), chat_instance="ci", data=data, message=msg
        ),
    )


@pytest.fixture
def tg_session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def aiobot(tg_session) -> Bot:
    return Bot(token="123456:TEST", session=tg_session)


async def test_stranger_message_and_button_get_no_reply(bot, aiobot, tg_session) -> None:
    dp = build_dispatcher(bot)
    await dp.feed_update(aiobot, message_update(STRANGER, "/status"))
    await dp.feed_update(aiobot, callback_update(STRANGER, "confirm:x"))
    assert tg_session.calls == []


async def test_admin_command_and_button_go_through_bot(bot, aiobot, tg_session, halt) -> None:
    dp = build_dispatcher(bot)
    await dp.feed_update(aiobot, message_update(ADMIN, "/halt"))
    sent = [c for c in tg_session.calls if isinstance(c, SendMessage)]
    assert len(sent) == 1 and "Остановить" in sent[0].text
    assert sent[0].reply_markup is not None and sent[0].parse_mode == "HTML"
    data = sent[0].reply_markup.inline_keyboard[0][0].callback_data

    await dp.feed_update(aiobot, callback_update(ADMIN, data))
    assert halt.is_halted()
    kinds = [type(c).__name__ for c in tg_session.calls[1:]]
    assert kinds == ["AnswerCallbackQuery", "EditMessageText"]


async def test_transport_sends_and_edits_and_reports_outage(aiobot, tg_session) -> None:
    from lab.ops.outbox import Button, TransportError

    transport = TelegramTransport(aiobot)
    msg = OutboxMessage(
        chat_id=str(ADMIN), kind="alert", text="<b>тест</b>",
        buttons=[[Button(text="Ок", data="x:1")]],
    )
    msg_id = await transport.send(msg)
    assert msg_id == 1 and isinstance(tg_session.calls[-1], SendMessage)
    await transport.edit(str(ADMIN), msg_id, OutboxMessage(chat_id=str(ADMIN), text="готово"))
    last = tg_session.calls[-1]
    assert isinstance(last, EditMessageText) and last.text == "готово"

    tg_session.down = True
    with pytest.raises(TransportError):
        await transport.send(msg)
