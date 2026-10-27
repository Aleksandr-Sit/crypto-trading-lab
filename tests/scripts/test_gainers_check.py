"""Шорт лидеров роста за сутки (`scripts/gainers_check.py`): исполнение по замороженным правилам.

Правила — `docs/research/open-automata-dhz-2026-10-26.md`, раздел 4: шорт продаёт по открытию
бара входа × (1 − проскальзывание), откупает по закрытию последнего бара × (1 + проскальзывание);
ликвидация ×3 изолированно — максимум часа ≥ вход × (1 + 1/3 − 0.5%), потеря −1/3 порции минус
комиссия; фандинг — выплаты строго внутри (T, T + H), шорт получает положительную ставку; выбор —
рост за 24 ч (закрытие T−1 ч к закрытию T−25 ч) среди перпов с оборотом 24 баров T−24…T−1 ч.
"""

from __future__ import annotations

import importlib.util
import sys
from array import array
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gc = _load("gainers_check")
H1 = gc.H1
SLIP, FEE = 0.0005, 0.0005
NAN = float("nan")


def _leg(bars, pays=(), slip=SLIP, fee=FEE):
    return gc.short_leg(list(bars), list(pays), slip, fee)


# ---------------------------------------------------------------- сторона и издержки

def test_short_profits_when_price_falls_without_costs():
    net, liq, fund, n = _leg([(0, 100.0, 101.0, 99.0), (H1, 99.0, 100.0, 90.0)], slip=0, fee=0)
    assert net == pytest.approx(0.10)
    assert (liq, fund, n) == (False, 0.0, 0)


def test_side_sell_at_bid_buy_at_ask():
    """Продажа по открытию × (1 − s), откуп по закрытию × (1 + s), тейкер с обеих сторон."""
    net, *_ = _leg([(0, 100.0, 101.0, 99.0), (H1, 99.0, 100.0, 90.0)])
    e, x = 100 * (1 - SLIP), 90 * (1 + SLIP)
    assert net == pytest.approx(1 - x / e - FEE * (1 + x / e))
    flat, *_ = _leg([(0, 100.0, 100.0, 100.0)])
    assert flat == pytest.approx(1 - 1.0005 / 0.9995 - FEE * (1 + 1.0005 / 0.9995))
    assert flat == pytest.approx(-0.002, abs=2e-6)  # круг ≈ 0.20% при главных издержках


def test_heavy_costs_worse_than_main():
    bars = [(0, 100.0, 101.0, 99.0), (H1, 99.0, 100.0, 95.0)]
    main, *_ = _leg(bars, slip=0.0005)
    heavy, *_ = _leg(bars, slip=0.0020)
    assert heavy < main


def test_exit_at_close_of_last_bar_not_first():
    bars = [(0, 100.0, 100.0, 80.0), (H1, 80.0, 120.0, 110.0)]
    net, liq, *_ = _leg(bars, slip=0, fee=0)
    assert not liq
    assert net == pytest.approx(-0.10)


# ---------------------------------------------------------------- ликвидация ×3

LIM = 100 * (1 - SLIP) * (1 + 1 / 3 - 0.005)


def test_liquidation_at_threshold_loses_third_minus_fee():
    bars = [(0, 100.0, 101.0, 100.0), (H1, 100.0, LIM, 95.0), (2 * H1, 95.0, 96.0, 90.0)]
    net, liq, _, _ = _leg(bars)
    assert liq
    assert net == pytest.approx(-1 / 3 - FEE)  # возврат цены после ликвидации не спасает


def test_just_below_threshold_survives():
    bars = [(0, 100.0, 101.0, 100.0), (H1, 100.0, LIM * 0.9999, 120.0)]
    net, liq, *_ = _leg(bars)
    assert not liq
    assert net == pytest.approx(1 - 120 * 1.0005 / 99.95 - FEE * (1 + 120 * 1.0005 / 99.95))


def test_entry_bar_high_counts_for_liquidation():
    net, liq, *_ = _leg([(0, 100.0, 140.0, 100.0), (H1, 100.0, 100.0, 50.0)])
    assert liq
    assert net == pytest.approx(-1 / 3 - FEE)


def test_heavier_slip_lowers_entry_and_threshold():
    """Порог считается от цены входа с проскальзыванием: у тяжёлых издержек он ниже."""
    hi = 100 * (1 - 0.0020) * (1 + 1 / 3 - 0.005) + 0.001
    bars = [(0, 100.0, 100.0, 100.0), (H1, 100.0, hi, 100.0)]
    assert not _leg(bars, slip=0.0005)[1]
    assert _leg(bars, slip=0.0020)[1]


# ---------------------------------------------------------------- фандинг

BARS24 = [(k * H1, 100.0, 101.0, 100.0) for k in range(24)]


def test_funding_strictly_inside_window():
    pays = [(0, 0.01), (8 * H1, 0.001), (16 * H1, -0.002), (24 * H1, 0.05)]
    net, _, fund, n = _leg(BARS24, pays, slip=0, fee=0)
    assert n == 2  # выплаты в T и в T + H не входят
    assert fund == pytest.approx(0.001 - 0.002)
    assert net == pytest.approx(fund)


def test_short_receives_positive_rate_on_price_at_payment():
    bars = [(0, 100.0, 100.0, 100.0), (H1, 100.0, 120.0, 120.0), (2 * H1, 120.0, 120.0, 120.0)]
    _, _, fund, n = _leg(bars, [(2 * H1, 0.001)], slip=0, fee=0)
    assert n == 1
    assert fund == pytest.approx(0.001 * 120 / 100)  # номинал на выплате — по закрытию часа до неё


