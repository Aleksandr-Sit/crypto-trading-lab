"""Стоп яруса размещения в замере: от вершины капитала, с открытой позицией (28.09.2026).

Решение владельца 4 (27.09.2026): стоп правила размещения — −35 % от ВЕРШИНЫ капитала
стратегии. Прежний стоп считал просадку только по закрытым сделкам и от стартового капитала:
правило, держащее BTC неделями, не увидело бы падения внутри позиции, пока та не закроется.
Живой портфель считает так же (`tests/ops/test_allocation_tier.py`) — определение одно,
`lab.contracts.allocation`.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Branch, Candle, Signal, StopSpec
from lab.contracts.allocation import is_allocation, peak_drawdown_pct
from lab.core.measure import PaperEngine
from lab.core.measure.simulator import simulate

DAY = timedelta(days=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SPOT = "BTC/USDT"
STOP = StopSpec(max_dd_pct=Decimal(35))


def _bars(prices: list[int]) -> list[Candle]:
    return [
        Candle(
            instrument=SPOT,
            tf="1d",
            ts=T0 + DAY * i,
            open=Decimal(p),
            high=Decimal(p),
            low=Decimal(p),
            close=Decimal(p),
            volume=Decimal(1_000_000),
        )
        for i, p in enumerate(prices)
    ]


def _engine() -> PaperEngine:
    return PaperEngine(venue="binance", instrument=SPOT, tf="1d", branch=Branch.CEX_SPOT)


class BuyAndHold:
    """Покупает на весь капитал на первом баре; на баре `again` пытается докупить."""

    def __init__(self, again: int | None = None) -> None:
        self.i = -1
        self.again = again

    def on_bar(self, bar: Candle) -> list[Signal]:
        self.i += 1
        if self.i != 0 and self.i != self.again:
            return []
        return [
            Signal(
                strategy_id="x",
                decided_at=bar.ts + DAY,
                instrument=SPOT,
                side="buy",
                size=Decimal("0.2"),  # 0.2 × 50 000 = 10 000 — капитал замера
                price_ref=bar.close,
                inputs_hash="h",
                ttl_s=2 * 86_400,
            )
        ]


# вход 50 000 (бар 1) → вершина 60 000 (капитал ≈12 000) → 40 000 (≈8 000, −33 % от
# вершины) → 36 000 (≈7 200, −40 %) — ни одной закрытой сделки за всё окно
PRICES = [50_000, 50_000, 60_000, 40_000, 36_000, 36_000, 36_000]


def test_contract_helpers():
    assert is_allocation({"allocation": True}) is True
    assert is_allocation({"allocation": False}) is False
    assert is_allocation({}) is False and is_allocation(None) is False
    assert peak_drawdown_pct(Decimal(12_000), Decimal(7_800)) == Decimal(35)
    assert peak_drawdown_pct(Decimal(12_000), Decimal(13_000)) == Decimal(0)
    assert peak_drawdown_pct(Decimal(0), Decimal(-5)) == Decimal(0)


def test_allocation_stop_sees_drawdown_inside_open_position():
    result = simulate(
        BuyAndHold(), _bars(PRICES), engine=_engine(), stop=STOP, allocation=True
    )
    assert result.trades == []  # позиция так и не закрылась
    assert result.stop_rule == "strategy_stop_dd"
    assert result.stopped_at == T0 + DAY * 4  # 36 000: −40 % от вершины 60 000


def test_ordinary_stop_does_not_see_it():
    """Та же позиция у обычной стратегии: по закрытым сделкам просадки нет — стопа нет."""
    result = simulate(BuyAndHold(), _bars(PRICES), engine=_engine(), stop=STOP)
    assert result.stopped_at is None


def test_drawdown_short_of_the_stop_does_not_fire():
    result = simulate(
        BuyAndHold(),
        _bars([50_000, 50_000, 60_000, 40_000, 40_000]),  # −33 % от вершины
        engine=_engine(),
        stop=STOP,
        allocation=True,
    )
    assert result.stopped_at is None


def test_after_the_stop_opening_is_blocked():
    """Как `degraded` вживую: после пробоя докупить нельзя."""
    result = simulate(
        BuyAndHold(again=5), _bars(PRICES), engine=_engine(), stop=STOP, allocation=True
    )
    assert result.stopped_at == T0 + DAY * 4
    assert result.blocked_signals == 1
