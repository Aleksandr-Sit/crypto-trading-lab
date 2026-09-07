"""aiogram 3: единственное место, где бот Trader касается Telegram.

`TelegramTransport` — транспорт для `ops.outbox` (send/edit через Bot API);
`build_dispatcher(bot)` — маршрутизация апдейтов в `TraderBot.handle_command/handle_callback`;
`run_bot(...)` — long polling сервиса `lab service bot`. Чужие апдейты не получают ответа.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from lab.bot.core import TraderBot
from lab.ops.outbox import Button, OutboxMessage, TransportError

log = logging.getLogger(__name__)


def keyboard(buttons: list[list[Button]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = [
        [InlineKeyboardButton(text=b.text, callback_data=b.data) for b in row] for row in buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class TelegramTransport:
    """`ops.outbox.Transport` поверх aiogram `Bot`. Любая сетевая/серверная ошибка →
    `TransportError`, сообщение остаётся в очереди."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, msg: OutboxMessage) -> int:
        try:
            sent = await self.bot.send_message(
                chat_id=msg.chat_id,
                text=msg.text,
                parse_mode=msg.parse_mode,
                reply_markup=keyboard(msg.buttons),
            )
        except (TelegramNetworkError, TelegramRetryAfter, TelegramAPIError, OSError) as err:
            raise TransportError(str(err)) from err
        return sent.message_id

    async def edit(self, chat_id: str, message_id: int, msg: OutboxMessage) -> None:
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=msg.text,
                parse_mode=msg.parse_mode,
                reply_markup=keyboard(msg.buttons),
            )
        except (TelegramNetworkError, TelegramRetryAfter, TelegramAPIError, OSError) as err:
            raise TransportError(str(err)) from err


def make_aiogram_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))


def build_dispatcher(bot: TraderBot) -> Dispatcher:
    """Роутер: текст → `handle_command`, кнопки → `handle_callback`. `None` от бота
    (чужой id) — молчание: ни ответа, ни всплывашки."""
    router = Router(name="trader")

    @router.message(F.text)
    async def on_message(message: Message) -> None:
        uid = message.from_user.id if message.from_user else None
        reply = await bot.handle_command(uid, message.text or "")
        if reply is None:
            return
        await message.answer(reply.text, parse_mode="HTML", reply_markup=keyboard(reply.buttons))

    @router.callback_query(F.data)
    async def on_callback(query: CallbackQuery) -> None:
        reply = await bot.handle_callback(query.from_user.id, query.data or "")
        if reply is None:
            return
        await query.answer(reply.answer[:200])
        if reply.text is not None and isinstance(query.message, Message):
            try:
                await query.message.edit_text(
                    reply.text, parse_mode="HTML", reply_markup=keyboard(reply.buttons)
                )
            except TelegramAPIError as err:  # «message is not modified» и т. п.
                log.debug("не удалось отредактировать сообщение под кнопкой: %s", err)

    dp = Dispatcher()
    dp.include_router(router)
    return dp


def threadsafe_runner(loop: asyncio.AbstractEventLoop) -> Callable[[Coroutine[Any, Any, Any]], Any]:
    """Исполнитель корутин для заданий APScheduler из потока планировщика в цикл aiogram."""

    def run(coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    return run


async def run_bot(bot: TraderBot, aio: Bot, *, scheduler=None, heartbeat=None) -> None:
    """Long polling + задания бота (утренний отчёт, протухание, повтор outbox)."""
    dp = build_dispatcher(bot)
    if scheduler is not None:
        bot.jobs(scheduler, run=threadsafe_runner(asyncio.get_running_loop()))
        scheduler.start()
    if heartbeat is not None:

        async def beat() -> None:
            while True:
                heartbeat()
                await asyncio.sleep(10)

        asyncio.create_task(beat())
    try:
        await bot.flush()  # недоставленное с прошлого запуска
        await dp.start_polling(aio, handle_signals=False)
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        await aio.session.close()


__all__ = [
    "TelegramTransport",
    "build_dispatcher",
    "keyboard",
    "make_aiogram_bot",
    "run_bot",
    "threadsafe_runner",
]
