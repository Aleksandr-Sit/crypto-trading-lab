"""Пресеты популярных ботов как стратегии-гипотезы (R07) — по карточкам тикета 13:

- `cex-spot-pionex-grid-arith`             — спот-грид Pionex (арифметическая сетка);
- `cex-spot-3commas-dca-safety`            — DCA-бот 3Commas со страховочными ордерами;
- `cex-perp-binance-futures-grid-neutral`  — нейтральный фьючерсный грид Binance;
- `cex-spot-martingale-capped`             — мартингейл Pionex с лимитом докупок и стопом;
- `cex-perp-trailing-breakout-bot`         — пробой с трейлинг-стопом (freqtrade trailing).

Правила — из карточек (`docs/research/strategies/<id>.md`), параметры карточек — `params`
манифеста. Одно допущение общее для всех: симулятор `core.measure` не умеет отменять лимитки,
поэтому касание уровня определяется по high/low бара, а исполнение — рыночно на открытии
следующего бара (без заглядывания в будущее). Для сетки это даёт чуть худшую цену, чем лимит
на уровне; замер честнее, чем оптимистичнее.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from lab.contracts import Candle, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)


def _pct(value: Any) -> Decimal:
    return D(str(value)) / 100


class _Deal:
    """Открытая позиция спота: средняя цена, количество, число докупок."""

    def __init__(self) -> None:
        self.qty = ZERO
        self.cost = ZERO
        self.safety_filled = 0
        self.last_buy = ZERO
        self.peak = ZERO
        self.tp_armed = False

    @property
    def avg(self) -> Decimal:
        return self.cost / self.qty if self.qty else ZERO

    def buy(self, qty: Decimal, price: Decimal) -> None:
        self.qty += qty
        self.cost += qty * price
        self.last_buy = price


# -- 1. Спот-грид Pionex ------------------------------------------------------------------------


class GridMixin:
    """Сетка уровней и переход цены между ними по закрытию бара."""

    levels: list[Decimal]
    last_index: int | None

    def _build_levels(self, lower: Decimal, upper: Decimal, grids: int, geometric: bool) -> None:
        if upper <= lower or grids < 1:
            raise ValueError("сетка: upper > lower и grids >= 1")
        if geometric:
            ratio = (upper / lower) ** (D(1) / D(grids))
            self.levels = [lower * ratio**i for i in range(grids + 1)]
        else:
            step = (upper - lower) / D(grids)
            self.levels = [lower + step * i for i in range(grids + 1)]
        self.last_index = None

    def _index(self, price: Decimal) -> int:
        """Номер верхнего уровня, не превышающего цену (−1 ниже сетки, grids — выше)."""
        idx = -1
        for i, lvl in enumerate(self.levels):
            if price >= lvl:
                idx = i
        return idx


@preset(manifest_from_card(CARDS_DIR / "cex-spot-pionex-grid-arith.md"))
class SpotGridStrategy(GridMixin, Strategy):
    card = "cex-spot-pionex-grid-arith"

    def reset(self) -> None:
        self.history: list[Candle] = []
        self.levels = []
        self.last_index = None
        self.held = ZERO  # базовая монета, купленная сеткой
        self.stopped = False
        self.qty_per_level = ZERO

    def _lookback(self) -> int:
        bars = self.param("range_lookback_bars")
        if bars:
            return int(bars)
        return int(D(30 * 24 * 3600) / D(self.step.total_seconds())) if self.step else 30

    def on_bar(self, bar: Candle) -> list[Signal]:
        if self.stopped:
            return []
        if not self.levels:
            lower, upper = self.param("lower_price"), self.param("upper_price")
            if lower is None or upper is None:
                self.history.append(bar)
                if len(self.history) < self._lookback():
                    return []
                lower = min(b.low for b in self.history)
                upper = max(b.high for b in self.history)
            grids = int(self.param("grids", 50))
            self._build_levels(
                D(str(lower)), D(str(upper)), grids, self.param("grid_type") == "geometric"
            )
            self.qty_per_level = D(str(self.param("investment_usd", 300))) / D(grids) / bar.close
            self.last_index = self._index(bar.close)
            return []
        out: list[Signal] = []
        lower, upper = self.levels[0], self.levels[-1]
        stop = lower * (1 - _pct(self.param("stop_loss_price_pct_below_lower", 5)))
        take = upper * (1 + _pct(self.param("take_profit_price_pct_above_upper", 5)))
        if bar.close <= stop or bar.close >= take:
            self.stopped = True
            if self.held > 0:
                out.append(self.signal(bar, "sell", self.held, inputs={"reason": "range_exit"}))
                self.held = ZERO
            return out
        idx = self._index(bar.close)
        assert self.last_index is not None
        if idx < self.last_index:  # цена опустилась на k уровней — k покупок
            k = self.last_index - idx
            qty = self.qty_per_level * k
            out.append(self.signal(bar, "buy", qty, inputs={"levels": k, "idx": idx}))
            self.held += qty
        elif idx > self.last_index and self.held > 0:  # поднялась — продаём по уровню на каждый
            k = (
                min(idx - self.last_index, int(self.held / self.qty_per_level))
                if self.qty_per_level
                else 0
            )
            if k > 0:
                qty = self.qty_per_level * k
                out.append(self.signal(bar, "sell", qty, inputs={"levels": k, "idx": idx}))
                self.held -= qty
        self.last_index = idx
        return out


# -- 2. DCA-бот 3Commas ----------------------------------------------------------------------


@preset(manifest_from_card(CARDS_DIR / "cex-spot-3commas-dca-safety.md"))
class DcaSafetyStrategy(Strategy):
    card = "cex-spot-3commas-dca-safety"

    def reset(self) -> None:
        self.deal: _Deal | None = None
        self.entry = ZERO

    def _safety_price(self, k: int) -> Decimal:
        dev = _pct(self.param("price_deviation_pct", 1.5))
        scale = D(str(self.param("safety_step_scale", 1.2)))
        total = sum((scale**i for i in range(k)), ZERO)
        return self.entry * (1 - dev * total)

    def on_bar(self, bar: Candle) -> list[Signal]:
        out: list[Signal] = []
        if self.deal is None:  # «open new trade immediately»
            qty = D(str(self.param("base_order_usd", 20))) / bar.close
            self.deal = _Deal()
            self.deal.buy(qty, bar.close)
            self.entry = bar.close
            out.append(self.signal(bar, "buy", qty, inputs={"kind": "base"}))
            return out
        d = self.deal
        tp = d.avg * (1 + _pct(self.param("take_profit_pct", 1.5)))
        sl_pct = D(str(self.param("stop_loss_pct", 0)))
        if bar.high >= tp:
            out.append(self.signal(bar, "sell", d.qty, inputs={"kind": "take_profit", "tp": tp}))
            self.deal = None
            return out
        if sl_pct > 0 and bar.close <= d.avg * (1 - sl_pct / 100):
            out.append(self.signal(bar, "sell", d.qty, inputs={"kind": "stop_loss"}))
            self.deal = None
            return out
        max_so = int(self.param("max_safety_orders", 5))
        if d.safety_filled < max_so:
            k = d.safety_filled + 1
            price = self._safety_price(k)
            if bar.low <= price:
                usd = D(str(self.param("safety_order_usd", 20))) * D(
                    str(self.param("safety_volume_scale", 1.5))
                ) ** (k - 1)
                qty = usd / bar.close
                d.buy(qty, bar.close)
                d.safety_filled = k
                out.append(self.signal(bar, "buy", qty, inputs={"kind": "safety", "k": k}))
        return out


# -- 3. Нейтральный фьючерсный грид Binance -----------------------------------------------------


@preset(manifest_from_card(CARDS_DIR / "cex-perp-binance-futures-grid-neutral.md"))
class FuturesGridNeutralStrategy(GridMixin, Strategy):
    card = "cex-perp-binance-futures-grid-neutral"

    def reset(self) -> None:
        self.history: list[Candle] = []
        self.levels = []
        self.last_index = None
        self.position = ZERO  # + лонг, − шорт (контракты)
        self.cost = ZERO  # знаковая стоимость открытия
        self.qty_per_level = ZERO
        self.stopped = False

    def _lookback(self) -> int:
        return int(self.param("range_lookback_bars") or 14)

    def _atr(self) -> Decimal:
        bars = self.history
        trs = []
        for prev, cur in zip(bars, bars[1:], strict=False):
            trs.append(
                max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
            )
        return sum(trs, ZERO) / D(len(trs)) if trs else ZERO

    def _pnl(self, price: Decimal) -> Decimal:
        return self.position * price - self.cost

    def on_bar(self, bar: Candle) -> list[Signal]:
        if self.stopped:
            return []
        if not self.levels:
            self.history.append(bar)
            if len(self.history) < self._lookback():
                return []
            atr = self._atr()
            if atr <= 0:
                return []
            k = D(str(self.param("range_atr_mult", 2.0)))
            grids = int(self.param("grids", 30))
            self._build_levels(bar.close - k * atr, bar.close + k * atr, grids, False)
            notional = D(str(self.param("margin_usd", 200))) * D(str(self.param("leverage", 3)))
            self.qty_per_level = notional / D(grids) / bar.close
            self.last_index = self._index(bar.close)
            return []
        out: list[Signal] = []
        margin = D(str(self.param("margin_usd", 200)))
        if self._pnl(bar.close) <= -margin * _pct(self.param("stop_loss_pct", 8)):
            self.stopped = True
            if self.position != 0:
                side = "sell" if self.position > 0 else "buy"
                out.append(self.signal(bar, side, abs(self.position), inputs={"reason": "stop"}))
                self.position = ZERO
                self.cost = ZERO
            return out
        idx = self._index(bar.close)
        assert self.last_index is not None
        if idx != self.last_index:
            k = abs(idx - self.last_index)
            qty = self.qty_per_level * k
            side = "buy" if idx < self.last_index else "sell"
            out.append(self.signal(bar, side, qty, inputs={"levels": k, "idx": idx}))
            signed = qty if side == "buy" else -qty
            self.position += signed
            self.cost += signed * bar.close
            self.last_index = idx
        return out


# -- 4. Мартингейл с лимитом докупок ------------------------------------------------------------


@preset(manifest_from_card(CARDS_DIR / "cex-spot-martingale-capped.md"))
class MartingaleCappedStrategy(Strategy):
    card = "cex-spot-martingale-capped"

    def reset(self) -> None:
        self.deal: _Deal | None = None

    def on_bar(self, bar: Candle) -> list[Signal]:
        out: list[Signal] = []
        initial = D(str(self.param("initial_order_usd", 10)))
        if self.deal is None:
            qty = initial / bar.close
            self.deal = _Deal()
            self.deal.buy(qty, bar.close)
            out.append(self.signal(bar, "buy", qty, inputs={"kind": "initial"}))
            return out
        d = self.deal
        tp = d.avg * (1 + _pct(self.param("take_profit_pct", 1.5)))
        trail = _pct(self.param("trailing_take_profit_pct", 0.3))
        if bar.close <= d.avg * (1 - _pct(self.param("stop_loss_pct", 15))):
            out.append(self.signal(bar, "sell", d.qty, inputs={"kind": "stop_loss"}))
            self.deal = None
            return out
        if d.tp_armed or bar.close >= tp:
            d.tp_armed = True
            d.peak = max(d.peak, bar.close)
            if trail <= 0 or bar.close <= d.peak * (1 - trail):
                out.append(self.signal(bar, "sell", d.qty, inputs={"kind": "take_profit"}))
                self.deal = None
            return out
        max_so = int(self.param("max_safety_orders", 5))
        if d.safety_filled < max_so:
            k = d.safety_filled + 1
            drop = _pct(self.param("price_drop_pct", 2.0)) * D(
                str(self.param("drop_scale", 1.0))
            ) ** (k - 1)
            if bar.close <= d.last_buy * (1 - drop):
                usd = initial * D(str(self.param("volume_multiplier", 2.0))) ** k
                qty = usd / bar.close
                d.buy(qty, bar.close)
                d.safety_filled = k
                out.append(self.signal(bar, "buy", qty, inputs={"kind": "safety", "k": k}))
        return out


# -- 5. Пробой с трейлинг-стопом ----------------------------------------------------------------


@preset(manifest_from_card(CARDS_DIR / "cex-perp-trailing-breakout-bot.md"))
class TrailingBreakoutStrategy(Strategy):
    card = "cex-perp-trailing-breakout-bot"

    def reset(self) -> None:
        self.history: list[Candle] = []
        self.side: str | None = None  # long | short
        self.qty = ZERO
        self.entry = ZERO
        self.stop = ZERO
        self.extreme = ZERO

    def on_bar(self, bar: Candle) -> list[Signal]:
        out: list[Signal] = []
        n = int(self.param("breakout_lookback", 96))
        hist = self.history
        if self.side is not None:
            trail = _pct(self.param("trailing_stop_pct", 1.5))
            activate = _pct(self.param("trailing_activate_profit_pct", 1.0))
            if self.side == "long":
                self.extreme = max(self.extreme, bar.high)
                if self.extreme >= self.entry * (1 + activate):
                    self.stop = max(self.stop, self.extreme * (1 - trail))
                hit = bar.close <= self.stop
            else:
                self.extreme = min(self.extreme, bar.low)
                if self.extreme <= self.entry * (1 - activate):
                    self.stop = min(self.stop, self.extreme * (1 + trail))
                hit = bar.close >= self.stop
            if hit:
                out.append(
                    self.signal(
                        bar,
                        "sell" if self.side == "long" else "buy",
                        self.qty,
                        inputs={"kind": "trailing_stop", "stop": self.stop},
                    )
                )
                self.side = None
            hist.append(bar)
            del hist[:-n]
            return out
        if len(hist) >= n:
            hi = max(b.high for b in hist[-n:])
            lo = min(b.low for b in hist[-n:])
            direction = self.param("direction", "both")
            stop_pct = _pct(self.param("initial_stop_pct", 2.0))
            capital = D(str(self.param("capital_usd", 10_000)))
            risk = _pct(self.param("risk_per_trade_pct", 1.0))
            qty = capital * risk / stop_pct / bar.close
            if bar.close > hi and direction in ("long", "both"):
                self.side, self.entry, self.qty = "long", bar.close, qty
                self.stop, self.extreme = bar.close * (1 - stop_pct), bar.close
                out.append(self.signal(bar, "buy", qty, inputs={"kind": "breakout", "level": hi}))
            elif bar.close < lo and direction in ("short", "both"):
                self.side, self.entry, self.qty = "short", bar.close, qty
                self.stop, self.extreme = bar.close * (1 + stop_pct), bar.close
                out.append(self.signal(bar, "sell", qty, inputs={"kind": "breakdown", "level": lo}))
        hist.append(bar)
        del hist[:-n]
        return out


__all__ = [
    "DcaSafetyStrategy",
    "FuturesGridNeutralStrategy",
    "MartingaleCappedStrategy",
    "SpotGridStrategy",
    "TrailingBreakoutStrategy",
]
