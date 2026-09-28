"""Стратегии вокруг ЛИСТИНГА — событие, а не показатель цены.

- `cex-perp-paper-listing-fade-short` — шорт перпа после первого полного дня торгов.

Чем это отличается от всего измеренного раньше. Тринадцать ценовых правил лаборатории
смотрели на ряд и искали в нём закономерность; здесь торгуется СОБЫТИЕ с известной
причиной. К листингу монету покупают на ожидании, после него продают те, кто держал её
дешевле: ранние инвесторы, фонды и команда. Спрос разовый, предложение постоянное.

Отсюда единственное требование, которого нет у остальных стратегий: правило должно
понимать, что инструмент ТОЛЬКО ЧТО появился. В бэктесте это видно по потоку — ряд
начинается позже начала окна; в бою дата листинга известна заранее.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle, Event, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)


@dataclass
class _Position:
    """Состояние ОДНОГО инструмента: сколько баров видели и что держим."""

    first_seen: datetime
    bars: int = 0
    qty: Decimal = ZERO
    entry: Decimal = ZERO
    opened_at: datetime | None = None


@preset(
    manifest_from_card(CARDS_DIR / "cex-perp-listing-fade-short.md", source_kind="paper")
)
class ListingFadeShortStrategy(Strategy):
    """Шорт перпа на закрытии первого полного дня торгов, выход через месяц или по стопу.

    Три решения в правилах, каждое из замера, а не из вкуса:

    * **день листинга не торгуется.** Цена открытия там условна (торги стартуют в середине
      суток), и вход по ней подарил бы результату движение, которого не было;
    * **стоп на +100%, а не ближе.** Замер распределения: у свежих листингов правый хвост
      доходит до +1871%, и без стопа один такой съедает семьдесят восемь удачных. Но стоп
      ближе выносит слишком часто: на +25% выносило 45% позиций и средняя падала. На +100%
      выносит одну из десяти, и средняя РАСТЁТ — это лучшая из измеренных границ;
    * **фиксированный номинал, а не доля капитала.** Проскальзывание у свежего перпа
      зависит от суммы, а не от того, сколько у нас денег.
    """

    def reset(self) -> None:
        self.book: dict[str, _Position] = {}
        self.stream_start: datetime | None = None
        self.open_count = 0
        self.listed_on: dict[str, date] = self._listing_dates()

    def _listing_dates(self) -> dict[str, date]:
        """Даты листинга из параметров: «инструмент → дата», JSON-строкой или словарём.

        Зачем явно, а не по первому бару потока. Торгуется бессрочный контракт, а событие —
        появление монеты на СПОТЕ, и у нашей выборки перп запущен РАНЬШЕ спота (иначе
        шортить было бы нечем). Первый бар перпа — это запуск контракта, другое событие
        и другая дата. Определение по потоку остаётся запасным путём: оно верно, когда
        перп и спот стартуют вместе, и это большинство листингов вне нашей выборки.
        """
        raw = self.manifest.params.get("listing_dates")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            return {}
        return {str(k): date.fromisoformat(str(v)) for k, v in raw.items()}

    # -- параметры ------------------------------------------------------------------

    @property
    def entry_bar(self) -> int:
        return int(self.manifest.params.get("entry_bar", 1))

    @property
    def hold(self) -> timedelta:
        return timedelta(days=int(self.manifest.params.get("hold_days", 30)))

    @property
    def stop_pct(self) -> Decimal:
        return D(str(self.manifest.params.get("trade_stop_pct", 50)))

    @property
    def trade_usd(self) -> Decimal:
        return D(str(self.manifest.params.get("trade_usd", 200)))

    @property
    def new_after(self) -> timedelta:
        return timedelta(days=int(self.manifest.params.get("min_new_after_days", 3)))

    @property
    def max_open(self) -> int:
        return int(self.manifest.params.get("max_open", 8))

    # -- правило --------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> list[Signal]:
        if self.stream_start is None:
            self.stream_start = bar.ts
        pos = self.book.get(bar.instrument)
        if pos is None:
            pos = self.book[bar.instrument] = _Position(first_seen=bar.ts)
        pos.bars += 1

        if pos.qty > 0:
            return self._maybe_close(bar, pos)
        return self._maybe_open(bar, pos)

    def _maybe_open(self, bar: Candle, pos: _Position) -> list[Signal]:
        if bar.close <= 0:
            return []
        listed = self.listed_on.get(bar.instrument)
        if listed is not None:
            # Дата листинга известна: вход — на баре КАЛЕНДАРНОГО дня «листинг + entry_bar»,
            # а не на N-м баре потока. До 28.09.2026 здесь считались бары перпа, и это
            # сдвигало вход в двух случаях (194 листинга замера 13.09): перп запущен только
            # в первый полный день — бара дня листинга у него нет, вход на сутки позже
            # (7 монет); листинг раньше начала окна — прогрева у движка нет, счёт шёл
            # с границы окна, и стратегия шортила монету через месяцы после листинга
            # (4 монеты, «вход» 10.10.2021). Нет бара этого дня — нет и входа: шортить
            # было нечем, как в правиле отбора исследования.
            if bar.ts.date() != listed + timedelta(days=self.entry_bar):
                return []
        else:
            # Запасной путь. Инструмент, торговавшийся ДО начала окна, листингом
            # не считается: его первый бар в потоке — граница окна, а не выход на биржу.
            if self.stream_start is None or pos.first_seen - self.stream_start < self.new_after:
                return []
            if pos.bars != self.entry_bar + 1:
                return []
        if self.open_count >= self.max_open:
            # Неделя массовых листингов не должна превращать всю ставку в одну.
            return []
        qty = self.trade_usd / bar.close
        if qty <= 0:
            return []
        pos.qty, pos.entry, pos.opened_at = qty, bar.close, bar.ts
        self.open_count += 1
        return [
            self._order(bar, "sell", qty, bar.close, {"kind": "listing_short_open"})
        ]

    def _maybe_close(self, bar: Candle, pos: _Position) -> list[Signal]:
        # Стоп проверяется по МАКСИМУМУ бара: шорт выносит внутри дня, а не по закрытию.
        limit = pos.entry * (1 + self.stop_pct / 100)
        if bar.high >= limit:
            return [self._close(bar, pos, limit, "stop")]
        if pos.opened_at is not None and bar.ts - pos.opened_at >= self.hold:
            return [self._close(bar, pos, bar.close, "hold_expired")]
        return []

    def _close(self, bar: Candle, pos: _Position, price: Decimal, reason: str) -> Signal:
        qty = pos.qty
        pos.qty, pos.entry, pos.opened_at = ZERO, ZERO, None
        self.open_count = max(0, self.open_count - 1)
        return self._order(
            bar, "buy", qty, price, {"kind": "listing_short_close", "reason": reason}
        )

    def _order(
        self,
        bar: Candle,
        side: str,
        qty: Decimal,
        price_ref: Decimal,
        inputs: dict[str, object],
    ) -> Signal:
        signal = self.signal(
            bar, side, qty, price_ref=price_ref, inputs={**inputs, "leg": bar.instrument}
        )
        return signal.model_copy(update={"instrument": bar.instrument})

    def on_event(self, event: Event) -> list[Signal]:
        return []


__all__ = ["ListingFadeShortStrategy"]
