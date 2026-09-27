"""Шов core.measure.run на синтетике (решение §17, истории 17–18, 24–26).

Стратегия с известным ответом: купить на первой свече, продать на последней.
Ожидаемый net_pnl считается вручную из свечей и тарифов costs.yaml, не кодом симулятора.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Branch, Candle, Signal, StopSpec, StrategyManifest
from lab.core.measure import (
    LookaheadError,
    Measurement,
    PaperEngine,
    run,
    walk_forward_windows,
)
from lab.core.registry import Registry
from tests.fixtures.synthetic import synthetic_candles

TF = timedelta(hours=1)
QTY = Decimal("1")


def _manifest(branch: Branch = Branch.CEX_SPOT, venue: str = "bybit") -> StrategyManifest:
    return StrategyManifest(
        slug="first-last",
        branch=branch,
        venue=venue,
        source_kind="test",
        instruments=["SYN/USD"],
        timeframe="1h",
        stop=StopSpec(daily_pct=Decimal(5)),
    )


class FirstLast:
    """Купить после первой свечи, продать после предпоследней (исполнение на следующем открытии)."""

    def __init__(self, n: int, manifest: StrategyManifest, *, lookahead: bool = False) -> None:
        self.manifest = manifest
        self.n = n
        self.seen = 0
        self.lookahead = lookahead

    def _signal(self, bar: Candle, side: str) -> Signal:
        decided = bar.ts if self.lookahead else bar.ts + TF
        return Signal(
            strategy_id="x",
            decided_at=decided,
            instrument=bar.instrument,
            side=side,
            size=QTY,
            price_ref=bar.close,
            inputs_hash="h",
            ttl_s=3600,
        )

    def on_bar(self, bar: Candle):
        self.seen += 1
        if self.seen == 1:
            return [self._signal(bar, "buy")]
        if self.seen == self.n - 1:
            return [self._signal(bar, "sell")]
        return []

    def on_event(self, event):
        return []


def _expected_net_pnl(candles: list[Candle]) -> Decimal:
    """Вручную: вход по открытию 2-й свечи, выход по открытию последней; без стакана
    проскальзывание = spread/2 (5 bps / 2) + impact (100 bps * оборот / 100 000);
    комиссия Bybit taker 10 bps от оборота по цене исполнения."""
    bps = Decimal(10_000)

    def leg(ref: Decimal, sign: int) -> tuple[Decimal, Decimal]:
        notional = ref * QTY
        slip_bps = Decimal("2.5") + Decimal(100) * notional / Decimal(100_000)
        px = ref * (1 + sign * slip_bps / bps)
        fee = QTY * px * Decimal(10) / bps
        return px, fee

    entry, fee_in = leg(candles[1].open, +1)
    exit_, fee_out = leg(candles[-1].open, -1)
    return (exit_ - entry) * QTY - fee_in - fee_out


@pytest.fixture
def candles() -> list[Candle]:
    return synthetic_candles(48, "trend", drift_pct=Decimal("0.5"), noise_pct=0, seed=1)


def _run(candles, strategy, **kw) -> Measurement:
    return run(
        "cex-spot-test-first-last",
        "backtest",
        (candles[0].ts, candles[-1].ts + TF),
        strategy=strategy,
        candles=candles,
        capital=Decimal(10_000),
        **kw,
    )


def test_backtest_known_answer_with_costs(candles):
    m = _run(candles, FirstLast(len(candles), _manifest()))
    assert m.status == "ok"
    assert m.metrics.n_trades == 1
    expected = _expected_net_pnl(candles)
    assert m.metrics.net_pnl.quantize(Decimal("0.00000001")) == expected.quantize(
        Decimal("0.00000001")
    )
    assert m.metrics.net_pnl > 0
    assert m.metrics.costs.fee > 0 and m.metrics.costs.slippage > 0
    assert m.metrics.sample.status == "insufficient"
    assert m.metrics.sample.detail == "недостаточно данных: 1 из 30"
    assert m.threshold.status == "insufficient"
    assert m.threshold.criteria[0].name == "n_trades"


def test_lookahead_signal_raises(candles):
    with pytest.raises(LookaheadError):
        _run(candles, FirstLast(len(candles), _manifest(), lookahead=True))


def test_snapshot_is_reproducible_and_idempotent(candles, session):
    reg = Registry(session)
    strategy = reg.add(_manifest())
    first = run(
        strategy.id,
        "backtest",
        (candles[0].ts, candles[-1].ts + TF),
        strategy=FirstLast(len(candles), _manifest()),
        candles=candles,
        session=session,
    )
    second = run(
        strategy.id,
        "backtest",
        (candles[0].ts, candles[-1].ts + TF),
        strategy=FirstLast(len(candles), _manifest()),
        candles=candles,
        session=session,
    )
    assert first.data_hash == second.data_hash
    assert len(first.data_hash) == 64
    assert len(first.code_version) >= 7
    assert first.metrics.net_pnl == second.metrics.net_pnl
    assert first.id == second.id and second.cached  # повтор — тот же снимок, без второй строки
    assert first.costs_version.startswith("costs-v")


def test_gap_in_data_marks_incomplete(candles):
    broken = candles[:20] + candles[25:]
    m = _run(broken, FirstLast(len(broken), _manifest()))
    assert m.status == "incomplete"
    assert "разрыв" in m.reason
    assert m.metrics is None


def test_source_failure_marks_incomplete(candles):
    def source(instrument, tf, from_ts, to_ts):
        raise ConnectionError("feed down")

    m = run(
        "cex-spot-test-first-last",
        "backtest",
        (candles[0].ts, candles[-1].ts + TF),
        strategy=FirstLast(len(candles), _manifest()),
        source=source,
    )
    assert m.status == "incomplete"
    assert "feed down" in m.reason


def test_vs_benchmark_uses_benchmark_window(candles):
    btc = synthetic_candles(
        48, "trend", drift_pct=Decimal("1"), noise_pct=0, seed=2, instrument="BTC/USD"
    )
    m = _run(candles, FirstLast(len(candles), _manifest()), benchmark=btc)
    # BTC B&H за окно = close[-1]/open[0] - 1 = 1.01^47 - 1 ≈ 59.6%
    bh = (btc[-1].close / btc[0].open - 1) * 100
    assert m.metrics.benchmark_pct.quantize(Decimal("0.0001")) == bh.quantize(Decimal("0.0001"))
    assert m.metrics.vs_benchmark == m.metrics.net_pnl_pct - m.metrics.benchmark_pct


def test_walk_forward_windows_split_in_and_out_of_sample():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    folds = walk_forward_windows(
        start,
        start + timedelta(days=10),
        in_sample=timedelta(days=4),
        out_of_sample=timedelta(days=2),
    )
    assert [(f.is_from, f.is_to, f.oos_from, f.oos_to) for f in folds] == [
        (start, start + timedelta(days=4), start + timedelta(days=4), start + timedelta(days=6)),
        (
            start + timedelta(days=2),
            start + timedelta(days=6),
            start + timedelta(days=6),
            start + timedelta(days=8),
        ),
        (
            start + timedelta(days=4),
            start + timedelta(days=8),
            start + timedelta(days=8),
            start + timedelta(days=10),
        ),
    ]


def test_walk_forward_run_reports_folds(candles):
    def factory():
        return FirstLast(12, _manifest())

    m = run(
        "cex-spot-test-first-last",
        "backtest",
        (candles[0].ts, candles[-1].ts + TF),
        strategy=factory,
        candles=candles,
        walk_forward=(timedelta(hours=24), timedelta(hours=12)),
    )
    assert m.status == "ok"
    assert len(m.folds) == 2  # 48 ч: [0,24)+[24,36), [12,36)+[36,48)
    assert all(f.in_sample is not None and f.out_of_sample is not None for f in m.folds)


def test_paper_price_is_worse_than_mid_and_rejects_late_signal(candles):
    engine = PaperEngine(venue="bybit", instrument="SYN/USD", tf="1h")
    engine.on_bar(candles[0])
    sig = Signal(
        strategy_id="x",
        decided_at=candles[0].ts + TF,
        instrument="SYN/USD",
        side="buy",
        size=QTY,
        price_ref=candles[0].close,
        inputs_hash="h",
        ttl_s=3600,
    )
    engine.submit(sig)
    fills = engine.on_bar(candles[1])
    assert len(fills) == 1
    assert fills[0].price > candles[1].open  # покупка хуже мида
    assert fills[0].ts >= sig.decided_at
    late = sig.model_copy(update={"decided_at": candles[0].ts})  # «решено» до уже известного бара
    with pytest.raises(LookaheadError):
        engine.submit(late)


def test_limit_partial_fill_and_perp_funding(candles):
    # Лимитка на 20 при объёме бара ~50–150 и участии 10 % исполняется частями по ≤ volume/10.
    engine = PaperEngine(venue="bybit", instrument="SYN/USD", tf="1h", branch="cex-perp",
                         funding_rate=Decimal("0.001"))
    engine.on_bar(candles[0])
    sig = Signal(strategy_id="x", decided_at=candles[0].ts + TF, instrument="SYN/USD", side="buy",
                 size=Decimal(20), price_ref=candles[1].high, inputs_hash="h", ttl_s=10 * 3600,
                 meta={"order_type": "limit", "limit_price": str(candles[1].high * 2)})
    engine.submit(sig)
    fills = engine.on_bar(candles[1])
    assert len(fills) == 1 and fills[0].price == candles[1].high * 2
    assert fills[0].qty == candles[1].volume * Decimal("0.1")  # частично
    assert engine.position == fills[0].qty and engine.pending
    for bar in candles[2:12]:
        engine.on_bar(bar)
    assert engine.position > fills[0].qty  # дозаполнилось на следующих барах
    # Фандинг раз в 8 ч (00:00, 08:00): позиция открыта с 01:00 → начисление в 08:00 по лонгу.
    total_funding = sum((lot.costs.funding for lot in engine.lots), Decimal(0))
    assert total_funding > 0
    bar8 = candles[8]
    assert bar8.ts.hour == 8
    # Лоты, открытые до 08:00, платят rate × qty × open бара 08:00 — проверяем по первому лоту.
    assert engine.lots[0].costs.funding == Decimal("0.001") * engine.lots[0].qty * bar8.open


def test_funding_counts_every_boundary_inside_bar():
    # Дневные бары, интервал фандинга Bybit 8 ч → внутри одного бара три начисления.
    daily = synthetic_candles(4, "flat", noise_pct=0, seed=7, tf="1d",
                              start=datetime(2026, 2, 1, tzinfo=UTC))
    engine = PaperEngine(venue="bybit", instrument="SYN/USD", tf="1d", branch="cex-perp",
                         funding_rate=Decimal("0.001"))
    engine.on_bar(daily[0])
    sig = Signal(strategy_id="x", decided_at=daily[0].ts + timedelta(days=1),
                 instrument="SYN/USD", side="buy", size=Decimal(2), price_ref=daily[1].open,
                 inputs_hash="h", ttl_s=86400)
    engine.submit(sig)
    # вход по открытию бара 02.02 00:00 — позиция застаёт все три отсечки дня: 00, 08, 16
    engine.on_bar(daily[1])
    after_entry = engine.lots[0].costs.funding
    assert after_entry == Decimal("0.001") * Decimal(2) * daily[1].open * 3
    engine.on_bar(daily[2])
    step = engine.lots[0].costs.funding - after_entry
    assert step == Decimal("0.001") * Decimal(2) * daily[2].open * 3
    # на часовых барах внутри бара не больше одной границы — старый счёт «час кратен 8» сохранён
    hourly = synthetic_candles(3, "flat", noise_pct=0, seed=7, tf="1h",
                               start=datetime(2026, 2, 1, 7, tzinfo=UTC))
    e2 = PaperEngine(venue="bybit", instrument="SYN/USD", tf="1h", branch="cex-perp",
                     funding_rate=Decimal("0.001"))
    e2.on_bar(hourly[0])
    sig2 = Signal(strategy_id="x", decided_at=hourly[0].ts + timedelta(hours=1),
                  instrument="SYN/USD", side="buy", size=Decimal(1), price_ref=None,
                  inputs_hash="h", ttl_s=7200)
    e2.submit(sig2)
    e2.on_bar(hourly[1])  # бар 08:00 — одна граница
    one = e2.lots[0].costs.funding
    assert one == Decimal("0.001") * Decimal(1) * hourly[1].open
    e2.on_bar(hourly[2])  # бар 09:00 — границ нет
    assert e2.lots[0].costs.funding == one


def test_partial_closes_split_lot_costs_without_loss(candles):
    # Издержки лота распределяются между частичными закрытиями без потерь и без удвоения.
    seen: list[Decimal] = []
    engine = PaperEngine(venue="bybit", instrument="SYN/USD", tf="1h",
                         on_fill=lambda fill, sig, costs, ref: seen.append(costs.total))
    engine.on_bar(candles[0])

    def send(side: str, size: str, at) -> None:
        engine.submit(Signal(strategy_id="x", decided_at=at, instrument="SYN/USD", side=side,
                             size=Decimal(size), price_ref=None, inputs_hash="h", ttl_s=3600))

    send("buy", "2", candles[0].ts + TF)
    engine.on_bar(candles[1])
    send("sell", "0.5", candles[1].ts + TF)
    engine.on_bar(candles[2])
    send("sell", "1.5", candles[2].ts + TF)
    engine.on_bar(candles[3])

    assert [t.qty for t in engine.closed] == [Decimal("0.5"), Decimal("1.5")]
    assert engine.position == 0
    total_costs = sum((t.costs.total for t in engine.closed), Decimal(0))
    assert total_costs == sum(seen, Decimal(0))  # ровно издержки трёх филлов, ничего не потеряно


def test_partial_closes_split_accrued_funding_too():
    """Фандинг, начисленный лоту между частичными закрытиями, делится без потерь."""
    daily = synthetic_candles(6, "flat", noise_pct=0, seed=11, tf="1d",
                              start=datetime(2026, 4, 1, tzinfo=UTC))
    seen: list[Decimal] = []
    engine = PaperEngine(venue="bybit", instrument="SYN/USD", tf="1d", branch="cex-perp",
                         funding_rate=Decimal("0.001"),
                         on_fill=lambda fill, sig, costs, ref: seen.append(costs.total))
    engine.on_bar(daily[0])

    def send(side: str, size: str, at) -> None:
        engine.submit(Signal(strategy_id="x", decided_at=at, instrument="SYN/USD", side=side,
                             size=Decimal(size), price_ref=None, inputs_hash="h", ttl_s=86400))

    send("buy", "2", daily[0].ts + timedelta(days=1))
    engine.on_bar(daily[1])
    send("sell", "1", daily[1].ts + timedelta(days=1))
    engine.on_bar(daily[2])   # между закрытиями лоту капает фандинг
    engine.on_bar(daily[3])
    send("sell", "1", daily[3].ts + timedelta(days=1))
    engine.on_bar(daily[4])

    assert engine.position == 0 and len(engine.closed) == 2
    funding_total = sum((t.costs.funding for t in engine.closed), Decimal(0))
    # начислено: бар 1 (2 ед) + бары 2 и 3 (1 ед) = 0.6 + 0.3 + 0.3; на баре 4 позиции уже нет
    day = Decimal("0.001") * Decimal(100) * 3  # 3 отсечки в сутки, цена 100 всюду
    assert funding_total == day * 2 + day + day
    # делится между закрытиями по остатку: первое забирает половину накопленного к тому моменту
    assert engine.closed[0].costs.funding == day * 2 / 2
    assert engine.closed[1].costs.funding == funding_total - day * 2 / 2
    assert sum((t.costs.total for t in engine.closed), Decimal(0)) == (
        sum(seen, Decimal(0)) + funding_total
    )


def test_perp_leg_pays_perp_tariff_spot_leg_spot_tariff(candles):
    """costs v2: у Bybit перпы 5.5 б.п. тейкера, спот 10. Та же покупка по тому же бару
    стоит ровно в 5.5/10 комиссии спота; проскальзывание от рынка не зависит."""
    got: dict[bool, tuple] = {}
    for is_perp in (False, True):
        engine = PaperEngine(venue="bybit", instrument="SYN/USD", tf="1h", is_perp=is_perp,
                             on_fill=lambda fill, sig, costs, ref, k=is_perp:
                             got.__setitem__(k, (fill.price, costs)))
        engine.on_bar(candles[0])
        engine.submit(Signal(strategy_id="x", decided_at=candles[0].ts + TF,
                             instrument="SYN/USD", side="buy", size=QTY, price_ref=None,
                             inputs_hash="h", ttl_s=3600))
        engine.on_bar(candles[1])
    (spot_px, spot), (perp_px, perp) = got[False], got[True]
    assert spot_px == perp_px and spot.slippage == perp.slippage
    # цена филла и цена в формуле комиссии считаются разными выражениями — сверка до 1e-12
    q = Decimal("1e-12")
    assert spot.fee.quantize(q) == (QTY * spot_px * 10 / Decimal(10_000)).quantize(q)
    assert perp.fee.quantize(q) == (QTY * perp_px * Decimal("5.5") / Decimal(10_000)).quantize(q)
