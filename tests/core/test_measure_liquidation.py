"""Ликвидация ноги с залогом (09.09.2026).

До этого замер отвечал на вопрос «сколько заработали бы правила», но не на вопрос «дожил
ли счёт». Для хеджа «спот-лонг + шорт фьючерса» это принципиально: на бирже спот и фьючерс
— РАЗНЫЕ счета, и шорт ликвидируют, даже когда спот-нога того же хеджа в прибыли. Именно
так теряют деньги на кэш-энд-керри, который в бэктесте выглядит безрисковым.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Branch, Candle, Signal, StopSpec, StrategyManifest
from lab.core.measure import PaperEngine
from lab.core.measure.simulator import simulate

HOUR = timedelta(hours=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
PERP = "BTC/USDT:USDT"
SPOT = "BTC/USDT"


def _bar(instrument: str, i: int, price: Decimal, high: Decimal | None = None) -> Candle:
    return Candle(
        instrument=instrument,
        tf="1h",
        ts=T0 + HOUR * i,
        open=price,
        high=high or price,
        low=price,
        close=price,
        volume=Decimal(1000),
    )


def _engine(**kwargs) -> PaperEngine:
    return PaperEngine(
        venue="binance",
        instrument=PERP,
        tf="1h",
        branch=Branch.CEX_PERP,
        **kwargs,
    )


class ShortOnce:
    """Шортит один раз на первом баре и больше ничего не делает."""

    def __init__(self, instrument: str = PERP) -> None:
        self.instrument = instrument
        self.manifest = StrategyManifest(
            slug="short-once",
            branch=Branch.CEX_PERP,
            venue="binance",
            source_kind="test",
            instruments=[instrument],
            timeframe="1h",
            stop=StopSpec(max_dd_pct=Decimal(90)),
            params={"leverage": 1},
        )
        self.done = False

    def on_bar(self, bar: Candle) -> list[Signal]:
        if self.done:
            return []
        self.done = True
        return [
            Signal(
                strategy_id="x",
                decided_at=bar.ts + HOUR,
                instrument=self.instrument,
                side="sell",
                size=Decimal(1),
                price_ref=bar.close,
                inputs_hash="h",
                ttl_s=7200,
            )
        ]


def test_spot_leg_can_never_be_liquidated():
    """У спота залога нет: сколько бы ни вырос рынок, ликвидировать нечего."""
    engine = PaperEngine(venue="binance", instrument=SPOT, tf="1h", branch=Branch.CEX_SPOT)

    assert engine.leverage is None
    assert engine.margin_breach(_bar(SPOT, 0, Decimal(1_000_000))) is None


def test_short_survives_while_margin_holds():
    engine = _engine(leverage=Decimal(1))
    engine.submit(
        Signal(
            strategy_id="x",
            decided_at=T0,
            instrument=PERP,
            side="sell",
            size=Decimal(1),
            price_ref=Decimal(50_000),
            inputs_hash="h",
            ttl_s=7200,
        )
    )
    engine.on_bar(_bar(PERP, 0, Decimal(50_000)))

    # Рынок вырос на 50% — при плече 1 залога ещё хватает.
    assert engine.margin_breach(_bar(PERP, 1, Decimal(75_000))) is None


def test_short_is_liquidated_when_the_market_doubles():
    """Плечо 1: шорт теряет весь залог, когда цена удваивается."""
    engine = _engine(leverage=Decimal(1))
    engine.submit(
        Signal(
            strategy_id="x",
            decided_at=T0,
            instrument=PERP,
            side="sell",
            size=Decimal(1),
            price_ref=Decimal(50_000),
            inputs_hash="h",
            ttl_s=7200,
        )
    )
    engine.on_bar(_bar(PERP, 0, Decimal(50_000)))

    hit = engine.margin_breach(_bar(PERP, 1, Decimal(99_000), high=Decimal(101_000)))

    assert hit == Decimal(101_000), "смотреть надо на максимум бара, а не на закрытие"
    assert engine.liquidate(hit, T0 + HOUR) == 1
    assert engine.position == 0
    assert engine.closed and engine.closed[0].pnl_gross < 0


def test_liquidation_stops_the_strategy():
    """После ликвидации хедж сломан: открытия запрещены, как при пробое стопа."""
    strategy = ShortOnce()
    engine = _engine(leverage=Decimal(1))
    bars = [
        _bar(PERP, 0, Decimal(50_000)),
        _bar(PERP, 1, Decimal(50_000)),
        _bar(PERP, 2, Decimal(60_000), high=Decimal(120_000)),  # шип вверх
        _bar(PERP, 3, Decimal(60_000)),
    ]

    result = simulate(strategy, bars, engine=engine, stop=strategy.manifest.stop)

    assert result.liquidations, "ликвидация должна попасть в результат"
    assert result.liquidations[0][0] == PERP
    assert result.stop_rule == "ликвидация"
    assert result.stopped_at == T0 + HOUR * 2


def test_no_leverage_no_liquidation_in_simulation():
    """Без указанного плеча движок ведёт себя как раньше — ничего не ломается."""
    strategy = ShortOnce()
    engine = _engine()
    bars = [
        _bar(PERP, 0, Decimal(50_000)),
        _bar(PERP, 1, Decimal(50_000)),
        _bar(PERP, 2, Decimal(500_000)),
    ]

    result = simulate(strategy, bars, engine=engine, stop=strategy.manifest.stop)

    assert result.liquidations == []
