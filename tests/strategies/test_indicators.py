"""Индикаторы Pifagor/Coinmetrika (G08, История 41): v0 по публичным описаниям с меткой,
интерфейс `Indicator.compute(frame) -> frame`, заглушки там, где логики нет,
тест-скелет на эталонных значениях (`fixtures/indicator_reference.csv`)."""

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from lab.contracts import Candle, parse_tf
from lab.core.measure import run
from lab.strategies import registry
from lab.strategies.indicators import coinmetrika, pifagor
from lab.strategies.indicators.base import LABEL_V0, IndicatorNotPorted
from lab.strategies.indicators.frame import Frame
from lab.strategies.indicators.ta import rsi, sma
from tests.fixtures.synthetic import synthetic_candles

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE = FIXTURES / "indicator_reference.csv"
SRC = Path(__file__).resolve().parents[2] / "src" / "lab" / "strategies" / "indicators"


def _candles(closes: list[int], tf: str = "1d") -> list[Candle]:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    out = []
    for i, c in enumerate(closes):
        d = Decimal(c)
        out.append(
            Candle(
                instrument="BTC/USDT",
                tf=tf,
                ts=t0 + timedelta(days=i),
                open=d,
                high=d + 1,
                low=d - 1,
                close=d,
                volume=Decimal(100),
            )
        )
    return out


def test_sma_and_rsi_known_values():
    assert np.allclose(sma(np.array([1.0, 2, 3, 4, 5]), 3)[2:], [2, 3, 4])
    up = rsi(np.array([float(x) for x in range(1, 20)]), 14)  # монотонный рост — RSI = 100
    assert np.isnan(up[13]) and np.allclose(up[14:], 100)


def test_indicator_compute_adds_columns_and_keeps_length():
    frame = Frame.from_candles(_candles(list(range(100, 160))))
    out = pifagor.PifagorMfi(period=14).compute(frame)
    assert len(out) == len(frame) and "mfi" in out.columns
    assert np.allclose(out["mfi"][14:], 100)  # деньги только втекают — MFI = 100
    out = coinmetrika.Rsi4w(rsi_period=14, smooth=4).compute(frame)
    assert "rsi4w" in out.columns and np.allclose(out["rsi4w"][~np.isnan(out["rsi4w"])], 100)


def test_stubs_raise_with_label_and_modules_carry_v0_mark():
    with pytest.raises(IndicatorNotPorted, match="нужен скрипт"):
        pifagor.Trader01().compute(Frame.from_candles(_candles([1, 2, 3])))
    with pytest.raises(IndicatorNotPorted, match="нужен скрипт"):
        coinmetrika.M2Global().compute(Frame.from_candles(_candles([1, 2, 3])))
    for name in ("pifagor.py", "coinmetrika.py"):
        assert LABEL_V0 in (SRC / name).read_text(encoding="utf-8")
    assert LABEL_V0 == "[ИНДИКАТОР v0 — по публичным описаниям; v1 — по скрипту пользователя]"


# (id, параметры замера, минимум закрытых сделок на синтетическом цикле)
INDICATOR_STRATEGIES = [
    ("cex-spot-indicator-pifagor-mfi-v0", None, 1),
    ("cex-spot-indicator-pifagor-forever-sma-v0", None, 1),
    (
        "cex-spot-indicator-coinmetrika-rsi4w-v0",
        {"ma_filter_days": 0},
        1,
    ),  # вторая строка замера карточки
    # «крупная точка» на 1W+1M — единицы сигналов за цикл (карточка: замер по корзине), поэтому
    # здесь проверяется только, что стратегия проходит замер без ошибок
    ("cex-spot-indicator-coinmetrika-mtf-oversold", None, 0),
    ("cex-spot-indicator-coinmetrika-monthly-trend-flip", None, 1),
]


