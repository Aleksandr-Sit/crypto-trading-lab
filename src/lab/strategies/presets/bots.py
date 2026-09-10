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

**Состояние у каждого инструмента своё** (`_PerInstrument`). Бот из каталога настраивается
на ОДНУ пару: один DCA-бот — одна пара, один грид — одна пара. Запись реестра может нести
три пары, и тогда это три независимых бота с общим капиталом ветки, а не один бот, видящий
три ряда цен вперемешку. Разбор ошибки — в комментарии к `_PerInstrument`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
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


class _PerInstrument:
    """Состояние ПО ИНСТРУМЕНТАМ, а не одно на всю стратегию.

    Пока состояние было общим, портфельный замер получал ерунду, и молча: после бара
    BTC по 60 000 приходил бар ETH по 2467, DCA-бот считал это падением на 96% и докупал
    «страховочный ордер». За 30 суток — 43 199 покупок при ОДНОЙ закрытой сделке, полмиллиона
    лотов в памяти и SIGKILL от cgroup без единой строки вывода.

    Та же грабля уже была у черепах: там общая история сложила BTC по 60 тысяч с XRP
    по полдоллара, и канал Дончиана не пробивался никогда. Любая стратегия, которая может
    получить несколько рядов, обязана держать состояние по инструментам.
    """

    def reset(self) -> None:
        self.book: dict[str, Any] = {}

    def new_state(self) -> Any:  # pragma: no cover — переопределяется в каждом пресете
        raise NotImplementedError

    def st(self, instrument: str) -> Any:
        state = self.book.get(instrument)
        if state is None:
            state = self.book[instrument] = self.new_state()
        return state


def build_levels(lower: Decimal, upper: Decimal, grids: int, geometric: bool) -> list[Decimal]:
    """Уровни сетки от `lower` до `upper`. Общая мелочь двух гридов."""
    if upper <= lower or grids < 1:
        raise ValueError("сетка: upper > lower и grids >= 1")
    if geometric:
        ratio = (upper / lower) ** (D(1) / D(grids))
        return [lower * ratio**i for i in range(grids + 1)]
    step = (upper - lower) / D(grids)
    return [lower + step * i for i in range(grids + 1)]


def index_of(levels: list[Decimal], price: Decimal) -> int:
    """Номер верхнего уровня, не превышающего цену (−1 ниже сетки, grids — выше)."""
    idx = -1
    for i, lvl in enumerate(levels):
        if price >= lvl:
            idx = i
    return idx


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


@dataclass
class _GridState:
    """История для авто-диапазона — ОГРАНИЧЕННАЯ очередь.

    Была списком без предела: на минутках диапазон считается по 30 суткам, то есть 43 200
    свечей, и они оставались в памяти навсегда — под двести мегабайт на инструмент.
    """

    history: deque[Candle]
    levels: list[Decimal] = field(default_factory=list)
    last_index: int | None = None
    held: Decimal = ZERO
    stopped: bool = False
    qty_per_level: Decimal = ZERO


@preset(manifest_from_card(CARDS_DIR / "cex-spot-pionex-grid-arith.md"))
class SpotGridStrategy(_PerInstrument, Strategy):
    card = "cex-spot-pionex-grid-arith"

    def new_state(self) -> _GridState:
        return _GridState(history=deque(maxlen=self._lookback()))

    def _lookback(self) -> int:
        bars = self.param("range_lookback_bars")
        if bars:
            return int(bars)
        return int(D(30 * 24 * 3600) / D(self.step.total_seconds())) if self.step else 30

    def on_bar(self, bar: Candle) -> list[Signal]:
        st = self.st(bar.instrument)
        if st.stopped:
            return []
        if not st.levels:
            lower, upper = self.param("lower_price"), self.param("upper_price")
            if lower is None or upper is None:
                st.history.append(bar)
                if len(st.history) < self._lookback():
                    return []
                lower = min(b.low for b in st.history)
                upper = max(b.high for b in st.history)
            grids = int(self.param("grids", 50))
            st.levels = build_levels(
                D(str(lower)), D(str(upper)), grids, self.param("grid_type") == "geometric"
            )
            st.history.clear()  # диапазон посчитан, держать свечи больше незачем
            st.qty_per_level = D(str(self.param("investment_usd", 300))) / D(grids) / bar.close
            st.last_index = index_of(st.levels, bar.close)
            return []
        out: list[Signal] = []
        lower, upper = st.levels[0], st.levels[-1]
        stop = lower * (1 - _pct(self.param("stop_loss_price_pct_below_lower", 5)))
        take = upper * (1 + _pct(self.param("take_profit_price_pct_above_upper", 5)))
        if bar.close <= stop or bar.close >= take:
            st.stopped = True
            if st.held > 0:
                out.append(self.signal(bar, "sell", st.held, inputs={"reason": "range_exit"}))
                st.held = ZERO
            return out
        idx = index_of(st.levels, bar.close)
        assert st.last_index is not None
        if idx < st.last_index:  # цена опустилась на k уровней — k покупок
            k = st.last_index - idx
            qty = st.qty_per_level * k
            out.append(self.signal(bar, "buy", qty, inputs={"levels": k, "idx": idx}))
            st.held += qty
        elif idx > st.last_index and st.held > 0:  # поднялась — продаём по уровню на каждый
            k = (
                min(idx - st.last_index, int(st.held / st.qty_per_level))
                if st.qty_per_level
                else 0
            )
            if k > 0:
                qty = st.qty_per_level * k
                out.append(self.signal(bar, "sell", qty, inputs={"levels": k, "idx": idx}))
                st.held -= qty
        st.last_index = idx
        return out


