"""Пресеты ботов (R07, История 43): ≥5 стратегий-гипотез из карточек тикета 13, каждая
бэктестится через `core.measure.run` на синтетике и даёт ожидаемый знак результата
в понятных условиях (сетка во флэте — плюс, в тренде — минус, и т.п.)."""

from datetime import timedelta
from decimal import Decimal

import pytest

from lab.contracts import parse_tf
from lab.core.measure import run
from lab.strategies import registry
from tests.fixtures.synthetic import synthetic_candles

CARDS = {
    "cex-spot-preset-pionex-grid-arith": "cex-spot-pionex-grid-arith",
    "cex-spot-preset-3commas-dca-safety": "cex-spot-3commas-dca-safety",
    "cex-perp-preset-binance-futures-grid-neutral": "cex-perp-binance-futures-grid-neutral",
    "cex-spot-preset-martingale-capped": "cex-spot-martingale-capped",
    "cex-perp-preset-trailing-breakout-bot": "cex-perp-trailing-breakout-bot",
}


def _measure(sid: str, candles, **params):
    strategy = registry.build(sid, params=params or None)
    tf = strategy.manifest.timeframe
    candles = [
        c.model_copy(update={"instrument": strategy.manifest.instruments[0], "tf": tf})
        for c in candles
    ]
    window = (candles[0].ts, candles[-1].ts + timedelta(days=1))
    return run(sid, "backtest", window, strategy=strategy, candles=candles, capital=Decimal(10_000))


def test_five_presets_registered_with_card_params():
    ids = registry.ids()
    for sid, card in CARDS.items():
        assert sid in ids
        m = registry.manifest(sid)
        assert m.params["card"] == card and m.stop is not None and m.can_backtest


@pytest.mark.parametrize(
    "sid, kind, kwargs, params, sign",
    [
        # сетка: во флэте собирает колебания, в падении — стоп ниже нижней границы
        (
            "cex-spot-preset-pionex-grid-arith",
            "flat",
            dict(noise_pct=3),
            dict(grids=10, range_lookback_bars=60),
            +1,
        ),
        (
            "cex-spot-preset-pionex-grid-arith",
            "trend",
            dict(drift_pct=Decimal("-0.05"), noise_pct=1),
            dict(grids=10, range_lookback_bars=60),
            -1,
        ),
        # DCA: возврат к среднему во флэте — плюс; затяжное падение со стопом — минус
        ("cex-spot-preset-3commas-dca-safety", "flat", dict(noise_pct=3), {}, +1),
        (
            "cex-spot-preset-3commas-dca-safety",
            "trend",
            dict(drift_pct=Decimal("-0.05"), noise_pct=1),
            dict(stop_loss_pct=10),
            -1,
        ),
        # нейтральный грид на перпе: флэт — плюс; тренд накапливает позицию против — стоп
        (
            "cex-perp-preset-binance-futures-grid-neutral",
            "flat",
            dict(noise_pct=3),
            dict(grids=10, range_lookback_bars=60),
            +1,
        ),
        (
            "cex-perp-preset-binance-futures-grid-neutral",
            "trend",
            dict(drift_pct=Decimal("0.05"), noise_pct=1),
            dict(grids=10, range_lookback_bars=60),
            -1,
        ),
        # мартингейл с лимитом: мелкие откаты — плюс (трейлинг тейка 0,3 % на синтетике с шумом
        # 3 % за бар бессмыслен — отключён); падение — стоп
        (
            "cex-spot-preset-martingale-capped",
            "flat",
            dict(noise_pct=3),
            dict(trailing_take_profit_pct=0),
            +1,
        ),
        (
            "cex-spot-preset-martingale-capped",
            "trend",
            dict(drift_pct=Decimal("-0.05"), noise_pct=1),
            {},
            -1,
        ),
        # трейлинг-пробой: тренд с разворотом (трейлинг закрывает в плюс) — плюс;
        # флэт — серия ложных пробоев по стопу
        (
            "cex-perp-preset-trailing-breakout-bot",
            "reversal",
            dict(),
            dict(breakout_lookback=20),
            +1,
        ),
        (
            "cex-perp-preset-trailing-breakout-bot",
            "flat",
            dict(noise_pct=3),
            dict(breakout_lookback=5),
            -1,
        ),
    ],
)
def test_preset_sign_on_synthetic(sid, kind, kwargs, params, sign):
    tf = registry.manifest(sid).timeframe
    if kind == "reversal":  # 400 баров вверх по 0,3 %, затем 400 вниз — трейлинг фиксирует ход
        up = synthetic_candles(
            400, "trend", tf=tf, seed=7, drift_pct=Decimal("0.3"), noise_pct=Decimal("0.2")
        )
        down = synthetic_candles(
            400,
            "trend",
            tf=tf,
            seed=8,
            drift_pct=Decimal("-0.3"),
            noise_pct=Decimal("0.2"),
            start_price=up[-1].close,
            start=up[-1].ts + parse_tf(tf),
        )
        candles = up + down
    else:
        candles = synthetic_candles(1500, kind, tf=tf, seed=7, **kwargs)
    m = _measure(sid, candles, **params)
    assert m.status == "ok", m.reason
    assert m.metrics.n_trades > 0
    assert (m.metrics.net_pnl > 0) == (sign > 0), (sid, kind, m.metrics.net_pnl, m.metrics.n_trades)


def test_presets_keep_state_per_instrument():
    """Бот из каталога настраивается на ОДНУ пару — три пары в записи это три бота.

    Пока состояние было общим, портфельный замер получал ерунду и молча: после бара BTC
    по 60 000 приходил бар ETH по 2467, DCA-бот считал это падением на 96% и докупал
    «страховочный ордер». За 30 суток — 43 199 покупок при ОДНОЙ закрытой сделке
    и SIGKILL от cgroup без единой строки вывода.
    """
    from datetime import UTC, datetime, timedelta

    from lab.contracts import Candle
    from lab.strategies import registry as reg

    minute = timedelta(minutes=1)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    def bar(instrument: str, i: int, price: str) -> Candle:
        p = Decimal(price)
        return Candle(
            instrument=instrument,
            tf="1m",
            ts=t0 + minute * i,
            open=p,
            high=p,
            low=p,
            close=p,
            volume=Decimal(1000),
        )

    s = reg.build("cex-spot-preset-3commas-dca-safety")
    s.manifest = s.manifest.model_copy(update={"instruments": ["BTC/USDT", "ETH/USDT"]})

    signals = []
    for i in range(30):  # цены рядов различаются в 25 раз — как BTC и ETH на самом деле
        signals += s.on_bar(bar("BTC/USDT", i, "60000"))
        signals += s.on_bar(bar("ETH/USDT", i, "2400"))

    assert set(s.book) == {"BTC/USDT", "ETH/USDT"}, "у каждой пары своя сделка"
    # По одной базовой покупке на пару; докупок нет — цена не падала ни на одном ряду.
    assert len(signals) == 2, f"лишние покупки от чужого ряда: {len(signals)}"
    assert {sig.instrument for sig in signals} == {"BTC/USDT", "ETH/USDT"}
