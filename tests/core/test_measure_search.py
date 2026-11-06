"""Dependence, estimand consistency and selection accounting."""

from datetime import timedelta
from decimal import Decimal

from lab.config import load_threshold
from lab.core.measure import metrics
from lab.core.measure.types import ClosedTrade
from tests.core.test_measure_threshold import T0, _trade

WINDOW = (T0, T0 + timedelta(days=120))


def test_open_profit_does_not_change_closed_ev_or_its_interval():
    trades = [_trade(i, "-1", fee="0") for i in range(30)]
    m = metrics(trades, Decimal(0), capital=Decimal(1000), window=WINDOW,
                extra={"unrealized_pnl": 100})
    assert m.net_pnl == 70
    assert m.ev_per_trade == -1
    assert m.ev_ci95 == (Decimal(-1), Decimal(-1))


def test_simultaneous_hedge_legs_stay_together_and_order_does_not_matter():
    trades = []
    for i in range(30):
        long = _trade(i, "100", fee="0")
        short = _trade(i, "-99", fee="0")
        trades.extend([long, short])
    a = metrics(trades, None, capital=Decimal(1000), window=WINDOW)
    b = metrics(list(reversed(trades)), None, capital=Decimal(1000), window=WINDOW)
    assert a.ev_per_trade == Decimal("0.5")
    assert a.ev_ci95 == b.ev_ci95 == (Decimal("0.5"), Decimal("0.5"))
    assert a.bootstrap["groups"] == 30


def test_one_closing_moment_cannot_establish_temporal_uncertainty():
    trades = [_trade(0, "5") for _ in range(30)]
    m = metrics(trades, None, capital=Decimal(1000), window=WINDOW)
    assert m.n_trades == 30 and m.ev_ci95 is None
    assert m.bootstrap["groups"] == 1


def test_many_legs_in_few_groups_do_not_establish_search_confidence():
    trades = [_trade(i // 10, "5") for i in range(30)]
    m = metrics(trades, None, capital=Decimal(1000), window=WINDOW,
                extra={"research": {"tracked": True, "variants": 1}})
    assert m.n_trades == 30
    assert m.ev_search_ci is None
    assert m.bootstrap["groups"] == 3


def test_blocks_preserve_clusters_of_wins_and_losses():
    cfg = load_threshold().model_copy(update={"bootstrap_block_groups": 20})
    trades = [_trade(i, "1" if i < 100 else "-1", fee="0") for i in range(200)]
    m = metrics(trades, None, capital=Decimal(1000), window=WINDOW, config=cfg)
    # IID uncertainty for this balanced series would be about ±0.14.
    assert m.ev_ci95[0] < Decimal("-0.3")
    assert m.ev_ci95[1] > Decimal("0.3")
    assert m.bootstrap["block_groups"] == 20


def test_search_correction_widens_interval_and_requires_tail_resolution():
    trades = [_trade(i, "6" if i % 3 else "-4", fee="0") for i in range(90)]
    results = [metrics(
        trades, None, capital=Decimal(1000), window=WINDOW,
        extra={"research": {"tracked": True, "variants": n}},
    ) for n in (1, 5, 100)]
    assert results[0].ev_search_ci == results[0].ev_ci95
    assert results[1].ev_search_ci[0] <= results[0].ev_search_ci[0]
    assert results[1].ev_search_ci[1] >= results[0].ev_search_ci[1]
    assert results[2].ev_search_ci is None


def test_ev_weights_trades_not_closing_moments():
    trades: list[ClosedTrade] = [_trade(0, "100", fee="0")]
    trades += [_trade(i // 10 + 1, "0", fee="0") for i in range(30)]
    m = metrics(trades, None, capital=Decimal(1000), window=WINDOW)
    assert m.ev_per_trade == Decimal(100) / 31
    assert m.bootstrap["statistic"] == "closed_net_pnl_per_trade"


def test_actual_confidence_is_recorded():
    cfg = load_threshold().model_copy(update={"confidence": Decimal("0.8")})
    m = metrics([_trade(i, "5") for i in range(30)], None, capital=Decimal(1000),
                window=(T0, T0 + timedelta(days=30)), config=cfg)
    assert m.bootstrap["confidence"] == "0.8"
