"""Накопление на споте — нижний ярус системы (`docs/research/system-2026-09-13.md`).

- `cex-spot-paper-cycle-ladder` — лестница откупа у дна и распределения у пика.

Чем это отличается от всего измеренного раньше. Верхние ярусы ищут преимущество;
нижний — копит монеты и обязан быть лучше самого простого способа копить: докупать
равными долями раз в месяц (`btc_dca`). Правило намеренно только ценовое: якорь есть
годовая средняя по прошлым барам, зоны — отклонение от якоря, вход и выход ступенями.
Сигналы дна из мастер-промпта накопления (MVRV, соц-объём, разблокировки) добавляются
вторым шагом, только как события с датой, и только если ценовая лестница вообще работает.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from lab.contracts import Candle, Event, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)


@dataclass
class _Book:
    """Состояние одного инструмента: якорь, позиция и где стоят обе лестницы."""

    closes: deque[Decimal]
    qty: Decimal = ZERO
    steps_bought: int = 0  # ступеней куплено в ТЕКУЩЕЙ просадке
    last_buy: Decimal = ZERO  # цена последней ступени покупки
    last_sell: Decimal = ZERO  # цена последней ступени продажи
    in_dip: bool = False
    in_rally: bool = False

    def anchor(self, need: int) -> Decimal | None:
        if len(self.closes) < need:
            return None
        return sum(self.closes, ZERO) / len(self.closes)


@preset(manifest_from_card(CARDS_DIR / "cex-spot-cycle-ladder.md", source_kind="paper"))
class CycleLadderStrategy(Strategy):
    """Покупать ступенями ниже годовой средней, продавать ступенями выше неё.

    Два решения, каждое из мастер-промпта накопления, а не из вкуса:

    * **зоны, а не точки.** Первая ступень — на границе зоны, следующие — по шагу от
      последней сделки. Так лестница не пытается угадать дно, а покрывает его;
    * **просадка и рост «закрываются».** Вернулась цена внутрь коридора — лестница
      сбрасывается, и следующее отклонение открывает новую. Без этого одна долгая
      просадка съела бы все ступени в первый месяц и дальше ждала бы годами.
    """

    def reset(self) -> None:
        self.books: dict[str, _Book] = {}

    # -- параметры ------------------------------------------------------------------

    def _p(self, name: str, default: object) -> Decimal:
        return D(str(self.manifest.params.get(name, default)))

    @property
    def anchor_days(self) -> int:
        return int(self.manifest.params.get("anchor_days", 365))

    @property
    def max_steps(self) -> int:
        return int(self.manifest.params.get("max_steps", 5))

    # -- правило --------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> list[Signal]:
        book = self.books.get(bar.instrument)
        if book is None:
            book = self.books[bar.instrument] = _Book(closes=deque(maxlen=self.anchor_days))
        # Якорь считается ДО добавления текущего бара: решение по закрытию дня не должно
        # видеть это же закрытие в своей средней — иначе средняя чуть «подтягивается»
        # к сигналу, и это заглядывание в будущее, пусть и на один бар.
        anchor = book.anchor(self.anchor_days)
        book.closes.append(bar.close)
        if anchor is None or anchor <= 0 or bar.close <= 0:
            return []

        price = bar.close
        buy_edge = anchor * (1 - self._p("buy_zone_pct", 20) / 100)
        sell_edge = anchor * (1 + self._p("sell_zone_pct", 50) / 100)

        # Коридор: лестницы сбрасываются, когда цена вернулась внутрь.
        if price > buy_edge and book.in_dip:
            book.in_dip, book.steps_bought, book.last_buy = False, 0, ZERO
        if price < sell_edge and book.in_rally:
            book.in_rally, book.last_sell = False, ZERO

        if price <= buy_edge:
            return self._maybe_buy(bar, book, price)
        if price >= sell_edge and book.qty > 0:
            return self._maybe_sell(bar, book, price)
        return []

    def _maybe_buy(self, bar: Candle, book: _Book, price: Decimal) -> list[Signal]:
        if book.steps_bought >= self.max_steps:
            return []
        step_down = 1 - self._p("buy_step_pct", 15) / 100
        if book.in_dip and price > book.last_buy * step_down:
            return []  # ещё не упала на шаг от последней покупки
        usd = self._p("capital_usd", 10_000) * self._p("step_capital_pct", 10) / 100
        qty = usd / price
        if qty <= 0:
            return []
        book.in_dip = True
        book.steps_bought += 1
        book.last_buy = price
        book.qty += qty
        return [
            self.signal(
                bar, "buy", qty, price_ref=price,
                inputs={"kind": "ladder_buy", "step": book.steps_bought},
            )
        ]

    def _maybe_sell(self, bar: Candle, book: _Book, price: Decimal) -> list[Signal]:
        step_up = 1 + self._p("sell_step_pct", 25) / 100
        if book.in_rally and price < book.last_sell * step_up:
            return []  # ещё не выросла на шаг от последней продажи
        qty = book.qty * self._p("sell_fraction_pct", 25) / 100
        if qty <= 0:
            return []
        book.in_rally = True
        book.last_sell = price
        book.qty -= qty
        return [
            self.signal(
                bar, "sell", qty, price_ref=price,
                inputs={"kind": "ladder_sell", "reason": "peak_zone"},
            )
        ]

    def on_event(self, event: Event) -> list[Signal]:
        return []


__all__ = ["CycleLadderStrategy"]
