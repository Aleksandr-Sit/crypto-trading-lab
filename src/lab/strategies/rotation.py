"""Ротация между рисковым и защитным активом по относительному импульсу.

`cex-spot-external-rotation-gold-btc` — единственная внешняя идея, пережившая отбор
(`docs/research/sources-2026-09-14.md`). Правило Quantpedia: раз в неделю держать то,
что росло сильнее за последние `N` недель, и только если оно росло вообще; иначе кэш.

**Почему это стоит мерить после провала накопительной лестницы.** Лестница проиграла
докупке, потому что ПРОДАВАЛА В РОСТ. Ротация делает противоположное: выходит из актива,
когда его тренд сломался, и остаётся в нём, пока он ведёт. Одно и то же действие —
продажа — с разным знаком результата, и предварительная проверка это подтвердила:
61.5% годовых против 48.0% у BTC «купить и держать», на трёх разных стартах, с лучшей
просадкой и лучшим Sharpe.

**Сигнал составной, а не на одном окне.** Авторы объявили оптимум в 8 недель, и сетка
показала там пик: 4 нед — 56.7%, 8 нед — 73.9%, 12 нед — 38.5%. Пик, а не плато, —
признак отбора по известному исходу. Поэтому здесь голосуют несколько окон сразу:
составной сигнал заведомо хуже лучшего одиночного, зато не зависит от выбора параметра.

Два решения в коде, оба из граблей проекта:

* **решение принимается только при совпадении ДАТ обеих ног.** Ряды сливаются по времени,
  но бар приходит по одному, и на баре BTC цена золота — ещё вчерашняя. Их сравнение дало бы
  не относительный импульс, а движение рынка за сутки. Ровно так фандинг-арбитраж закрылся
  156 раз из 156 «по базису» при настоящем базисе в двадцать раз меньше порога;
* **исполняет решение каждая нога на СВОЁМ баре.** Сигнал принадлежит инструменту своего
  бара, и продавать BTC «на баре золота» нельзя. Поэтому решение хранится (`want`),
  а каждый инструмент приводит свою позицию к нему, когда до него доходит очередь.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from lab.contracts import Candle, Event, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)
CASH = "CASH"

# На сколько суток назад искать цену, если ровно на нужную дату её нет. У биржевых рядов
# бывают разрывы, а у классических инструментов — выходные; шесть суток закрывают и то,
# и другое, не давая при этом «заглянуть» слишком далеко.
LOOKBACK_SLACK_DAYS = 6


@dataclass
class _Leg:
    """Одна нога: история закрытий по датам, последняя дата и позиция."""

    closes: dict[date, Decimal] = field(default_factory=dict)
    last_day: date | None = None
    qty: Decimal = ZERO

    def at(self, day: date) -> Decimal | None:
        """Цена на дату или ближайшую ДО неё — но не позже."""
        for back in range(LOOKBACK_SLACK_DAYS + 1):
            price = self.closes.get(day - timedelta(days=back))
            if price is not None and price > 0:
                return price
        return None

    def prune(self, before: date) -> None:
        for day in [d for d in self.closes if d < before]:
            del self.closes[day]


@preset(manifest_from_card(CARDS_DIR / "cex-spot-rotation-gold-btc.md", source_kind="external"))
class DualMomentumRotation(Strategy):
    """Держать то, что ведёт; уйти в кэш, когда не ведёт никто."""

    def reset(self) -> None:
        self.legs: dict[str, _Leg] = {}
        self.want: str = CASH
        self.decided_on: date | None = None
        self.votes: dict[str, int] = {}

    # -- параметры ------------------------------------------------------------------

    @property
    def risk_instrument(self) -> str:
        return str(self.param("risk_instrument", "BTC/USDT"))

    @property
    def safe_instrument(self) -> str:
        return str(self.param("safe_instrument", "PAXG/USDT"))

    @property
    def windows(self) -> tuple[int, ...]:
        raw = self.param("windows_weeks", [4, 8, 12])
        if isinstance(raw, str):
            raw = [x for x in raw.split(",") if x.strip()]
        return tuple(int(x) for x in raw)

    @property
    def rebalance_days(self) -> int:
        return int(self.param("rebalance_days", 7))

    @property
    def capital_usd(self) -> Decimal:
        return D(str(self.param("capital_usd", 10_000)))

    # -- правило --------------------------------------------------------------------

    def _leg(self, instrument: str) -> _Leg:
        leg = self.legs.get(instrument)
        if leg is None:
            leg = self.legs[instrument] = _Leg()
        return leg

    def on_bar(self, bar: Candle) -> list[Signal]:
        if bar.close <= 0:
            return []
        day = bar.ts.date()
        leg = self._leg(bar.instrument)
        leg.closes[day] = bar.close
        leg.last_day = day
        leg.prune(day - timedelta(weeks=max(self.windows) + 2))

        self._maybe_decide(day)
        return self._reconcile(bar, leg)

    def _maybe_decide(self, day: date) -> None:
        """Пересмотреть решение, если прошла неделя И у обеих ног есть цена на эту дату.

        Требование одинаковой ДАТЫ — не формальность. Без него сравнивались бы цены
        разных суток, и «относительный импульс» превращался бы в шум дневного хода.
        """
        risk, safe = self._leg(self.risk_instrument), self._leg(self.safe_instrument)
        if risk.last_day != day or safe.last_day != day:
            return
        if self.decided_on is not None and (day - self.decided_on).days < self.rebalance_days:
            return
        vote = self._vote(risk, safe, day)
        if vote is None:
            return
        self.want, self.votes = vote
        self.decided_on = day

    def _vote(
        self, risk: _Leg, safe: _Leg, day: date
    ) -> tuple[str, dict[str, int]] | None:
        """Голосование окон. Окно без истории не голосует вовсе, а не голосует за кэш.

        Разница существенна на первых месяцах ряда: иначе короткая история сама по себе
        загоняла бы правило в кэш и создавала видимость «стратегия умеет пережидать».
        """
        now_risk, now_safe = risk.at(day), safe.at(day)
        if now_risk is None or now_safe is None:
            return None
        votes = {self.risk_instrument: 0, self.safe_instrument: 0, CASH: 0}
        counted = 0
        for weeks in self.windows:
            then = day - timedelta(weeks=weeks)
            was_risk, was_safe = risk.at(then), safe.at(then)
            if was_risk is None or was_safe is None:
                continue
            counted += 1
            r_risk = now_risk / was_risk - 1
            r_safe = now_safe / was_safe - 1
            if r_risk > r_safe and r_risk > 0:
                votes[self.risk_instrument] += 1
            elif r_safe > r_risk and r_safe > 0:
                votes[self.safe_instrument] += 1
            else:
                votes[CASH] += 1
        if not counted:
            return None
        return max(votes, key=lambda k: votes[k]), votes

    def _reconcile(self, bar: Candle, leg: _Leg) -> list[Signal]:
        """Привести позицию ЭТОГО инструмента к принятому решению."""
        if self.want == bar.instrument and leg.qty <= 0:
            qty = self.capital_usd / bar.close
            if qty <= 0:
                return []
            leg.qty = qty
            return [
                self.signal(
                    bar,
                    "buy",
                    qty,
                    price_ref=bar.close,
                    inputs={
                        "kind": "rotate_in",
                        "reason": "ведёт по импульсу",
                        "votes": dict(self.votes),
                    },
                )
            ]
        if self.want != bar.instrument and leg.qty > 0:
            qty, leg.qty = leg.qty, ZERO
            return [
                self.signal(
                    bar,
                    "sell",
                    qty,
                    price_ref=bar.close,
                    inputs={
                        "kind": "rotate_out",
                        "reason": f"импульс сломан, уходим в {self.want}",
                        "votes": dict(self.votes),
                    },
                )
            ]
        return []

    def on_event(self, event: Event) -> list[Signal]:
        return []


__all__ = ["CASH", "DualMomentumRotation"]