# -- 2. DCA-бот 3Commas ----------------------------------------------------------------------


@dataclass
class _DcaState:
    deal: _Deal | None = None
    entry: Decimal = ZERO


@preset(manifest_from_card(CARDS_DIR / "cex-spot-3commas-dca-safety.md"))
class DcaSafetyStrategy(_PerInstrument, Strategy):
    card = "cex-spot-3commas-dca-safety"

    def new_state(self) -> _DcaState:
        return _DcaState()

    def _safety_price(self, entry: Decimal, k: int) -> Decimal:
        dev = _pct(self.param("price_deviation_pct", 1.5))
        scale = D(str(self.param("safety_step_scale", 1.2)))
        total = sum((scale**i for i in range(k)), ZERO)
        return entry * (1 - dev * total)

    def on_bar(self, bar: Candle) -> list[Signal]:
        st = self.st(bar.instrument)
        out: list[Signal] = []
        if st.deal is None:  # «open new trade immediately»
            qty = D(str(self.param("base_order_usd", 20))) / bar.close
            st.deal = _Deal()
            st.deal.buy(qty, bar.close)
            st.entry = bar.close
            out.append(self.signal(bar, "buy", qty, inputs={"kind": "base"}))
            return out
        d = st.deal
        tp = d.avg * (1 + _pct(self.param("take_profit_pct", 1.5)))
        sl_pct = D(str(self.param("stop_loss_pct", 0)))
        if bar.high >= tp:
            out.append(self.signal(bar, "sell", d.qty, inputs={"kind": "take_profit", "tp": tp}))
            st.deal = None
            return out
        if sl_pct > 0 and bar.close <= d.avg * (1 - sl_pct / 100):
            out.append(self.signal(bar, "sell", d.qty, inputs={"kind": "stop_loss"}))
            st.deal = None
            return out
        max_so = int(self.param("max_safety_orders", 5))
        if d.safety_filled < max_so:
            k = d.safety_filled + 1
            price = self._safety_price(st.entry, k)
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


@dataclass
class _NeutralGridState:
    history: deque[Candle]
    levels: list[Decimal] = field(default_factory=list)
    last_index: int | None = None
    position: Decimal = ZERO  # + лонг, − шорт (контракты)
    cost: Decimal = ZERO  # знаковая стоимость открытия
    qty_per_level: Decimal = ZERO
    stopped: bool = False


@preset(manifest_from_card(CARDS_DIR / "cex-perp-binance-futures-grid-neutral.md"))
class FuturesGridNeutralStrategy(_PerInstrument, Strategy):
    card = "cex-perp-binance-futures-grid-neutral"

    def new_state(self) -> _NeutralGridState:
        return _NeutralGridState(history=deque(maxlen=self._lookback()))

    def _lookback(self) -> int:
        return int(self.param("range_lookback_bars") or 14)

    @staticmethod
    def _atr(bars: deque[Candle]) -> Decimal:
        rows = list(bars)
        trs = []
        for prev, cur in zip(rows, rows[1:], strict=False):
            trs.append(
                max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
            )
        return sum(trs, ZERO) / D(len(trs)) if trs else ZERO

    @staticmethod
    def _pnl(st: _NeutralGridState, price: Decimal) -> Decimal:
        return st.position * price - st.cost

    def on_bar(self, bar: Candle) -> list[Signal]:
        st = self.st(bar.instrument)
        if st.stopped:
            return []
        if not st.levels:
            st.history.append(bar)
            if len(st.history) < self._lookback():
                return []
            atr = self._atr(st.history)
            if atr <= 0:
                return []
            k = D(str(self.param("range_atr_mult", 2.0)))
            grids = int(self.param("grids", 30))
            st.levels = build_levels(bar.close - k * atr, bar.close + k * atr, grids, False)
            st.history.clear()
            notional = D(str(self.param("margin_usd", 200))) * D(str(self.param("leverage", 3)))
            st.qty_per_level = notional / D(grids) / bar.close
            st.last_index = index_of(st.levels, bar.close)
            return []
        out: list[Signal] = []
        margin = D(str(self.param("margin_usd", 200)))
        if self._pnl(st, bar.close) <= -margin * _pct(self.param("stop_loss_pct", 8)):
            st.stopped = True
            if st.position != 0:
                side = "sell" if st.position > 0 else "buy"
                out.append(self.signal(bar, side, abs(st.position), inputs={"reason": "stop"}))
                st.position = ZERO
                st.cost = ZERO
            return out
        idx = index_of(st.levels, bar.close)
        assert st.last_index is not None
        if idx != st.last_index:
            k = abs(idx - st.last_index)
            qty = st.qty_per_level * k
            side = "buy" if idx < st.last_index else "sell"
            out.append(self.signal(bar, side, qty, inputs={"levels": k, "idx": idx}))
            signed = qty if side == "buy" else -qty
            st.position += signed
            st.cost += signed * bar.close
            st.last_index = idx
        return out