def _cycle(tf: str, legs: tuple[tuple[int, str], ...]) -> list[Candle]:
    """Склейка трендовых отрезков (длина, дрейф %/бар): падение → рост → падение, чтобы недельные и
    месячные осцилляторы успели уйти в зону, выйти из неё и дать сигнал на выход."""
    out: list[Candle] = []
    for i, (n, drift) in enumerate(legs):
        kwargs = (
            {} if not out else {"start_price": out[-1].close, "start": out[-1].ts + parse_tf(tf)}
        )
        out += synthetic_candles(
            n, "trend", tf=tf, seed=3 + i, drift_pct=Decimal(drift), noise_pct=2, **kwargs
        )
    return out


@pytest.mark.parametrize(
    "sid, params, min_trades", INDICATOR_STRATEGIES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_indicator_strategies_backtest_on_synthetic(sid, params, min_trades):
    strategy = registry.build(sid, params=params)
    tf = strategy.manifest.timeframe
    raw = (
        _cycle(tf, ((1100, "-0.15"), (500, "0.3"), (300, "-0.3")))
        if tf == "1d"
        else synthetic_candles(900, "noise", tf=tf, seed=3, noise_pct=3)
    )
    candles = [
        c.model_copy(update={"instrument": strategy.manifest.instruments[0], "tf": tf}) for c in raw
    ]
    m = run(
        sid,
        "backtest",
        (candles[0].ts, candles[-1].ts + timedelta(days=1)),
        strategy=strategy,
        candles=candles,
    )
    assert m.status == "ok", m.reason
    assert m.metrics.n_trades >= min_trades, sid


def test_forever_sma_rides_uptrend():
    sid = "cex-spot-indicator-pifagor-forever-sma-v0"
    strategy = registry.build(sid, params={"sma_period_days": 50})
    up = synthetic_candles(300, "trend", tf="1d", seed=1, drift_pct=Decimal("0.5"), noise_pct=1)
    down = synthetic_candles(
        200,
        "trend",
        tf="1d",
        seed=2,
        drift_pct=Decimal("-0.5"),
        noise_pct=1,
        start_price=up[-1].close,
        start=up[-1].ts + timedelta(days=1),
    )
    candles = [c.model_copy(update={"instrument": "BTC/USDT"}) for c in up + down]
    m = run(
        sid,
        "backtest",
        (candles[0].ts, candles[-1].ts + timedelta(days=1)),
        strategy=strategy,
        candles=candles,
    )
    assert m.metrics.n_trades >= 1 and m.metrics.net_pnl > 0


def _reference_rows() -> list[dict[str, str]]:
    with REFERENCE.open(encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(line for line in fh if not line.startswith("#"))]
    return rows


@pytest.mark.parametrize("row", _reference_rows(), ids=lambda r: f"{r['indicator']}@{r['date']}")
def test_indicator_reference_values(row):
    """Скелет: строки с датами сигналов со скриншотов — эталон ±1 месяц; значения (число на дату)
    пользователь допишет. Данные BTC/USD с 2012 (Bitstamp) — файл `fixtures/btcusd_1d.csv`."""
    data = FIXTURES / row["data_file"]
    if not data.exists():
        pytest.skip(f"нет ряда {data.name}: положите дневные свечи BTC/USD (Bitstamp) и повторите")
    if not row["expected"] and not row["signal"]:
        pytest.skip("строка-шаблон: ни expected, ни signal не заполнены")
    frame = Frame.from_csv(data)
    indicator = (
        coinmetrika.by_name(row["indicator"])
        if row["author"] == "coinmetrika"
        else pifagor.by_name(row["indicator"])
    )
    out = indicator.compute(frame)
    at = datetime.fromisoformat(row["date"]).replace(tzinfo=UTC)
    if row["signal"]:
        hits = out.signal_dates(row["signal"])
        assert any(abs((h - at).days) <= int(row.get("tolerance_days") or 31) for h in hits), (
            f"{row['indicator']}: нет сигнала {row['signal']} около {row['date']}"
        )
    if row["expected"]:
        value = out.value_at(row["column"], at)
        assert abs(value - float(row["expected"])) <= float(row.get("abs_tol") or 1)
