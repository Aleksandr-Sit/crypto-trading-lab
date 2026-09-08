"""Стратегии, не зависящие от направления рынка.

- `cex-perp-api-docs-funding-arb-spot-hedge` — спот-лонг плюс шорт перпа: доход из фандинга.

Такие стратегии и нужны в боковике и падении, ради них делались портфельный замер (две ноги
одновременно) и история ставок фандинга (весь их доход — в ней). Бенчмарк у ветки `cex-perp`
теперь «кэш», а не биткоин: их альтернатива — не держать BTC, а не делать ничего.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
            pair.perp_price = bar.close
        else:
            pair.spot_price = bar.close
        if not pair.opened or pair.spot_price <= 0:
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


__all__ = ["FundingArbSpotHedgeStrategy", "annualized_pct", "base_of", "is_perp"]
