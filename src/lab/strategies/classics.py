"""Классические системы из книг — не пресеты ботов, а правила с опубликованным описанием.

- `cex-perp-book-turtle-donchian` — черепахи Денниса/Экхардта, System 2 (55/20).

Правила берутся из карточки `docs/research/strategies/<id>.md`, параметры карточки —
`params` манифеста. Общее допущение симулятора то же, что у пресетов: касание уровня
определяется по бару, исполнение — рыночно на открытии следующего (без заглядывания
в будущее), поэтому цена чуть хуже, чем у лимитки на уровне. Замер честнее, чем оптимистичнее.
"""

from __future__ import annotations

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


@preset(
    manifest_from_card(CARDS_DIR / "cex-perp-turtle-donchian.md", source_kind="book"),
)
class TurtleDonchianStrategy(Strategy):
    """Пробой канала Дончиана с добавлением по ходу движения и стопом в 2N.

    System 2 из оригинальных правил: вход на пробое 55-дневного экстремума, выход на пробое
    20-дневного в другую сторону, размер позиции — от волатильности (N = EMA истинного
    диапазона). Фильтра «прошлая сделка была прибыльной» здесь нет — это System 1.

    Чего в правилах намеренно нет: лимитов на коррелированные рынки и на общее число units
    в одну сторону. Они про ПОРТФЕЛЬ из нескольких инструментов, а замер ведёт один
    инструмент, и вписывать сюда portfolio-правила значило бы измерять несуществующее.
    """

    card = "cex-perp-turtle-donchian"

    def reset(self) -> None:
        self.history: list[Candle] = []
        self.prev_close: Decimal | None = None
        self.n_value: Decimal | None = None  # «N» — EMA истинного диапазона
        self.side: str | None = None
        self.qty = ZERO
        self.units = 0
        self.last_entry = ZERO
        self.stop = ZERO

    # -- волатильность ---------------------------------------------------------------

    def _update_n(self, bar: Candle) -> None:
        tr = true_range(bar, self.prev_close)
        period = int(self.param("atr_period", 20))
        if self.n_value is None:
            self.n_value = tr
        else:
            # EMA по Уайлдеру, как в оригинале: N = (19·N_прошлое + TR) / 20.
            self.n_value = (self.n_value * (period - 1) + tr) / period
        self.prev_close = bar.close

    def _unit_qty(self, bar: Candle) -> Decimal:
        """1 unit = risk_unit_pct капитала на движение в 1N, с потолком по плечу."""
        if not self.n_value or self.n_value <= 0:
            return ZERO
        capital = D(str(self.param("capital_usd", 10_000)))
        risk = D(str(self.param("risk_unit_pct", 1.0))) / 100
        qty = capital * risk / self.n_value
        leverage = D(str(self.param("leverage_cap", 2)))
        max_qty = capital * leverage / bar.close if bar.close > 0 else ZERO
        room = max_qty - self.qty
        return max(ZERO, min(qty, room))

    # -- решение ---------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> list[Signal]:
        out: list[Signal] = []
        entry_days = int(self.param("entry_days", 55))
        exit_days = int(self.param("exit_days", 20))
        keep = max(entry_days, exit_days)
        hist = self.history

        self._update_n(bar)
        if self.side is not None:
            out.extend(self._manage(bar, hist, exit_days))
        elif len(hist) >= entry_days:
            out.extend(self._enter(bar, hist, entry_days))

        hist.append(bar)
        del hist[:-keep]
        return out

    def _enter(self, bar: Candle, hist: list[Candle], entry_days: int) -> list[Signal]:
        high = max(b.high for b in hist[-entry_days:])
        low = min(b.low for b in hist[-entry_days:])
        qty = self._unit_qty(bar)
        if qty <= 0 or self.n_value is None:
            return []
        stop_mult = D(str(self.param("stop_atr_mult", 2.0)))
        if bar.close > high:
            self.side, self.qty, self.units = "long", qty, 1
            self.last_entry = bar.close
            self.stop = bar.close - stop_mult * self.n_value
            return [self.signal(bar, "buy", qty, inputs={"kind": "breakout", "level": high})]
        if bar.close < low:
            self.side, self.qty, self.units = "short", qty, 1
            self.last_entry = bar.close
            self.stop = bar.close + stop_mult * self.n_value
            return [self.signal(bar, "sell", qty, inputs={"kind": "breakdown", "level": low})]
        return []

    def _manage(self, bar: Candle, hist: list[Candle], exit_days: int) -> list[Signal]:
        """Порядок важен: сначала стоп, потом выход по каналу, и только потом добавление."""
        long = self.side == "long"
        stop_hit = bar.low <= self.stop if long else bar.high >= self.stop
        if stop_hit:
            return [self._close(bar, "stop")]
        if len(hist) >= exit_days:
            channel = (
                min(b.low for b in hist[-exit_days:])
                if long
                else max(b.high for b in hist[-exit_days:])
            )
            if (bar.close < channel) if long else (bar.close > channel):
                return [self._close(bar, "channel_exit", level=channel)]
        return self._pyramid(bar)

    def _pyramid(self, bar: Candle) -> list[Signal]:
        """Добавление по ½N в сторону прибыли; стоп подтягивается к последнему входу."""
        if self.n_value is None or self.n_value <= 0:
            return []
        if self.units >= int(self.param("max_units_per_market", 4)):
            return []
        step = D(str(self.param("pyramid_step_atr", 0.5))) * self.n_value
        long = self.side == "long"
        moved = (bar.close - self.last_entry) if long else (self.last_entry - bar.close)
        if moved < step:
            return []
        qty = self._unit_qty(bar)
        if qty <= 0:
            return []
        stop_mult = D(str(self.param("stop_atr_mult", 2.0)))
        self.qty += qty
        self.units += 1
        self.last_entry = bar.close
        self.stop = (
            bar.close - stop_mult * self.n_value if long else bar.close + stop_mult * self.n_value
        )
        return [
            self.signal(
                bar,
                "buy" if long else "sell",
                qty,
                inputs={"kind": "pyramid", "unit": self.units},
            )
        ]

    def _close(self, bar: Candle, kind: str, level: Decimal | None = None) -> Signal:
        side = "sell" if self.side == "long" else "buy"
        qty = self.qty
        inputs: dict[str, object] = {"kind": kind, "units": self.units}
        if level is not None:
            inputs["level"] = level
        self.side, self.qty, self.units = None, ZERO, 0
        self.stop = ZERO
        return self.signal(bar, side, qty, inputs=inputs)


__all__ = ["TurtleDonchianStrategy", "true_range"]
