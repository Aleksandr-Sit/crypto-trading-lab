"""Относительная стоимость: доход из РАСХОЖДЕНИЯ двух активов, а не из направления рынка.

- `cex-perp-paper-pairs-cointegration` — парный арбитраж по методу Gatev–Goetzmann–Rouwenhorst.

Зачем отдельно от `neutral`. Там собраны связки, которые собирают премию за плечо: фандинг
и базис квартального фьючерса. Замер 11.09.2026 показал, что это ОДНА ставка — связь между
ними +0.91, и обе пусты, когда на рынке мало плеча. Здесь премия другой природы: плата за
риск того, что связь между двумя активами порвётся насовсем. Она может быть богата тогда,
когда керри пуст, — ради этого правило и заводится.

Устройство, общее для всей относительной стоимости: раз в период торговли пары отбираются
заново на ОКНЕ ФОРМИРОВАНИЯ (только по данным до этого момента), внутри периода
открываются и закрываются по отклонению спреда. Состояние — по ПАРЕ, а не по стратегии.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

from lab.contracts import Candle, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)
HISTORY = 300  # окно формирования 252 дня плюс запас


def normalized(closes: list[Decimal]) -> list[Decimal]:
    """Накопленная доходность от начала окна: ряды разных цен становятся сравнимыми.

    BTC по 60 000 и DOGE по 0.2 нельзя вычитать напрямую — спред был бы про масштаб,
    а не про расхождение. После нормировки оба начинаются с единицы.
    """
    if not closes or closes[0] <= 0:
        return []
    base = closes[0]
    return [c / base for c in closes]


def spread_stats(a: list[Decimal], b: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    """Среднее, стандартное отклонение и сумма квадратов спреда двух нормированных рядов.

    Сумма квадратов — мера близости пары у Gatev: чем меньше, тем теснее ряды ходили
    вместе на окне формирования. Статистического теста на коинтеграцию здесь нет
    намеренно: на 252 наблюдениях он даёт много ложных срабатываний, а расстояние устойчивее.
    """
    n = min(len(a), len(b))
    if n < 2:
        return ZERO, ZERO, ZERO
    diffs = [a[i] - b[i] for i in range(n)]
    mean = sum(diffs, ZERO) / n
    var = sum(((d - mean) ** 2 for d in diffs), ZERO) / (n - 1)
    sigma = D(str(float(var) ** 0.5))
    sq = sum((d * d for d in diffs), ZERO)
    return mean, sigma, sq


@dataclass
class _Series:
    """Дневной ряд одного инструмента: закрытия с датами и оборот."""

    closes: deque[tuple[date, Decimal]] = field(default_factory=lambda: deque(maxlen=HISTORY))
    turnover: deque[Decimal] = field(default_factory=lambda: deque(maxlen=30))
    price: Decimal = ZERO

    def window(self, days: int) -> list[Decimal]:
        return [c for _, c in list(self.closes)[-days:]]


@dataclass
class _Pair:
    """Отобранная пара: чем нормируем, где среднее спреда и какая позиция открыта."""

    left: str
    right: str
    mean: Decimal
    sigma: Decimal
    base_left: Decimal  # цена левой ноги на начало окна — нормировка должна быть та же
    base_right: Decimal
    qty_left: Decimal = ZERO
    qty_right: Decimal = ZERO
    long_left: bool = True  # True: лонг левой, шорт правой
    opened: date | None = None


@preset(
    manifest_from_card(CARDS_DIR / "cex-perp-pairs-cointegration.md", source_kind="paper")
)
class PairsCointegrationStrategy(Strategy):
    """Купить отстающего, продать убежавшего, дождаться схождения.

    Доход — не предсказание рынка, а плата за риск того, что связь порвётся насовсем.
    Именно поэтому выход по `stop_sigma` обязателен: расхождение, которое продолжает
    расти, чаще всего означает, что один из активов умирает, а не что схождение близко.
    """

    card = "cex-perp-pairs-cointegration"

    def reset(self) -> None:
        self.series: dict[str, _Series] = {}
        self.pairs: list[_Pair] = []
        self.day: date | None = None
        self.since_formation = 0
        self.formed = False

    # -- данные ---------------------------------------------------------------------

    def _series(self, instrument: str) -> _Series:
        row = self.series.get(instrument)
        if row is None:
            row = self.series[instrument] = _Series()
        return row

    def on_bar(self, bar: Candle) -> list[Signal]:
        day = bar.ts.date()
        out: list[Signal] = []
        if self.day is None:
            self.day = day
        elif day != self.day:
            # Пришёл бар следующего дня — значит предыдущий закрыт по всем инструментам,
            # и только теперь срез рынка полон. То же требование, что у кросс-моментума.
            closed, self.day = self.day, day
            self.since_formation += 1
            if self.since_formation >= int(self.param("trading_days", 126)) or not self.formed:
                out += self._close_all(bar, "конец периода")
                self._form(closed)
                self.since_formation = 0
            else:
                out += self._trade(bar, closed)
        row = self._series(bar.instrument)
        row.price = bar.close
        row.closes.append((day, bar.close))
        row.turnover.append(bar.close * bar.volume)
        return out

    # -- формирование ---------------------------------------------------------------

    def _eligible(self) -> list[str]:
        """Инструменты с полной историей формирования и достаточным оборотом."""
        days = int(self.param("formation_days", 252))
        floor = D(str(self.param("min_daily_volume_usd", 0)))
        out = []
        for name, row in self.series.items():
            if len(row.closes) < days or row.price <= 0:
                continue
            if row.turnover and sum(row.turnover, ZERO) / len(row.turnover) < floor:
                continue
            out.append(name)
        return sorted(out)

    def _form(self, day: date) -> None:
        """Отбор пар по расстоянию на окне формирования — только по данным до `day`."""
        days = int(self.param("formation_days", 252))
        names = self._eligible()
        scored: list[tuple[Decimal, str, str]] = []
        norm = {n: normalized(self.series[n].window(days)) for n in names}
        for left, right in combinations(names, 2):
            a, b = norm[left], norm[right]
            if not a or not b:
                continue
            mean, sigma, sq = spread_stats(a, b)
            if sigma <= 0:
                continue
            scored.append((sq, left, right))
        scored.sort(key=lambda r: r[0])
        self.pairs = []
        taken: set[str] = set()
        for _sq, left, right in scored:
            # Один инструмент — в одной паре: иначе капитал утроится на одном ряду,
            # а «нейтральность» превратится в концентрированную ставку.
            if left in taken or right in taken:
                continue
            a, b = norm[left], norm[right]
            mean, sigma, _ = spread_stats(a, b)
            self.pairs.append(
                _Pair(
                    left=left,
                    right=right,
                    mean=mean,
                    sigma=sigma,
                    base_left=self.series[left].window(days)[0],
                    base_right=self.series[right].window(days)[0],
                )
            )
            taken.update((left, right))
            if len(self.pairs) >= int(self.param("top_pairs", 10)):
                break
        # Формирование считается выполненным, только если пары ДЕЙСТВИТЕЛЬНО отобраны.
        # Иначе первая же смена дня (истории ещё нет, вселенная пуста) помечала бы его
        # сделанным, и стратегия простаивала бы до конца периода торговли — сорок дней
        # в тесте, полгода в бою, и всё это молча.
        self.formed = bool(self.pairs)

    # -- торговля -------------------------------------------------------------------

    def _deviation(self, pair: _Pair) -> Decimal | None:
        """На сколько сигм спред ушёл от своего среднего ПРЯМО СЕЙЧАС."""
        left, right = self.series.get(pair.left), self.series.get(pair.right)
        if left is None or right is None or pair.sigma <= 0:
            return None
        if left.price <= 0 or right.price <= 0 or pair.base_left <= 0 or pair.base_right <= 0:
            return None
        spread = left.price / pair.base_left - right.price / pair.base_right
        return (spread - pair.mean) / pair.sigma

    def _notional(self) -> Decimal:
        capital = D(str(self.param("capital_usd", 10_000)))
        share = D(str(self.param("max_notional_pct_of_branch", 60))) / 100
        n = int(self.param("top_pairs", 10)) or 1
        return capital * share / n / 2  # на КАЖДУЮ ногу, их две

    def _trade(self, bar: Candle, day: date) -> list[Signal]:
        entry = D(str(self.param("entry_sigma", 2)))
        exit_at = D(str(self.param("exit_sigma", 0.5)))
        stop = D(str(self.param("stop_sigma", 4)))
        hold = timedelta(days=int(self.param("max_hold_days", 30)))
        out: list[Signal] = []
        for pair in self.pairs:
            dev = self._deviation(pair)
            if dev is None:
                continue
            if pair.qty_left > 0 or pair.qty_right > 0:
                assert pair.opened is not None
                if abs(dev) <= exit_at:
                    out += self._close(pair, bar, "схождение")
                elif abs(dev) >= stop:
                    out += self._close(pair, bar, "связь порвалась")
                elif day - pair.opened >= hold:
                    out += self._close(pair, bar, "время вышло")
                continue
            if abs(dev) >= entry:
                out += self._open(pair, bar, day, long_left=dev < 0)
        return out

    def _open(self, pair: _Pair, bar: Candle, day: date, *, long_left: bool) -> list[Signal]:
        """Лонг отстающего, шорт убежавшего. `dev < 0` значит левая нога отстала."""
        left, right = self.series[pair.left], self.series[pair.right]
        notional = self._notional()
        qty_left, qty_right = notional / left.price, notional / right.price
        if qty_left <= 0 or qty_right <= 0:
            return []
        pair.qty_left, pair.qty_right, pair.long_left, pair.opened = (
            qty_left,
            qty_right,
            long_left,
            day,
        )
        inputs = {"kind": "pairs_open", "pair": f"{pair.left}|{pair.right}"}
        return [
            self._leg(bar, pair.left, "buy" if long_left else "sell", qty_left, inputs),
            self._leg(bar, pair.right, "sell" if long_left else "buy", qty_right, inputs),
        ]

    def _close(self, pair: _Pair, bar: Candle, reason: str) -> list[Signal]:
        if pair.qty_left <= 0 and pair.qty_right <= 0:
            return []
        long_left = pair.long_left
        qty_left, qty_right = pair.qty_left, pair.qty_right
        pair.qty_left, pair.qty_right, pair.opened = ZERO, ZERO, None
        inputs = {"kind": "pairs_close", "reason": reason, "pair": f"{pair.left}|{pair.right}"}
        return [
            self._leg(bar, pair.left, "sell" if long_left else "buy", qty_left, inputs),
            self._leg(bar, pair.right, "buy" if long_left else "sell", qty_right, inputs),
        ]

    def _close_all(self, bar: Candle, reason: str) -> list[Signal]:
        out: list[Signal] = []
        for pair in self.pairs:
            out += self._close(pair, bar, reason)
        return out

    def _leg(
        self, bar: Candle, instrument: str, side: str, qty: Decimal, inputs: dict[str, object]
    ) -> Signal:
        """Обе ноги решаются одним баром: разъехавшись во времени, они перестали бы быть хеджем."""
        signal = self.signal(bar, side, qty, inputs={**inputs, "leg": instrument})
        return signal.model_copy(update={"instrument": instrument})


__all__ = ["PairsCointegrationStrategy", "normalized", "spread_stats"]
