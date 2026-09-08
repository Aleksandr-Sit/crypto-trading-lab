"""Классические системы из книг — не пресеты ботов, а правила с опубликованным описанием.

- `cex-perp-book-turtle-donchian` — черепахи Денниса/Экхардта, System 2 (55/20).

Правила берутся из карточки `docs/research/strategies/<id>.md`, параметры карточки —
`params` манифеста. Общее допущение симулятора то же, что у пресетов: касание уровня
определяется по бару, исполнение — рыночно на открытии следующего (без заглядывания
в будущее), поэтому цена чуть хуже, чем у лимитки на уровне. Замер честнее, чем оптимистичнее.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from lab.contracts import Candle, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)


def true_range(bar: Candle, prev_close: Decimal | None) -> Decimal:
    """TR: размах бара, а при разрыве — расстояние до вчерашнего закрытия."""
    if prev_close is None:
        return bar.high - bar.low
    return max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))


@dataclass
class _Market:
    """Состояние ОДНОГО инструмента: у черепах каждый рынок живёт своей жизнью.

    Общее состояние на портфель было бы ошибкой, и не теоретической: пока история баров
    была общей, свечи BTC по 60 тысяч и XRP по полдоллара складывались в один ряд, канал
    Дончиана считался по этой каше и не пробивался никогда — портфельный замер дал ноль
    сделок там, где на одном инструменте их было 91.
    """

    history: list[Candle] = field(default_factory=list)
    prev_close: Decimal | None = None
    n_value: Decimal | None = None  # «N» — EMA истинного диапазона
    side: str | None = None
    qty: Decimal = ZERO
    units: int = 0
    last_entry: Decimal = ZERO
    stop: Decimal = ZERO


@preset(
    manifest_from_card(CARDS_DIR / "cex-perp-turtle-donchian.md", source_kind="book"),
)
class TurtleDonchianStrategy(Strategy):
    """Пробой канала Дончиана с добавлением по ходу движения и стопом в 2N.

    System 2 из оригинальных правил: вход на пробое 55-дневного экстремума, выход на пробое
    20-дневного в другую сторону, размер позиции — от волатильности (N = EMA истинного
    диапазона). Фильтра «прошлая сделка была прибыльной» здесь нет — это System 1.

    Портфельная по замыслу: каждый рынок торгуется независимо, а ограничения общие —
    потолок units в одну сторону и потолок плеча по всей занятой позиции.
    """

    card = "cex-perp-turtle-donchian"

    def reset(self) -> None:
        self.markets: dict[str, _Market] = {}

    def _market(self, instrument: str) -> _Market:
        market = self.markets.get(instrument)
        if market is None:
            market = self.markets[instrument] = _Market()
        return market

    # -- волатильность ---------------------------------------------------------------

    def _update_n(self, market: _Market, bar: Candle) -> None:
        tr = true_range(bar, market.prev_close)
        period = int(self.param("atr_period", 20))
        # EMA по Уайлдеру, как в оригинале: N = (19·N_прошлое + TR) / 20.
        market.n_value = (
            tr if market.n_value is None else (market.n_value * (period - 1) + tr) / period
        )
        market.prev_close = bar.close

    def _open_units(self) -> int:
        return sum(m.units for m in self.markets.values())

    def _exposure(self, price: Decimal, instrument: str) -> Decimal:
        """Занятый номинал по всем рынкам: свой — по текущей цене, чужие — по цене входа."""
        total = ZERO
        for name, market in self.markets.items():
            if market.qty <= 0:
                continue
            total += market.qty * (price if name == instrument else market.last_entry)
        return total

    def _unit_qty(self, market: _Market, bar: Candle) -> Decimal:
        """1 unit = risk_unit_pct капитала на движение в 1N, с ОБЩИМ потолком плеча."""
        if not market.n_value or market.n_value <= 0 or bar.close <= 0:
            return ZERO
        capital = D(str(self.param("capital_usd", 10_000)))
        risk = D(str(self.param("risk_unit_pct", 1.0))) / 100
        qty = capital * risk / market.n_value
        leverage = D(str(self.param("leverage_cap", 2)))
        room_usd = capital * leverage - self._exposure(bar.close, bar.instrument)
        return max(ZERO, min(qty, room_usd / bar.close))

    # -- решение ---------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> list[Signal]:
        market = self._market(bar.instrument)
        entry_days = int(self.param("entry_days", 55))
        exit_days = int(self.param("exit_days", 20))
        out: list[Signal] = []

        self._update_n(market, bar)
        if market.side is not None:
            out.extend(self._manage(market, bar, exit_days))
        elif len(market.history) >= entry_days:
            out.extend(self._enter(market, bar, entry_days))

        market.history.append(bar)
        del market.history[: -max(entry_days, exit_days)]
        return out

    def _enter(self, market: _Market, bar: Candle, entry_days: int) -> list[Signal]:
        if self._open_units() >= int(self.param("max_units_total", 12)):
            return []
        window = market.history[-entry_days:]
        high = max(b.high for b in window)
        low = min(b.low for b in window)
        qty = self._unit_qty(market, bar)
        if qty <= 0 or market.n_value is None:
            return []
        stop_mult = D(str(self.param("stop_atr_mult", 2.0)))
        if bar.close > high:
            market.side, market.qty, market.units = "long", qty, 1
            market.last_entry = bar.close
            market.stop = bar.close - stop_mult * market.n_value
            return [self.signal(bar, "buy", qty, inputs={"kind": "breakout", "level": high})]
        if bar.close < low:
            market.side, market.qty, market.units = "short", qty, 1
            market.last_entry = bar.close
            market.stop = bar.close + stop_mult * market.n_value
            return [self.signal(bar, "sell", qty, inputs={"kind": "breakdown", "level": low})]
        return []

    def _manage(self, market: _Market, bar: Candle, exit_days: int) -> list[Signal]:
        """Порядок важен: сначала стоп, потом выход по каналу, и только потом добавление."""
        long = market.side == "long"
        if (bar.low <= market.stop) if long else (bar.high >= market.stop):
            return [self._close(market, bar, "stop")]
        if len(market.history) >= exit_days:
            window = market.history[-exit_days:]
            channel = min(b.low for b in window) if long else max(b.high for b in window)
            if (bar.close < channel) if long else (bar.close > channel):
                return [self._close(market, bar, "channel_exit", level=channel)]
        return self._pyramid(market, bar)

    def _pyramid(self, market: _Market, bar: Candle) -> list[Signal]:
        """Добавление по ½N в сторону прибыли; стоп подтягивается к последнему входу."""
        if market.n_value is None or market.n_value <= 0:
            return []
        if market.units >= int(self.param("max_units_per_market", 4)):
            return []
        if self._open_units() >= int(self.param("max_units_total", 12)):
            return []
        long = market.side == "long"
        step = D(str(self.param("pyramid_step_atr", 0.5))) * market.n_value
        moved = (bar.close - market.last_entry) if long else (market.last_entry - bar.close)
        if moved < step:
            return []
        qty = self._unit_qty(market, bar)
        if qty <= 0:
            return []
        stop_mult = D(str(self.param("stop_atr_mult", 2.0)))
        market.qty += qty
        market.units += 1
        market.last_entry = bar.close
        market.stop = (
            bar.close - stop_mult * market.n_value
            if long
            else bar.close + stop_mult * market.n_value
        )
        return [
            self.signal(
                bar,
                "buy" if long else "sell",
                qty,
                inputs={"kind": "pyramid", "unit": market.units},
            )
        ]

    def _close(
        self, market: _Market, bar: Candle, kind: str, level: Decimal | None = None
    ) -> Signal:
        side = "sell" if market.side == "long" else "buy"
        qty = market.qty
        inputs: dict[str, object] = {"kind": kind, "units": market.units}
        if level is not None:
            inputs["level"] = level
        market.side, market.qty, market.units, market.stop = None, ZERO, 0, ZERO
        return self.signal(bar, side, qty, inputs=inputs)


__all__ = ["TurtleDonchianStrategy", "true_range"]
