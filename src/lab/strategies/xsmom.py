"""Кросс-секционные стратегии: решение принимается СРАВНЕНИЕМ инструментов между собой.

- `cex-spot-xsmom-alts-weekly` — моментум альтов: покупать лучших за три недели, держать неделю.

Отличие от всего, что было раньше в лаборатории: правило смотрит не на один ряд, а на срез
рынка целиком. Отсюда два требования, которых нет у остальных стратегий. Первое — решение
принимается только по ПОЛНОМУ дню: пока бары дня приходят по одному, срез неполон, и
ранжировать нечего. Второе — вселенная обязана содержать и мёртвые пары: если брать
сегодняшних лидеров, бэктест покупает только тех, кто выжил, и всегда показывает прибыль.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from lab.contracts import Candle, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)
HISTORY = 110  # хватает и на SMA(100) биткоина, и на моментум за три недели


@dataclass
class _Asset:
    """Состояние ОДНОГО инструмента: история по дням и своя позиция.

    Закрытия хранятся вместе с ДАТОЙ, а не по индексу: у альтов ряды начинаются в разные
    годы и обрываются при делистинге, и «21 бар назад» у двух пар — это разные даты.
    """

    closes: deque[tuple[date, Decimal]] = field(default_factory=lambda: deque(maxlen=HISTORY))
    turnover: deque[Decimal] = field(default_factory=lambda: deque(maxlen=30))
    price: Decimal = ZERO
    qty: Decimal = ZERO
    entry: Decimal = ZERO

    def close_at(self, day: date) -> Decimal | None:
        """Закрытие на дату или ближайшее до неё — пропуск дня не должен сдвигать окно."""
        for stamp, close in reversed(self.closes):
            if stamp <= day:
                return close
        return None


@preset(manifest_from_card(CARDS_DIR / "cex-spot-xsmom-alts-weekly.md", source_kind="paper"))
class CrossSectionalMomentumStrategy(Strategy):
    """Раз в неделю купить `top_n` альтов с лучшей доходностью за три недели.

    Liu–Tsyvinski–Wu документируют моментум-премию на горизонте 1–4 недель. Вселенная —
    `universe_size` самых оборотистых пар на дату решения; фильтр по BTC выше SMA держит
    в кэше на медвежьем рынке; стоп −15% от входа закрывает позицию не дожидаясь ребаланса.

    Чего в правилах нет намеренно: доведения долей до равных на каждом ребалансе (карточка
    требует продавать выпавших и покупать вошедших, а не подрезать выросших) — это удвоило
    бы оборот, а он здесь и так главная статья расходов.
    """

    card = "cex-spot-xsmom-alts-weekly"

    def reset(self) -> None:
        self.assets: dict[str, _Asset] = {}
        self.day: date | None = None
        self.since_rebalance = 0
        self.held: set[str] = set()

    # -- данные ---------------------------------------------------------------------

    def _asset(self, instrument: str) -> _Asset:
        asset = self.assets.get(instrument)
        if asset is None:
            asset = self.assets[instrument] = _Asset()
        return asset

    def on_bar(self, bar: Candle) -> list[Signal]:
        day = bar.ts.date()
        out: list[Signal] = []
        if self.day is None:
            self.day = day
        elif day != self.day:
            # Пришёл бар СЛЕДУЮЩЕГО дня — значит предыдущий закрыт целиком по всем
            # инструментам, и только теперь срез рынка полон. Раньше ранжировать нельзя:
            # часть пар этого дня ещё не пришла бы, и вселенная получилась бы случайной.
            closed, self.day = self.day, day
            self.since_rebalance += 1
            # Ширина проверяется КАЖДЫЙ день, а не раз в неделю: в сентябре 2020 она
            # рухнула с 80% до 22% за двое суток, и ждать ребаланса значило бы отдать
            # рынку ещё неделю. Выход из фазы — это стоп режима, а не ротация портфеля.
            if self.held and not self._breadth_allows(closed):
                out += self._exit_all(bar, "ширина рынка")
                self.since_rebalance = 0
            elif self.since_rebalance >= int(self.param("rebalance_days", 7)):
                self.since_rebalance = 0
                out += self._rebalance(bar, closed)
        asset = self._asset(bar.instrument)
        asset.price = bar.close
        asset.closes.append((day, bar.close))
        asset.turnover.append(bar.close * bar.volume)
        out += self._stop(bar, asset)
        return out

    # -- правила --------------------------------------------------------------------

    def _stop(self, bar: Candle, asset: _Asset) -> list[Signal]:
        """Стоп −15% от входа: ждать ребаланса неделю на падающей позиции незачем."""
        if asset.qty <= 0 or asset.entry <= 0:
            return []
        # Имя параметра НЕ `stop_loss_pct`: под этим именем `manifest_from_card` заводит
        # стоп ВСЕЙ стратегии (`StopSpec.max_dd_pct`), а здесь стоп на одну сделку.
        limit = D(1) - D(str(self.param("trade_stop_pct", 15))) / 100
        if bar.close > asset.entry * limit:
            return []
        return [self._sell(bar, bar.instrument, asset, "стоп")]

    def _eligible(self, day: date) -> list[tuple[Decimal, str]]:
        """Вселенная на дату: пары с полной 30-дневной историей оборота, по обороту вниз."""
        floor = D(str(self.param("min_daily_volume_usd", 0)))
        rows: list[tuple[Decimal, str]] = []
        for name, asset in self.assets.items():
            if len(asset.turnover) < asset.turnover.maxlen or asset.price <= 0:
                continue
            mean = sum(asset.turnover, ZERO) / len(asset.turnover)
            if mean < floor:
                continue
            # Пара должна торговаться и НА дату решения: у делистнутой последний бар
            # остаётся в памяти навсегда, и без этой проверки она вечно в топе.
            last_day = asset.closes[-1][0] if asset.closes else None
            if last_day is None or (day - last_day) > timedelta(days=3):
                continue
            rows.append((mean, name))
        rows.sort(reverse=True)
        return rows[: int(self.param("universe_size", 50))]

    def _momentum(self, asset: _Asset, day: date) -> Decimal | None:
        skip = int(self.param("skip_days", 1))
        lookback = int(self.param("lookback_days", 21))
        recent = asset.close_at(day - timedelta(days=skip))
        older = asset.close_at(day - timedelta(days=skip + lookback))
        if recent is None or older is None or older <= 0:
            return None
        return recent / older - 1

    def breadth_pct(self, day: date) -> Decimal | None:
        """Доля пар вселенной выше своей SMA — «ширина рынка», %.

        Зачем отдельно от фильтра по BTC: тот смотрит на ОДИН ряд и опаздывает на месяцы.
        В сентябре 2020 биткоин был выше своей средней (10736 против 10421), а доля альтов
        выше своей рухнула с 80% до 22% за два дня — и именно тогда моментум отдал всё,
        что набрал за лето. Считается по тем же данным, что уже есть у стратегии.
        """
        days = int(self.param("breadth_sma_days", 50))
        alive = fresh = 0
        for name, asset in self.assets.items():
            if name == str(self.param("btc_instrument", "BTC/USDT")):
                continue  # ширину меряем по АЛЬТАМ: биткоин тут не участник, а ориентир
            if len(asset.closes) < days or not asset.closes:
                continue
            last_day, last_close = asset.closes[-1]
            if (day - last_day) > timedelta(days=3):
                continue  # пара не торгуется — в знаменателе ей не место
            window = [c for _, c in list(asset.closes)[-days:]]
            alive += 1
            if last_close > sum(window, ZERO) / len(window):
                fresh += 1
        if alive < int(self.param("breadth_min_pairs", 20)):
            return None  # пар слишком мало, доля ничего не значит
        return Decimal(fresh) * 100 / alive

    def _breadth_allows(self, day: date) -> bool:
        """Ширина ниже порога — рынок альтов развернулся, моментуму здесь делать нечего."""
        floor = D(str(self.param("breadth_min_pct", 0)))
        if floor <= 0:
            return True  # фильтр выключен — поведение прежнее
        width = self.breadth_pct(day)
        return width is None or width >= floor

    def _btc_allows(self, day: date) -> bool:
        """Фильтр режима: на медвежьем рынке моментум альтов не работает, сидим в кэше."""
        days = int(self.param("btc_filter_sma_days", 0))
        if days <= 0:
            return True
        btc = self.assets.get(str(self.param("btc_instrument", "BTC/USDT")))
        if btc is None or len(btc.closes) < days:
            return False  # нечем проверить режим — не торгуем, а не «торгуем вслепую»
        window = [c for _, c in list(btc.closes)[-days:]]
        return btc.closes[-1][1] > sum(window, ZERO) / len(window)

    def _exit_all(self, bar: Candle, reason: str) -> list[Signal]:
        """Выйти из всего разом: фаза кончилась, держать нечего."""
        out: list[Signal] = []
        for name in sorted(self.held):
            asset = self.assets[name]
            if asset.qty > 0:
                out.append(self._sell(bar, name, asset, reason))
        return out

    def _rebalance(self, bar: Candle, day: date) -> list[Signal]:
        top_n = int(self.param("top_n", 5))
        target: list[str] = []
        if self._btc_allows(day) and self._breadth_allows(day):
            ranked = []
            for _, name in self._eligible(day):
                mom = self._momentum(self.assets[name], day)
                if mom is not None:
                    ranked.append((mom, name))
            ranked.sort(reverse=True)
            target = [name for _, name in ranked[:top_n]]

        out: list[Signal] = []
        for name in sorted(self.held - set(target)):
            asset = self.assets[name]
            if asset.qty > 0:
                out.append(self._sell(bar, name, asset, "выпал из топа"))
        capital = D(str(self.param("capital_usd", 10_000)))
        alloc = capital / top_n if top_n else ZERO
        for name in target:
            asset = self.assets[name]
            if asset.qty > 0 or asset.price <= 0:
                continue
            qty = alloc / asset.price
            if qty <= 0:
                continue
            asset.qty, asset.entry = qty, asset.price
            self.held.add(name)
            out.append(
                self._order(bar, name, "buy", qty, asset.price, {"kind": "xsmom_open"})
            )
        return out

    # -- сигналы --------------------------------------------------------------------

    def _sell(self, bar: Candle, name: str, asset: _Asset, reason: str) -> Signal:
        qty = asset.qty
        asset.qty, asset.entry = ZERO, ZERO
        self.held.discard(name)
        return self._order(
            bar, name, "sell", qty, asset.price, {"kind": "xsmom_close", "reason": reason}
        )

    def _order(
        self,
        bar: Candle,
        instrument: str,
        side: str,
        qty: Decimal,
        price_ref: Decimal,
        inputs: dict[str, object],
    ) -> Signal:
        """Решение принято по бару-триггеру, а инструмент и цена — свои у каждой ноги."""
        signal = self.signal(
            bar, side, qty, price_ref=price_ref, inputs={**inputs, "leg": instrument}
        )
        return signal.model_copy(update={"instrument": instrument})


__all__ = ["CrossSectionalMomentumStrategy"]
