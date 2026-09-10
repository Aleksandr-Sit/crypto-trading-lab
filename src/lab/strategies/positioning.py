"""Стратегии по ПОЗИЦИОНИРОВАНИЮ — данные другой природы, чем цена.

- `cex-perp-api-funding-extreme-reversal` — экстремальная ставка фандинга как контр-сигнал.

Зачем отдельный модуль. Тринадцать ценовых правил лаборатория измерила, и ни одно не дало
устойчивого преимущества; фильтры режима по цене опаздывали. Единственный сигнал, который
опередил цену (сентябрь 2020, ETH), был ставкой фандинга — то есть измерял не цену,
а позиционирование: сколько плеча в рынке и кто за него платит. Здесь собираются правила,
которые читают именно это.

Общее устройство: ставки приходят событием `funding` (движок отдаёт их из истории), решения
принимаются по перцентилю текущей ставки в её собственном окне. Порогов в абсолютных числах
нет намеренно — «выше 0.05%» пришлось бы подбирать по известному исходу, а перцентиль
сравнивает ряд с самим собой.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle, Event, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)
PAYOUTS_PER_DAY = 3


def percentile_rank(values: deque[Decimal] | list[Decimal], value: Decimal) -> Decimal:
    """Доля значений окна, не превышающих `value`, в процентах.

    Ряд сравнивается сам с собой: у BTC в 2021 «обычная» ставка была выше, чем экстремум
    2023 года, и абсолютный порог одинаково врал бы в обе эпохи.
    """
    if not values:
        return D(50)
    below = sum(1 for v in values if v <= value)
    return D(below) * 100 / len(values)


@dataclass
class _Book:
    """Состояние ОДНОГО инструмента: история ставок и позиция. У BTC и ETH они разные."""

    rates: deque[Decimal]
    price: Decimal = ZERO
    last_bar: Candle | None = None
    side: str | None = None  # long | short
    qty: Decimal = ZERO
    opened_at: datetime | None = None
    entry_pct: Decimal = ZERO


@preset(
    manifest_from_card(CARDS_DIR / "cex-perp-funding-extreme-reversal.md", source_kind="api")
)
class FundingExtremeReversalStrategy(Strategy):
    """Шорт, когда лонги переполнены (ставка в верхних процентах истории), лонг — наоборот.

    Экономика: каждый, кто хотел купить с плечом, уже купил и теперь платит за это
    каждые восемь часов. Шорт в этот момент получает и выплату, и движение против толпы.

    Чего в правилах нет намеренно — пирамидинга при углублении экстремума: переполненность
    может держаться неделями (февраль 2021 — выше 100% годовых весь месяц), и докупать
    против неё — путь к ликвидации. Одна позиция на инструмент.
    """

    card = "cex-perp-funding-extreme-reversal"

    def reset(self) -> None:
        self.books: dict[str, _Book] = {}

    # -- состояние ------------------------------------------------------------------

    def _book(self, instrument: str) -> _Book:
        book = self.books.get(instrument)
        if book is None:
            window = int(self.param("lookback_days", 365)) * PAYOUTS_PER_DAY
            book = self.books[instrument] = _Book(rates=deque(maxlen=window))
        return book

    def _notional(self) -> Decimal:
        capital = D(str(self.param("capital_usd", 10_000)))
        share = D(str(self.param("max_notional_pct_of_branch", 50))) / 100
        n = len(self.manifest.instruments) or 1
        return capital * share / n

    # -- данные ---------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> list[Signal]:
        book = self._book(bar.instrument)
        book.price = bar.close
        book.last_bar = bar
        if book.side is None or book.opened_at is None:
            return []
        # Предельное удержание: контр-сигнал не должен превращаться в позицию по тренду.
        hold = timedelta(hours=int(self.param("hold_hours", 72)))
        if bar.ts - book.opened_at >= hold:
            return self._close(book, bar, "время вышло")
        return []

    def on_event(self, event: Event) -> list[Signal]:
        if event.kind != "funding":
            return []
        instrument = str(event.payload.get("instrument", ""))
        book = self._book(instrument)
        rate = D(str(event.payload.get("rate", 0)))
        # Перцентиль считается ДО добавления текущей ставки: ставка не должна сравниваться
        # сама с собой, иначе первая же выплата в пустом окне получает 100%.
        history = list(book.rates)
        book.rates.append(rate)
        bar = book.last_bar
        if bar is None or len(history) < int(self.param("min_history", 300)):
            return []
        pct = percentile_rank(history, rate)
        if book.side is None:
            return self._maybe_open(book, bar, pct)
        return self._maybe_exit(book, bar, pct)

    # -- решения --------------------------------------------------------------------

    def _maybe_open(self, book: _Book, bar: Candle, pct: Decimal) -> list[Signal]:
        upper = D(str(self.param("upper_pct", 95)))
        lower = D(str(self.param("lower_pct", 5)))
        if pct >= upper:
            side = "short"
        elif pct <= lower:
            side = "long"
        else:
            return []
        if book.price <= 0:
            return []
        qty = self._notional() / book.price
        if qty <= 0:
            return []
        book.side, book.qty, book.opened_at, book.entry_pct = side, qty, bar.ts, pct
        inputs = {"kind": "funding_extreme_open", "side": side, "pct": str(pct)}
        return [self._order(bar, "sell" if side == "short" else "buy", qty, inputs)]

    def _maybe_exit(self, book: _Book, bar: Candle, pct: Decimal) -> list[Signal]:
        """Ставка вернулась к середине — переполненности больше нет, держать незачем."""
        exit_pct = D(str(self.param("exit_pct", 50)))
        if book.side == "short" and pct <= exit_pct:
            return self._close(book, bar, "ставка остыла")
        if book.side == "long" and pct >= 100 - exit_pct:
            return self._close(book, bar, "ставка остыла")
        return []

    def _close(self, book: _Book, bar: Candle, reason: str) -> list[Signal]:
        if book.side is None or book.qty <= 0:
            return []
        side = "buy" if book.side == "short" else "sell"
        qty = book.qty
        book.side, book.qty, book.opened_at = None, ZERO, None
        return [self._order(bar, side, qty, {"kind": "funding_extreme_close", "reason": reason})]

    def _order(self, bar: Candle, side: str, qty: Decimal, inputs: dict[str, object]) -> Signal:
        """Решение принято по событию фандинга, но время сигнала — закрытие бара.

        Событие приходит С МОМЕНТОМ внутри уже обработанного бара, и сигнал с этим временем
        движок отверг бы как заглядывание в будущее. Исполнение — на открытии следующего бара.
        """
        return self.signal(bar, side, qty, inputs=inputs)


__all__ = ["FundingExtremeReversalStrategy", "percentile_rank"]
