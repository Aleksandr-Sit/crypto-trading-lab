"""Стоп стратегии действует и в бэктесте (G04, История 10).

Живьём пробой стопа переводит стратегию в `degraded`: открывать нельзя, закрывать можно
(`RiskEngine` → `strategy_stop_dd|strategy_stop_daily`, `StopWatch`). Симулятор обязан вести
себя так же — иначе замер обещает то, чего система никогда не сделает: у одного из пресетов
бэктест без стопа показал убыток −222% при просадке 223%, то есть больше капитала.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Branch, Candle, Signal, StopSpec, StrategyManifest
from lab.core.measure import PaperEngine
from lab.core.measure.simulator import simulate

TF = timedelta(hours=1)
START = datetime(2026, 1, 1, tzinfo=UTC)
CAPITAL = Decimal(10_000)


def _falling(n: int) -> list[Candle]:
    """Каждый бар дешевле предыдущего на 1 — любая покупка закрывается в убыток."""
    out = []
    price = Decimal(1000)
    for i in range(n):
        out.append(
            Candle(
                instrument="SYN/USD",
                tf="1h",
                ts=START + TF * i,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal(1000),
            )
        )
        price -= Decimal(1)
    return out


class PingPong:
    """Покупает на чётных барах, продаёт на нечётных: на падающем ряду — стабильный минус."""

    def __init__(self, manifest: StrategyManifest) -> None:
        self.manifest = manifest
        self.i = 0

    def on_bar(self, bar: Candle) -> list[Signal]:
        self.i += 1
        side = "buy" if self.i % 2 else "sell"
        return [
            Signal(
                strategy_id="x",
                decided_at=bar.ts + TF,
                instrument=bar.instrument,
                side=side,
                size=Decimal(2),
                price_ref=bar.close,
                inputs_hash=f"h{self.i}",
                ttl_s=7200,
            )
        ]


def _manifest(stop: StopSpec) -> StrategyManifest:
    return StrategyManifest(
        slug="pingpong",
        branch=Branch.CEX_SPOT,
        venue="bybit",
        source_kind="test",
        instruments=["SYN/USD"],
        timeframe="1h",
        stop=stop,
    )


def _engine() -> PaperEngine:
    return PaperEngine(venue="bybit", instrument="SYN/USD", tf="1h", branch=Branch.CEX_SPOT)


def test_stop_by_drawdown_halts_opening_but_not_closing():
    manifest = _manifest(StopSpec(max_dd_pct=Decimal("0.2")))
    result = simulate(
        PingPong(manifest), _falling(60), engine=_engine(), stop=manifest.stop, capital=CAPITAL
    )

    assert result.stopped_at is not None, "стоп по просадке обязан сработать на падающем ряду"
    assert result.stop_rule == "strategy_stop_dd"
    assert result.blocked_signals > 0, "после пробоя открытия должны отсекаться"
    # Просадка не уходит далеко за лимит: после пробоя новых входов нет.
    losses = sum((t.pnl_net for t in result.trades), Decimal(0))
    assert -losses < CAPITAL * Decimal("0.01"), "убыток не должен расти после стопа"


def test_no_stop_in_manifest_means_no_halt():
    """Стоп задан только дневной и не пробит — симуляция идёт до конца окна."""
    manifest = _manifest(StopSpec(daily_pct=Decimal(90)))
    result = simulate(
        PingPong(manifest), _falling(60), engine=_engine(), stop=manifest.stop, capital=CAPITAL
    )

    assert result.stopped_at is None
    assert result.blocked_signals == 0


def test_stop_ignored_without_capital():
    """Капитал не задан — стоп не считается: делить проценты не на что."""
    manifest = _manifest(StopSpec(max_dd_pct=Decimal("0.2")))
    result = simulate(
        PingPong(manifest),
        _falling(60),
        engine=_engine(),
        stop=manifest.stop,
        capital=Decimal(0),
    )

    assert result.stopped_at is None