# -- 4. Мартингейл с лимитом докупок ------------------------------------------------------------


@dataclass
class _MartingaleState:
    deal: _Deal | None = None


@preset(manifest_from_card(CARDS_DIR / "cex-spot-martingale-capped.md"))
class MartingaleCappedStrategy(_PerInstrument, Strategy):
    card = "cex-spot-martingale-capped"

    def new_state(self) -> _MartingaleState:
        return _MartingaleState()

    def on_bar(self, bar: Candle) -> list[Signal]:
        st = self.st(bar.instrument)
        out: list[Signal] = []
        initial = D(str(self.param("initial_order_usd", 10)))
        if st.deal is None:
            qty = initial / bar.close
            st.deal = _Deal()
            st.deal.buy(qty, bar.close)
            out.append(self.signal(bar, "buy", qty, inputs={"kind": "initial"}))
            return out
        d = st.deal
        tp = d.avg * (1 + _pct(self.param("take_profit_pct", 1.5)))
        trail = _pct(self.param("trailing_take_profit_pct", 0.3))
        if bar.close <= d.avg * (1 - _pct(self.param("stop_loss_pct", 15))):
            out.append(self.signal(bar, "sell", d.qty, inputs={"kind": "stop_loss"}))
            st.deal = None
            return out
        if d.tp_armed or bar.close >= tp:
            d.tp_armed = True
            d.peak = max(d.peak, bar.close)
            if trail <= 0 or bar.close <= d.peak * (1 - trail):
                out.append(self.signal(bar, "sell", d.qty, inputs={"kind": "take_profit"}))
                st.deal = None
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


@dataclass
class _BreakoutState:
    history: deque[Candle]
    side: str | None = None  # long | short
    qty: Decimal = ZERO
    entry: Decimal = ZERO
    stop: Decimal = ZERO
    extreme: Decimal = ZERO


@preset(manifest_from_card(CARDS_DIR / "cex-perp-trailing-breakout-bot.md"))
class TrailingBreakoutStrategy(_PerInstrument, Strategy):
    card = "cex-perp-trailing-breakout-bot"

    def new_state(self) -> _BreakoutState:
        return _BreakoutState(history=deque(maxlen=int(self.param("breakout_lookback", 96))))

    def on_bar(self, bar: Candle) -> list[Signal]:
        st = self.st(bar.instrument)
        out: list[Signal] = []
        n = int(self.param("breakout_lookback", 96))
        if st.side is not None:
            trail = _pct(self.param("trailing_stop_pct", 1.5))
            activate = _pct(self.param("trailing_activate_profit_pct", 1.0))
            if st.side == "long":
                st.extreme = max(st.extreme, bar.high)
                if st.extreme >= st.entry * (1 + activate):
                    st.stop = max(st.stop, st.extreme * (1 - trail))
                hit = bar.close <= st.stop
            else:
                st.extreme = min(st.extreme, bar.low)
                if st.extreme <= st.entry * (1 - activate):
                    st.stop = min(st.stop, st.extreme * (1 + trail))
                hit = bar.close >= st.stop
            if hit:
                out.append(
                    self.signal(
                        bar,
                        "sell" if st.side == "long" else "buy",
                        st.qty,
                        inputs={"kind": "trailing_stop", "stop": st.stop},
                    )
                )
                st.side = None
            st.history.append(bar)
            return out
        if len(st.history) >= n:
            rows = list(st.history)
            hi = max(b.high for b in rows)
            lo = min(b.low for b in rows)
            direction = self.param("direction", "both")
            stop_pct = _pct(self.param("initial_stop_pct", 2.0))
            capital = D(str(self.param("capital_usd", 10_000)))
            risk = _pct(self.param("risk_per_trade_pct", 1.0))
            qty = capital * risk / stop_pct / bar.close
            if bar.close > hi and direction in ("long", "both"):
                st.side, st.entry, st.qty = "long", bar.close, qty
                st.stop, st.extreme = bar.close * (1 - stop_pct), bar.close
                out.append(self.signal(bar, "buy", qty, inputs={"kind": "breakout", "level": hi}))
            elif bar.close < lo and direction in ("short", "both"):
                st.side, st.entry, st.qty = "short", bar.close, qty
                st.stop, st.extreme = bar.close * (1 + stop_pct), bar.close
                out.append(self.signal(bar, "sell", qty, inputs={"kind": "breakdown", "level": lo}))
        st.history.append(bar)
        return out


__all__ = [
    "DcaSafetyStrategy",
    "FuturesGridNeutralStrategy",
    "MartingaleCappedStrategy",
    "SpotGridStrategy",
    "TrailingBreakoutStrategy",
    "build_levels",
    "index_of",
]
