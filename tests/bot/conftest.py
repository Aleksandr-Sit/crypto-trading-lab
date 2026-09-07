"""Бот на фейковом Telegram: транспорт запоминает отправленное и отредактированное."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.bot import TraderBot
from lab.contracts import Signal
from lab.core.journal import Journal
from lab.core.ladder import Ladder
from lab.core.registry import Registry
from lab.core.risk import MemoryHaltSwitch
from lab.ops.outbox import OutboxMessage, TransportError
from tests.core.test_ladder import FakeThreshold, manifest

ADMIN = 100500
T0 = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[OutboxMessage] = []
        self.edited: list[tuple[int, OutboxMessage]] = []
        self.down = False

    async def send(self, msg: OutboxMessage) -> int:
        if self.down:
            raise TransportError("сеть недоступна")
        self.sent.append(msg)
        return 1000 + len(self.sent)

    async def edit(self, chat_id: str, message_id: int, msg: OutboxMessage) -> None:
        if self.down:
            raise TransportError("сеть недоступна")
        self.edited.append((message_id, msg))

    def texts(self) -> list[str]:
        return [m.text for m in self.sent]


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def halt() -> MemoryHaltSwitch:
    return MemoryHaltSwitch()


@pytest.fixture
def clock():
    state = {"now": T0}

    def now() -> datetime:
        return state["now"]

    now.set = lambda t: state.__setitem__("now", t)  # type: ignore[attr-defined]
    return now


@pytest.fixture
def bot(session, tg, halt, clock) -> TraderBot:
    return TraderBot(
        session_factory=lambda: session,
        admin_id=ADMIN,
        transport=tg,
        ladder_factory=lambda s: Ladder(s, threshold=FakeThreshold(), halt=halt),
        clock=clock,
        close_sessions=False,
    )


@pytest.fixture
def strategy(session):
    return Registry(session).add(manifest())


def make_signal(strategy_id: str, *, ttl_s: int = 600, decided_at: datetime = T0) -> Signal:
    return Signal(
        strategy_id=strategy_id,
        decided_at=decided_at,
        instrument="BTC-USDT",
        side="buy",
        size=Decimal("0.01"),
        price_ref=Decimal("60000"),
        inputs_hash="a" * 64,
        ttl_s=ttl_s,
        meta={},
    )


@pytest.fixture
def journal(session) -> Journal:
    return Journal(session)
