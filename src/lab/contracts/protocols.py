"""Протоколы Feed / Executor / Strategy / NftMarket (interfaces.md, решения §6, §9).

paper и live для Executor — один класс, режим приходит параметром.
Ни один ордер не уходит без core.risk.check(...) == Allow — это обязанность вызывающего
слоя (core), исполнитель лишь принимает OrderIntent.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from lab.contracts.types import (
    Balance,
    Book,
    Candle,
    Costs,
    Event,
    Fill,
    Health,
    KeyRights,
    MintAttemptSpec,
    MintResult,
    ModeLiteral,
    NftMint,
    Order,
    OrderIntent,
    Position,
    Signal,
    StrategyManifest,
    Trade,
)


@runtime_checkable
class Feed(Protocol):
    id: str

    def candles(
        self, instrument: str, tf: str, from_ts: datetime, to_ts: datetime
    ) -> Sequence[Candle]: ...

    def trades(self, instrument: str, from_ts: datetime, to_ts: datetime) -> Sequence[Trade]: ...

    def book(self, instrument: str) -> Book: ...

    def events(self, kind: str) -> AsyncIterator[Event]: ...

    def health(self) -> Health: ...


@runtime_checkable
class Executor(Protocol):
    venue: str

    def rights(self) -> KeyRights: ...

    def place(self, intent: OrderIntent, mode: ModeLiteral) -> Order: ...

    def cancel(self, order_id: str) -> Order: ...

    def positions(self) -> Sequence[Position]: ...

    def fills(self, since: datetime) -> Sequence[Fill]: ...

    def balance(self) -> Sequence[Balance]: ...

    def health(self) -> Health: ...


@runtime_checkable
class Strategy(Protocol):
    manifest: StrategyManifest

    def on_bar(self, bar: Candle) -> Sequence[Signal]: ...

    def on_event(self, event: Event) -> Sequence[Signal]: ...


@runtime_checkable
class NftMarket(Protocol):
    market: str
    chain: str

    def upcoming(self) -> Sequence[NftMint]: ...

    def floor(self, collection: str) -> Decimal | None: ...

    def mint(self, spec: MintAttemptSpec) -> MintResult: ...

    def estimate_costs(self, spec: MintAttemptSpec) -> Costs: ...

    def health(self) -> Health: ...
