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
from dataclasses import dataclass
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
    """Место значения в своём окне, в процентах: строго меньшие плюс половина равных.

    Ряд сравнивается сам с собой: у BTC в 2021 «обычная» ставка была выше, чем экстремум
    2023 года, и абсолютный порог одинаково врал бы в обе эпохи.

    Половина равных — не украшение, а защита от вырожденного случая. Считая «долю
    не превышающих», ровный ряд даёт КАЖДОЙ ставке сотый перцентиль: у стейблкоиновых
    пар и в спокойные месяцы ставка неделями стоит на одном значении, и стратегия
    открывала бы «экстремум» на самой обычной выплате. При среднем ранге ровный ряд
    честно даёт середину — сигнала нет.
    """
    if not values:
        return D(50)
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (D(below) + D(equal) / 2) * 100 / len(values)


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


@dataclass
class _Flush:
    """Состояние ОДНОГО инструмента: вчерашние ориентиры и открытая позиция.

    Два закрытия, а не одно: правило сравнивает сегодняшнее со вчерашним, и хранить надо
    оба. Событие метрик приходит ПОСЛЕ бара того же дня, поэтому `close_today` к моменту
    решения уже сегодняшнее — заглядывания вперёд нет.
    """

    prev_oi: Decimal | None = None
    close_today: Decimal | None = None
    close_yesterday: Decimal | None = None
    last_bar: Candle | None = None
    qty: Decimal = ZERO
    opened_at: datetime | None = None


@preset(manifest_from_card(CARDS_DIR / "cex-perp-oi-flush.md", source_kind="api"))
class OpenInterestFlushStrategy(Strategy):
    """Покупка после ВЫМЫВАНИЯ плеча: открытый интерес сжался вместе с ценой.

    Экономика: резкое сжатие интереса на падающей цене — это не спокойный уход из позиций,
    а принудительные закрытия. После них давление продаж снято: тех, кого могли заставить
    продать, уже заставили.

    Почему это не повтор фандинга: ставка говорит, сколько ПЛАТЯТ за плечо, интерес —
    сколько его НАБРАЛИ. Связь между изменением интереса и ставкой −0.03, то есть её нет.

    Чего в правилах нет намеренно — шорта на обратном сигнале (интерес растёт вместе
    с ценой). Симметрия красива, но причина у неё слабее: накопление плеча длится
    месяцами, и замер экстремального фандинга это уже показал — шорт против толпы
    теряет на цене в восемь раз больше, чем собирает.
    """

    card = "cex-perp-oi-flush"

    def reset(self) -> None:
        self.flush: dict[str, _Flush] = {}

    def _state(self, instrument: str) -> _Flush:
        state = self.flush.get(instrument)
        if state is None:
            state = self.flush[instrument] = _Flush()
        return state

    def _notional(self) -> Decimal:
        capital = D(str(self.param("capital_usd", 10_000)))
        share = D(str(self.param("max_notional_pct_of_branch", 50))) / 100
        n = len(self.manifest.instruments) or 1
        return capital * share / n

    def on_bar(self, bar: Candle) -> list[Signal]:
        """Бар нужен для выхода по сроку и для пары закрытий: вход решается на метриках."""
        state = self._state(bar.instrument)
        state.close_yesterday, state.close_today = state.close_today, bar.close
        state.last_bar = bar
        if state.qty <= 0 or state.opened_at is None:
            return []
        hold = timedelta(days=int(self.param("hold_days", 5)))
        if bar.ts - state.opened_at < hold:
            return []
        qty = state.qty
        state.qty, state.opened_at = ZERO, None
        return [
            self.signal(bar, "sell", qty, inputs={"kind": "flush_close", "reason": "срок вышел"})
        ]

    def on_event(self, event: Event) -> list[Signal]:
        if event.kind != "positioning":
            return []
        state = self._state(str(event.payload.get("instrument", "")))
        oi = D(str(event.payload.get("open_interest", 0)))
        prev_oi = state.prev_oi
        state.prev_oi = oi
        if prev_oi is None or prev_oi <= 0 or oi <= 0 or state.qty > 0:
            # Позиция уже есть — докупать в вымывание значит ловить нож: одна на инструмент.
            return []
        today, yesterday = state.close_today, state.close_yesterday
        bar = state.last_bar
        if bar is None or today is None or yesterday is None or yesterday <= 0:
            return []

        oi_drop = (prev_oi - oi) / prev_oi * 100
        price_drop = (yesterday - today) / yesterday * 100
        # ОБА условия обязательны. Сжатие интереса на растущей цене — это фиксация
        # прибыли, а не вымывание: продавцов заставили выйти только во втором случае.
        if oi_drop < D(str(self.param("flush_drop_pct", 5))):
            return []
        if price_drop < D(str(self.param("flush_price_drop_pct", 3))):
            return []
        if today <= 0:
            return []
        qty = self._notional() / today
        if qty <= 0:
            return []
        state.qty, state.opened_at = qty, bar.ts
        return [
            self.signal(
                bar,
                "buy",
                qty,
                inputs={
                    "kind": "flush_open",
                    "oi_drop": str(oi_drop),
                    "price_drop": str(price_drop),
                },
            )
        ]


__all__ = [
    "FundingExtremeReversalStrategy",
    "OpenInterestFlushStrategy",
    "percentile_rank",
]
