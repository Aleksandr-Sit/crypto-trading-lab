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


class HedgeOnce:
    """Открывает связку «спот-лонг + шорт фьючерса» одним баром и держит её."""

    def __init__(self, mode: str) -> None:
        self.manifest = StrategyManifest(
            slug="hedge-once",
            branch=Branch.CEX_PERP,
            venue="binance",
            source_kind="test",
            instruments=[SPOT, PERP],
            timeframe="1h",
            stop=StopSpec(max_dd_pct=Decimal(90)),
            params={"leverage": 1, "margin_mode": mode},
        )
        self.done = False

    def on_bar(self, bar: Candle) -> list[Signal]:
        if self.done or bar.instrument != PERP:
            return []
        self.done = True
        return [
            Signal(
                strategy_id="x",
                decided_at=bar.ts + HOUR,
                instrument=name,
                side=side,
                size=Decimal(1),
                price_ref=bar.close,
                inputs_hash=f"h-{name}",
                ttl_s=7200,
            )
            for name, side in ((SPOT, "buy"), (PERP, "sell"))
        ]


def _hedge_bars(prices: list[int]) -> list[Candle]:
    """Спот и фьючерс идут почти вровень — как настоящая связка кэш-энд-керри."""
    out: list[Candle] = []
    for i, price in enumerate(prices):
        out.append(_bar(SPOT, i, Decimal(price)))
        out.append(_bar(PERP, i, Decimal(price + 40)))
    return out


def _hedge_engines(leverage: Decimal | None) -> dict[str, PaperEngine]:
    return {
        SPOT: PaperEngine(venue="binance", instrument=SPOT, tf="1h", branch=Branch.CEX_PERP),
        PERP: PaperEngine(
            venue="binance", instrument=PERP, tf="1h", branch=Branch.CEX_PERP, leverage=leverage
        ),
    }


def test_isolated_margin_kills_the_hedge_that_the_spot_leg_would_have_saved():
    """Май 2021 в миниатюре: цена удваивается, шорт ликвидируют, а спот в этот момент в плюсе.

    На бирже спот и фьючерс — разные счета, и прибыль спота фьючерс не спасает. Именно
    так кэш-энд-керри и умер: шорт ETH открыт по 1956, закрыт биржей по 3966, а спот-нога
    в тот момент стоила на 114% дороже входа.
    """
    strategy = HedgeOnce("isolated")
    engines = _hedge_engines(Decimal(1))

    result = simulate(
        strategy,
        _hedge_bars([1956, 1956, 2500, 3200, 4000]),
        engines=engines,
        stop=strategy.manifest.stop,
    )

    assert [name for name, _ in result.liquidations] == [PERP]
    assert result.stop_rule == "ликвидация"


def test_cross_margin_saves_the_same_hedge():
    """Тот же рынок и та же связка, но счёт общий — ликвидации не происходит."""
    strategy = HedgeOnce("cross")
    engines = _hedge_engines(Decimal(1))

    result = simulate(
        strategy,
        _hedge_bars([1956, 1956, 2500, 3200, 4000]),
        engines=engines,
        stop=strategy.manifest.stop,
        cross_margin=True,
    )

    assert result.liquidations == [], "прибыль спота держит убыток фьючерса"
    assert result.stopped_at is None


def test_cross_margin_still_liquidates_a_naked_short():
    """Кросс-маржа не волшебство: держать убыток нечем, если второй ноги нет."""
    strategy = ShortOnce()
    engines = _hedge_engines(Decimal(1))

    result = simulate(
        strategy,
        [b for b in _hedge_bars([50_000, 50_000, 70_000, 90_000, 110_000]) if b.instrument == PERP],
        engines={PERP: engines[PERP]},
        stop=strategy.manifest.stop,
        cross_margin=True,
    )

    assert [name for name, _ in result.liquidations] == [PERP]


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
