"""Стратегии, не зависящие от направления рынка.

- `cex-perp-api-docs-funding-arb-spot-hedge` — спот-лонг плюс шорт перпа: доход из фандинга.
- `cex-perp-api-docs-basis-cash-carry` — спот-лонг плюс шорт КВАРТАЛЬНОГО фьючерса:
  доход из премии, которая обязана сойтись к нулю на расчёте.

Такие стратегии и нужны в боковике и падении, ради них делались портфельный замер (две ноги
одновременно) и история ставок фандинга (весь их доход — в ней). Бенчмарк у ветки `cex-perp`
теперь «кэш», а не биткоин: их альтернатива — не держать BTC, а не делать ничего.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle, Event, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)
YEAR_PAYOUTS = 3 * 365  # выплат в год при интервале 8 часов


def base_of(instrument: str) -> str:
    """`BTC/USDT:USDT` и `BTC/USDT` → `BTC`: по базовому активу и связываются ноги."""
    return instrument.split("/")[0]


def is_perp(instrument: str) -> bool:
    return ":" in instrument


def annualized_pct(rate: Decimal) -> Decimal:
    """Ставка за период → проценты годовых: три выплаты в сутки."""
    return rate * YEAR_PAYOUTS * 100


@dataclass
class _Pair:
    """Связка «спот + перп» одного актива."""

    spot: str = ""
    perp: str = ""
    spot_price: Decimal = ZERO
    perp_price: Decimal = ZERO
    # Время бара КАЖДОЙ ноги: базис имеет смысл только между ценами одного момента.
    spot_ts: datetime | None = None
    perp_ts: datetime | None = None
    qty: Decimal = ZERO
    payouts: int = 0  # сколько выплат прошло с открытия — раньше времени не выходим
    last_bar: Candle | None = None
    opened: bool = False
    legs_open: set[str] = field(default_factory=set)


@preset(manifest_from_card(CARDS_DIR / "cex-perp-funding-arb-spot-hedge.md", source_kind="api"))
class FundingArbSpotHedgeStrategy(Strategy):
    """Собирает фандинг: шорт перпа получает выплату, спот-лонг гасит движение цены.

    Решение принимается по СТАВКЕ, а не по свече, поэтому вход и выход считаются в
    `on_event(kind="funding")`: симулятор отдаёт туда реальные ставки из истории. Из свечей
    берутся только цены — для размера позиции и проверки базиса.

    Чего в правилах нет намеренно: изолированной маржи и ребаланса хеджа по дельте. Первое
    симулятор не моделирует вовсе, второе требует расчёта дельты между ногами на каждом баре
    и без модели маржи ничего не добавляет к честности замера.
    """

    card = "cex-perp-funding-arb-spot-hedge"

    def reset(self) -> None:
        self.pairs: dict[str, _Pair] = {}

    # -- связка ---------------------------------------------------------------------

    def _pair(self, instrument: str) -> _Pair:
        base = base_of(instrument)
        pair = self.pairs.get(base)
        if pair is None:
            pair = self.pairs[base] = _Pair()
        if is_perp(instrument):
            pair.perp = instrument
        else:
            pair.spot = instrument
        return pair

    def _notional(self) -> Decimal:
        """Номинал на одну связку: доля капитала, поделённая между активами манифеста."""
        capital = D(str(self.param("capital_usd", 10_000)))
        share = D(str(self.param("max_notional_pct_of_branch", 50))) / 100
        bases = {base_of(i) for i in self.manifest.instruments} or {"?"}
        return capital * share / len(bases)

    # -- данные ---------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> list[Signal]:
        pair = self._pair(bar.instrument)
        pair.last_bar = bar
        if is_perp(bar.instrument):
            pair.perp_price, pair.perp_ts = bar.close, bar.ts
        else:
            pair.spot_price, pair.spot_ts = bar.close, bar.ts
        if not pair.opened or pair.spot_price <= 0:
            return []
        # Ноги приходят РАЗНЫМИ барами: на баре спота цена перпа ещё с прошлого часа.
        # Их разность — не базис, а движение рынка за час, и на волатильном часе она
        # спокойно уходит за −1%. Так замер получил 156 «выходов по базису» из 156 сделок,
        # хотя настоящий базис BTC не отходил дальше −0.04%. Считаем по одному моменту.
        if pair.spot_ts != pair.perp_ts:
            return []
        # Базис ушёл в минус — перп дешевле спота, фандинг вот-вот развернётся против нас.
        limit = D(str(self.param("basis_exit_pct", -1))) / 100
        basis = (pair.perp_price - pair.spot_price) / pair.spot_price
        if basis < limit:
            return self._close(pair, bar, "basis")
        return []

    def on_event(self, event: Event) -> list[Signal]:
        if event.kind != "funding":
            return []
        instrument = str(event.payload.get("instrument", ""))
        if not is_perp(instrument):
            return []
        pair = self._pair(instrument)
        bar = pair.last_bar
        if bar is None or not pair.spot or not pair.perp:
            return []  # вторая нога ещё не появилась в потоке — открывать нечем

        rate = D(str(event.payload.get("rate", 0)))
        annual = annualized_pct(rate)
        if pair.opened:
            pair.payouts += 1
            held = pair.payouts >= int(self.param("min_hold_funding_intervals", 3))
            if held and annual < D(str(self.param("exit_funding_annualized_pct", 3))):
                return self._close(pair, bar, "funding_low")
            return []
        if annual > D(str(self.param("entry_funding_annualized_pct", 15))):
            return self._open(pair, bar, annual)
        return []

    # -- решения --------------------------------------------------------------------

    def _open(self, pair: _Pair, bar: Candle, annual: Decimal) -> list[Signal]:
        if pair.spot_price <= 0 or pair.perp_price <= 0:
            return []
        qty = self._notional() / pair.spot_price
        if qty <= 0:
            return []
        pair.qty = qty
        pair.opened = True
        pair.payouts = 0
        pair.legs_open = {pair.spot, pair.perp}
        inputs = {"kind": "funding_arb_open", "annualized_pct": str(annual)}
        return [
            self._leg(bar, pair.spot, "buy", qty, inputs),
            self._leg(bar, pair.perp, "sell", qty, inputs),
        ]

    def _close(self, pair: _Pair, bar: Candle, reason: str) -> list[Signal]:
        qty = pair.qty
        if qty <= 0:
            return []
        legs = sorted(pair.legs_open)
        pair.opened = False
        pair.qty = ZERO
        pair.payouts = 0
        pair.legs_open = set()
        inputs = {"kind": "funding_arb_close", "reason": reason}
        return [
            self._leg(bar, leg, "sell" if leg == pair.spot else "buy", qty, inputs) for leg in legs
        ]

    def _leg(
        self, bar: Candle, instrument: str, side: str, qty: Decimal, inputs: dict[str, object]
    ) -> Signal:
        """Сигнал по ноге: инструмент свой, а решение и время — общие для связки.

        Обе ноги решаются одним баром: разъехавшись во времени, они перестали бы быть хеджем.
        """
        signal = self.signal(bar, side, qty, inputs={**inputs, "leg": instrument})
        return signal.model_copy(update={"instrument": instrument})


EXPIRY_HOUR = 8  # квартальные контракты Binance рассчитываются в 08:00 UTC
YEAR_DAYS = D(365)


def expiry_of(instrument: str) -> datetime | None:
    """`BTC/USDT:USDT-260925` → 25.09.2026 08:00 UTC; у бессрочного и спота — None."""
    _, _, settle = instrument.partition(":")
    _, dash, tail = settle.partition("-")
    if not dash or len(tail) != 6 or not tail.isdigit():
        return None
    return datetime(
        2000 + int(tail[:2]), int(tail[2:4]), int(tail[4:]), EXPIRY_HOUR, tzinfo=UTC
    )


def basis_annualized_pct(spot: Decimal, futures: Decimal, days_left: Decimal) -> Decimal | None:
    """Премия фьючерса к споту, приведённая к году. Меньше суток до расчёта — не считаем.

    Годовые здесь не украшение: премия в 1% за две недели и та же премия за три месяца —
    совершенно разные сделки, а сравнивать их приходится одним порогом.
    """
    if spot <= 0 or days_left < 1:
        return None
    return (futures - spot) / spot * (YEAR_DAYS / days_left) * 100


@dataclass
class _Carry:
    """Связка «спот + один срочный контракт» одного актива."""

    spot: str = ""
    spot_price: Decimal = ZERO
    spot_ts: datetime | None = None
    # Цена и ВРЕМЯ БАРА каждого контракта: базис считается только между ценами одного
    # момента — на этом уже обожглись в фандинг-арбитраже (см. `_Pair.spot_ts`).
    futures: dict[str, tuple[Decimal, datetime]] = field(default_factory=dict)
    contract: str = ""  # какой контракт держим сейчас
    qty: Decimal = ZERO
    negative_since: datetime | None = None


@preset(manifest_from_card(CARDS_DIR / "cex-perp-basis-cash-carry.md", source_kind="api"))
class BasisCashCarryStrategy(Strategy):
    """Спот-лонг плюс шорт квартального фьючерса: премия обязана сойтись к нулю на расчёте.

    Отличие от фандинг-арбитража — в источнике дохода и в его определённости. Там платит
    фандинг: ставка меняется каждые восемь часов и может развернуться. Здесь доход задан
    заранее — премия контракта, потому что на расчёте фьючерс сходится к индексу по
    правилам биржи, а не по настроению рынка. Цена этой определённости — деньги заперты
    до экспирации.

    Допущение симулятора, которое важно знать: расчёта контракта движок не моделирует, и
    ряд просто обрывается на дате экспирации. Поэтому «держать до расчёта» реализовано как
    «закрыть обе ноги на последнем баре перед ним». Расхождение с настоящим расчётом мало
    (фьючерс к этому моменту уже сошёлся к индексу), но оно не в нашу пользу: закрытие
    рыночным ордером стоит комиссии, а расчёт на бирже бесплатен.

    Ролла между кварталами нет намеренно: карточка его не описывает, а придумывать
    правило, которого нет в источнике, — это уже другая стратегия.
    """

    card = "cex-perp-basis-cash-carry"

    def reset(self) -> None:
        self.carries: dict[str, _Carry] = {}

    # -- связка ---------------------------------------------------------------------

    def _carry(self, instrument: str) -> _Carry:
        base = base_of(instrument)
        carry = self.carries.get(base)
        if carry is None:
            carry = self.carries[base] = _Carry()
        if expiry_of(instrument) is None:
            carry.spot = instrument
        return carry

    def _notional(self) -> Decimal:
        capital = D(str(self.param("capital_usd", 10_000)))
        share = D(str(self.param("max_notional_pct_of_branch", 50))) / 100
        bases = {base_of(i) for i in self.manifest.instruments} or {"?"}
        return capital * share / len(bases)

    @staticmethod
    def _days_left(instrument: str, now: datetime) -> Decimal | None:
        expiry = expiry_of(instrument)
        if expiry is None:
            return None
        return D(str((expiry - now).total_seconds() / 86400))

    # -- данные ---------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> list[Signal]:
        carry = self._carry(bar.instrument)
        if expiry_of(bar.instrument) is None:
            carry.spot_price, carry.spot_ts = bar.close, bar.ts
        else:
            carry.futures[bar.instrument] = (bar.close, bar.ts)
        if carry.spot_price <= 0 or carry.spot_ts is None:
            return []
        if carry.contract:
            return self._maybe_close(carry, bar)
        return self._maybe_open(carry, bar)

    def _quote(self, carry: _Carry, contract: str) -> Decimal | None:
        """Цена контракта, если она из ТОГО ЖЕ бара, что и цена спота."""
        row = carry.futures.get(contract)
        if row is None:
            return None
        price, stamp = row
        return price if stamp == carry.spot_ts and price > 0 else None

    # -- решения --------------------------------------------------------------------

    def _maybe_open(self, carry: _Carry, bar: Candle) -> list[Signal]:
        entry = D(str(self.param("entry_basis_annualized_pct", 10)))
        min_days = D(str(self.param("min_days_to_expiry", 14)))
        best: tuple[Decimal, str, Decimal] | None = None
        for contract in carry.futures:
            days = self._days_left(contract, bar.ts)
            price = self._quote(carry, contract)
            if days is None or price is None or days < min_days:
                continue
            ann = basis_annualized_pct(carry.spot_price, price, days)
            # Из нескольких живых контрактов берём самый доходный в годовых: держать
            # капитал в дальнем квартале ради той же премии смысла нет.
            if ann is not None and ann > entry and (best is None or ann > best[0]):
                best = (ann, contract, price)
        if best is None:
            return []
        ann, contract, _price = best
        qty = self._notional() / carry.spot_price
        if qty <= 0:
            return []
        carry.contract, carry.qty, carry.negative_since = contract, qty, None
        inputs = {"kind": "carry_open", "annualized_pct": str(ann)}
        return [
            self._leg(bar, carry.spot, "buy", qty, inputs),
            self._leg(bar, contract, "sell", qty, inputs),
        ]

    def _maybe_close(self, carry: _Carry, bar: Candle) -> list[Signal]:
        contract = carry.contract
        days = self._days_left(contract, bar.ts)
        if days is None:
            return []
        # Расчёт контракта движок не моделирует — закрываем сами на последнем баре.
        if days <= D(str(self.param("close_before_expiry_days", 1))):
            return self._close(carry, bar, "экспирация")
        price = self._quote(carry, contract)
        if price is None:
            return []
        ann = basis_annualized_pct(carry.spot_price, price, days)
        if ann is None:
            return []
        if ann < 0:
            # Бэквордация: премии больше нет, а с ней и смысла держать капитал запертым.
            # Ждём подтверждения сутками, чтобы не выходить на одной случайной свече.
            if carry.negative_since is None:
                carry.negative_since = bar.ts
            hours = D(str(self.param("negative_basis_hours", 24)))
            if (bar.ts - carry.negative_since) >= timedelta(hours=float(hours)):
                return self._close(carry, bar, "бэквордация")
            return []
        carry.negative_since = None
        if ann < D(str(self.param("exit_basis_annualized_pct", 2))):
            return self._close(carry, bar, "премия выбрана")
        return []

    def _close(self, carry: _Carry, bar: Candle, reason: str) -> list[Signal]:
        qty, contract = carry.qty, carry.contract
        if qty <= 0 or not contract:
            return []
        carry.contract, carry.qty, carry.negative_since = "", ZERO, None
        inputs = {"kind": "carry_close", "reason": reason}
        return [
            self._leg(bar, carry.spot, "sell", qty, inputs),
            self._leg(bar, contract, "buy", qty, inputs),
        ]

    def _leg(
        self, bar: Candle, instrument: str, side: str, qty: Decimal, inputs: dict[str, object]
    ) -> Signal:
        signal = self.signal(bar, side, qty, inputs={**inputs, "leg": instrument})
        return signal.model_copy(update={"instrument": instrument})


__all__ = [
    "BasisCashCarryStrategy",
    "FundingArbSpotHedgeStrategy",
    "annualized_pct",
    "base_of",
    "basis_annualized_pct",
    "expiry_of",
    "is_perp",
]
