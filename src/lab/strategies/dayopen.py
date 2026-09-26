"""Гашение раннего движения суток UTC (п.5 плана трейдеров, 26.09.2026).

- `cex-perp-paper-day-open-fade` — вход против хода первого часа суток, выход в полночь.

Единственный эффект лаборатории, прошедший оба порога ПРЯМОЙ проверки
(`docs/research/hold-horizon-2026-09-21.md`): +0.244% при шуме ±0.143, знак в 6 годах
из 7. Прямой расчёт не видел фандинга и не вёл капитал по дням — это и меряет движок.

Правило живёт на ГРАНИЦАХ суток, поэтому всё, что здесь есть, — про время, а не про цену:
решение ровно на закрытии первого часа, выход ровно на границе следующих суток, сутки без
первого бара не торгуются.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle, Event, Signal
from lab.strategies.base import Strategy
from lab.strategies.presets.cards import CARDS_DIR
from lab.strategies.registry import manifest_from_card, preset

D = Decimal
ZERO = D(0)


@dataclass
class _Leg:
    """Состояние ОДНОГО инструмента: открытие текущих суток и то, что держим."""

    day_start: datetime | None = None  # начало суток, чей ПЕРВЫЙ бар мы видели
    day_open: Decimal = ZERO
    side: str = ""  # "long" | "short" | "" — сторона открытой позиции
    qty: Decimal = ZERO
    exit_at: datetime | None = None  # граница суток, на которой позиция закрывается


@preset(manifest_from_card(CARDS_DIR / "cex-perp-day-open-fade.md", source_kind="paper"))
class DayOpenFadeStrategy(Strategy):
    """Против хода первого часа суток UTC, удержание до следующей полуночи.

    Три решения в правилах, каждое из замера:

    * **окно — час, а не 15 минут.** Кандидат был найден через пробой и назван по длине
      диапазона; прямая сетка показала горб с вершиной на часе, на 15 минутах эффекта нет;
    * **удержание до конца суток.** Эффект пропорционален времени, круг издержек — нет:
      час даёт 0.04–0.07% при круге 0.10%, порог издержек берётся к двенадцати часам;
    * **выход на границе суток, а не через 23 часа от входа.** За пределами суток эффект
      не продолжается — он принадлежит дневной свече, а не времени удержания.
    """

    def reset(self) -> None:
        self.legs: dict[str, _Leg] = {}

    # -- параметры ------------------------------------------------------------------

    @property
    def day_hour(self) -> int:
        return int(self.param("day_start_hour_utc", 0))

    @property
    def lead(self) -> timedelta:
        return timedelta(minutes=int(self.param("lead_minutes", 60)))

    @property
    def min_move_pct(self) -> Decimal:
        return D(str(self.param("min_move_pct", 0)))

    def _notional(self) -> Decimal:
        # `capital_usd` — как у соседних стратегий; замер кладёт свой капитал в `capital`.
        capital = D(str(self.param("capital_usd", self.param("capital", 10_000))))
        gross = D(str(self.param("gross_pct", 25))) / 100
        n = len(self.manifest.instruments) or 1
        return capital * gross / n

    def _day_start(self, ts: datetime) -> datetime:
        ts = ts.astimezone(UTC)
        start = ts.replace(hour=self.day_hour, minute=0, second=0, microsecond=0)
        return start if start <= ts else start - timedelta(days=1)

    # -- правило --------------------------------------------------------------------

    def on_bar(self, bar: Candle) -> list[Signal]:
        leg = self.legs.get(bar.instrument)
        if leg is None:
            leg = self.legs[bar.instrument] = _Leg()
        end = bar.ts.astimezone(UTC) + self.step
        out: list[Signal] = []

        # Выход раньше входа: позиция вчерашних суток закрывается на их границе, и только
        # потом бар может начать новые сутки. Дыра в ряду не должна удерживать позицию
        # сутками — поэтому «>=», а не «==».
        if leg.qty > 0 and leg.exit_at is not None and end >= leg.exit_at:
            out.append(self._close(bar, leg, "day_end"))

        start = self._day_start(bar.ts)
        if bar.ts.astimezone(UTC) == start:
            leg.day_start, leg.day_open = start, bar.open

        if leg.qty == 0 and leg.day_start == start and end == start + self.lead:
            opened = self._maybe_open(bar, leg, start)
            if opened is not None:
                out.append(opened)
        return out

    def _maybe_open(self, bar: Candle, leg: _Leg, start: datetime) -> Signal | None:
        if leg.day_open <= 0 or bar.close <= 0:
            return None
        move_pct = (bar.close / leg.day_open - 1) * 100
        if move_pct == 0 or abs(move_pct) < self.min_move_pct:
            return None
        qty = self._notional() / bar.close
        if qty <= 0:
            return None
        side = "sell" if move_pct > 0 else "buy"
        leg.side = "short" if side == "sell" else "long"
        leg.qty = qty
        leg.exit_at = start + timedelta(days=1)
        return self.signal(
            bar,
            side,
            qty,
            inputs={
                "kind": "day_fade_open",
                "move_pct": move_pct.quantize(D("0.0001")),
                "leg": bar.instrument,
            },
        )

    def _close(self, bar: Candle, leg: _Leg, reason: str) -> Signal:
        side = "buy" if leg.side == "short" else "sell"
        qty = leg.qty
        leg.side, leg.qty, leg.exit_at = "", ZERO, None
        return self.signal(
            bar,
            side,
            qty,
            inputs={"kind": "day_fade_close", "reason": reason, "leg": bar.instrument},
        )

    def on_event(self, event: Event) -> list[Signal]:
        return []


__all__ = ["DayOpenFadeStrategy"]