def test_funding_after_liquidation_not_counted():
    bars = [(k * H1, 100.0, 101.0, 100.0) for k in range(8)]
    bars[4] = (4 * H1, 100.0, 200.0, 100.0)
    pays = [(4 * H1, 0.001), (6 * H1, 0.5)]
    net, liq, fund, n = _leg(bars, pays)
    assert liq
    assert n == 1  # в начале часа ликвидации позиция ещё жива, после — нет
    assert fund == pytest.approx(0.001 * 100 / 99.95)
    assert net == pytest.approx(-1 / 3 - FEE + fund)


def test_funding_after_delisting_not_counted():
    bars = BARS24[:10]  # данные кончились: позиция закрыта по последнему закрытию
    _, _, _, n = _leg(bars, [(8 * H1, 0.001), (16 * H1, 0.001)], slip=0, fee=0)
    assert n == 1


# ---------------------------------------------------------------- выбор

def _ser(n_h: int, close: dict[int, float], qv: dict[int, float] | None = None,
         base: float = 1.0, vol: float = 1e6) -> tuple:
    """Монета: n_h часов, закрытие по умолчанию `base`, оборот `vol` в каждом часе."""
    o, h, lo, c, q = (array("d", [base] * n_h) for _ in range(5))
    for i in range(n_h):
        q[i] = vol
    for i, v in close.items():
        c[i] = v
    for i, v in (qv or {}).items():
        q[i] = v
    return o, h, lo, c, q


T = 30 * H1  # индекс бара входа 30: рост — c[29] / c[5], оборот — q[6..29]


def test_rank_by_24h_growth_and_turnover_filter():
    ser = {
        "AAA": _ser(40, {29: 1.5}),  # +50%
        "BBB": _ser(40, {29: 1.2}),  # +20%
        "CCC": _ser(40, {29: 2.0}, vol=1e5),  # +100%, но оборот 2.4 млн за сутки
        "DDD": _ser(40, {5: 2.0, 29: 1.0}),  # −50%
    }
    row = gc.select_at(T, 0, ser, seed=7, uni=True)
    top = [x[0] for x in row["top"]["10"]]
    assert top == ["AAA", "BBB", "DDD"]  # CCC — 2.4 млн за сутки, у прочих 24 × 1 млн
    assert row["top"]["10"][0][1] == pytest.approx(0.5)
    assert row["n"] == {"5": 3, "10": 3, "25": 0}
    assert sorted(row["uni"]) == ["AAA", "BBB", "DDD"]


def test_turnover_window_is_bars_t24_to_t1():
    # 9.9 млн в окне T−24…T−1 ч; огромный оборот в барах T−25 и T в сумму не входит
    vol = 9.9e6 / 24
    ser = {"EEE": _ser(40, {29: 1.1}, {5: 1e9, 30: 1e9}, vol=vol)}
    assert gc.select_at(T, 0, ser, seed=7)["n"]["10"] == 0
    ser = {"EEE": _ser(40, {29: 1.1}, {6: 1e7}, vol=vol)}
    assert gc.select_at(T, 0, ser, seed=7)["n"]["10"] == 1


def test_needs_bars_t1_t25_and_entry():
    for miss in (29, 5):
        s = _ser(40, {29: 1.3})
        s[3][miss] = NAN
        assert gc.select_at(T, 0, {"X": s}, seed=7)["n"]["5"] == 0
    s = _ser(40, {29: 1.3})
    s[0][30] = NAN  # нет бара входа
    assert gc.select_at(T, 0, {"X": s}, seed=7)["n"]["5"] == 0


def test_ties_broken_by_name():
    ser = {n: _ser(40, {29: 1.3}) for n in ("ZZZ", "AAA", "MMM")}
    assert [x[0] for x in gc.select_at(T, 0, ser, seed=7)["top"]["10"]] == ["AAA", "MMM", "ZZZ"]


def test_random_control_fixed_seed():
    ser = {f"C{k:02d}": _ser(60, {29: 1 + k / 100, 41: 1 + k / 50}) for k in range(30)}
    a = gc.select_at(T, 0, ser, seed=7)
    b = gc.select_at(T, 0, ser, seed=7)
    assert a["rnd"] == b["rnd"]
    assert len(a["rnd"]) == 10 and len(set(a["rnd"])) == 10
    assert set(a["rnd"]) <= set(ser)
    later = gc.select_at(T + 12 * H1, 0, ser, seed=7)
    assert later["rnd"] != a["rnd"]  # зерно — ещё и время запуска
    assert gc.select_at(T, 0, ser, seed=8)["rnd"] != a["rnd"]


# ---------------------------------------------------------------- монето-запуск и сводка

def test_sim_one_cut_and_unknown_funding():
    o, h, lo, c, q = _ser(60, {})
    for i in range(40, 60):  # делистинг: данные кончились через 10 ч после входа
        for arr in (o, h, lo, c):
            arr[i] = NAN
    rec = gc.sim_one("X", T, {24}, 0, {"X": (o, h, lo, c, q)}, {}, set())
    v = rec["r"]["24"]
    assert v[gc.CUT] == 1 and v[gc.KNOWN] == 0 and v[gc.NPAY] == 0
    assert v[gc.NET] == pytest.approx(-0.002, abs=2e-6)


def test_drawdown_of_fixed_capital_curve():
    xs = {0: 0.05, 1: -0.10, 2: -0.05, 3: 0.20, 4: -0.30}
    assert gc.drawdown(xs) == pytest.approx(0.30)  # пик +0.10 → −0.20
    assert gc.drawdown({0: -0.10}) == pytest.approx(0.10)  # стартовый ноль — тоже вершина
