"""Логика исполнения `scripts/twins_check.py` на синтетической ленте (п.9, двойники CryptosMX).

Правила заморожены (раздел 4 `docs/research/open-automata-twins-2026-10-27.md`); тесты держат,
что скрипт исполняет именно их: касание уровня (рынок — сделка ≤ уровня, лимитка — строго
ниже), вход через 0.25 с первой покупкой по ask, цель строго выше, таймер — первая продажа по
bid, одна позиция на монету, контроль вне ±30 мин от событий ≥2%, — и что розыгрыш совпадает
с `crash_bounce_check._play` (инструмент, прошедший сверку fidelity у CryptosMX).
"""

from __future__ import annotations

import importlib.util
import math
import random
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


cb = _load("crash_bounce_check")
tw = _load("twins_check")

DAY = "2026-10-05"
D0 = tw._d0(DAY)
T = D0 + 10 * 3_600_000  # 10:00 UTC — минута события
TAKER, MAKER, SLIP = tw.TAKER, tw.MAKER, tw.SLIP
MAIN = tw.CELLS["main"]
LV = 100.0 * (1 - 0.02)  # уровень главной клетки при закрытии прошлой минуты 100


def tape(trades: list[tuple[int, float, bool]]):
    """(время мс, цена, продавец-агрессор) → массивы ленты, как у `_trades`."""
    ts, px, qty, sell = array("q"), array("d"), array("d"), array("b")
    for t, p, s in trades:
        ts.append(t)
        px.append(p)
        qty.append(1.0)
        sell.append(s)
    return ts, px, qty, sell


def ev(t: int = T, pc: float = 100.0, drop: float = -0.03, q: float = 200e6) -> dict:
    return {"sym": "X", "t": t, "prev_close": pc, "open": pc, "low": pc * (1 + drop),
            "close": pc, "drop": drop, "qvol_min": 1.0, "qvol_24h": q}


def run(trades, e, mode="market", lag=0.25, tp=0.014, drop=0.02, start=None):
    ts, px, _, sell = tape(trades)
    return tw.play(ts, px, sell, e, drop, mode, lag, tp, start)


# ---------------------------------------------------------------- касание и вход

def test_market_entry_first_ask_after_lag():
    # касание ровно на уровне 98.0 (≤), покупки раньше 0.25 с и продажи пропускаются
    trades = [(T + 1000, 99.0, True), (T + 2000, LV, True), (T + 2100, 97.9, False),
              (T + 2250, 97.95, True), (T + 2300, 97.96, False),
              (T + 2300 + 400_000, 97.0, False),
              (T + 2300 + 420_000, 97.5, False),  # таймер: покупка по ask не годится
              (T + 2300 + 420_001, 97.4, True)]
    r = run(trades, ev())
    assert (r["entry"], r["t_in"]) == (97.96, T + 2300)
    assert (r["how"], r["exit"], r["t_out"]) == ("time", 97.4, T + 2300 + 420_001)
    assert r["net"] == pytest.approx(97.4 / 97.96 - 1 - 2 * TAKER)


def test_market_needs_touch_inside_event_minute():
    # уровень пройден только в следующей минуте — у этой минуты входа нет
    assert run([(T + 1000, 99.0, True), (T + 60_000, 97.0, True), (T + 61_000, 97.0, False)],
               ev()) is None


def test_limit_fills_only_strictly_below_level():
    trades = [(T + 1000, LV, True), (T + 5000, 97.99, True), (T + 6000, 98.5, False),
              (T + 5000 + 420_000, 98.2, True)]
    r = run(trades, ev(), mode="limit", lag=0)
    assert (r["entry"], r["t_in"]) == (LV, T + 5000)  # сделка ровно на уровне: очередь не наша
    assert r["net"] == pytest.approx(98.2 / LV - 1 - MAKER - TAKER)
    # та же лента рынком: касание уже на 98.0
    assert run(trades, ev())["t_in"] == T + 6000


def test_target_strictly_above():
    entry = 50.0
    tgt = entry * (1 + 0.014)
    trades = [(T + 1000, 48.9, True), (T + 1300, entry, False),
              (T + 10_000, tgt, False),          # ровно цель — не исполнение
              (T + 20_000, tgt * 1.0001, False)]  # строго выше — продано по цели
    r = run(trades, ev(pc=50.0))
    assert (r["how"], r["t_out"], r["exit"]) == ("tp", T + 20_000, tgt)
    assert r["net"] == pytest.approx(0.014 - TAKER - MAKER)


def test_tape_ends_before_exit_is_cut():
    trades = [(T + 1000, 97.0, True), (T + 1300, 97.0, False), (T + 100_000, 96.0, False)]
    assert run(trades, ev())["how"] == "cut"


# ---------------------------------------------------------------- совпадение с crash_bounce

def _random_tape(rng: random.Random):
    trades, t, p = [], T - 10_000, 100.0
    while t < T + 700_000:
        t += rng.randint(1, 2500)
        if T <= t < T + 20_000:
            p *= 1 - rng.uniform(0, 0.004)  # обвал в первые 20 с минуты
        else:
            p *= 1 + rng.gauss(0, 0.002)
        trades.append((t, round(p, 4), rng.random() < 0.5))
    return trades


@pytest.mark.parametrize("seed", range(40))
def test_play_matches_crash_bounce(seed):
    """С целью +1.5% (как у `crash_bounce_check`) розыгрыш тот же до сделки."""
    rng = random.Random(seed)
    ts, px, qty, sell = tape(_random_tape(rng))
    e = ev(drop=-0.06)
    for mode, lag in (("market", 0.1), ("market", 0.25), ("market", 1), ("limit", 0)):
        for drop in (0.02, 0.03, 0.05):
            a = cb._play(ts, px, qty, e, drop, mode, lag, sell)
            b = tw.play(ts, px, sell, e, drop, mode, lag, cb.TP)
            if a is None:
                assert b is None or b["how"] == "cut"
                continue
            for k in ("entry", "exit", "t_in", "t_out", "how", "low"):
                assert a[k] == b[k], (mode, lag, drop, k)
            assert a["net"] == pytest.approx(b["net"])


# ---------------------------------------------------------------- одна позиция на монету

def _patch(monkeypatch, trades):
    data = tape(trades)
    monkeypatch.setattr(tw, "_trades", lambda sym, day: data if day == DAY else tape([]))


def _position_tape():
    return [(T + 1000, LV, True), (T + 1250, 98.0, False),     # 1: вход 98.0
            (T + 90_000, 99.4, False),                          # цель 99.372 → выход
            (T + 61_000, 97.9, True),                           # 2: касание во время позиции
            (T + 95_000, LV, True), (T + 95_300, 98.1, False),    # 2: касание после выхода
            (T + 121_000, 97.0, True),                          # 3: вся минута занята
            (T + 515_301, 98.0, True), (T + 900_000, 98.0, True)]


def test_one_position_per_coin(monkeypatch):
    trades = sorted(_position_tape())
    _patch(monkeypatch, trades)
    evs = [ev(T), ev(T + 60_000), ev(T + 120_000)]
    rows = tw.sim_day("X", DAY, evs, [], ("main",), 7, ctrl=False)
    assert [(r["t"], r["t_in"], r["how"]) for r in rows] == [
        (T, T + 1250, "tp"),
        (T + 60_000, T + 95_300, "time"),  # касание после выхода в той же минуте — вход
    ]  # событие T+120 000 пропущено: позиция открыта всю минуту


def test_independent_plays_every_event(monkeypatch):
    _patch(monkeypatch, sorted(_position_tape()))
    evs = [ev(T), ev(T + 60_000), ev(T + 120_000)]
    rows = tw.sim_day("X", DAY, evs, [], ("main",), 7, ctrl=False, independent=True)
    assert [r["t"] for r in rows] == [T, T + 60_000, T + 120_000]
    assert rows[1]["t_in"] == T + 90_000  # от первого касания минуты, без учёта позиции


def test_cell_filters_drop_and_turnover(monkeypatch):
    _patch(monkeypatch, sorted(_position_tape()))
    small = ev(T, q=99e6)  # оборот ниже 100 млн — не главная клетка, но сосед ≥ 50 млн
    rows = tw.sim_day("X", DAY, [small], [], ("main", "q50"), 7, ctrl=False)
    assert [r["var"] for r in rows] == ["q50"]


# ---------------------------------------------------------------- контроль

def test_control_minutes_exclude_30min_around_events():
    lo, hi = D0, D0 + tw.DAY_MS - 60_000
    allowed = set(tw.control_minutes(lo, hi, [T]))
    for k in range(-30, 31):
        assert T + k * 60_000 not in allowed
    assert T - 31 * 60_000 in allowed and T + 31 * 60_000 in allowed
    assert len(allowed) == 1440 - 61


def test_control_moment_seeded_and_inside_allowed():
    allowed = tw.control_minutes(D0, D0 + tw.DAY_MS - 60_000, [T])
    a = tw.control_moment(allowed, 7, "X", T)
    assert a == tw.control_moment(allowed, 7, "X", T)
    assert a != tw.control_moment(allowed, 8, "X", T)
    assert a - a % 60_000 in set(allowed)
    assert tw.control_moment([], 7, "X", T) is None


def test_control_play_buys_first_ask_and_uses_same_exits():
    t0 = T + 3_600_000 + 12_345
    trades = [(t0 - 1, 100.0, False), (t0 + 10, 100.0, True), (t0 + 20, 100.2, False),
              (t0 + 20 + 420_000, 99.9, True)]
    ts, px, _, sell = tape(trades)
    r = tw.control_play(ts, px, sell, t0, 0.014)
    assert (r["entry"], r["t_in"], r["how"], r["exit"]) == (100.2, t0 + 20, "time", 99.9)
    assert r["net"] == pytest.approx(99.9 / 100.2 - 1 - 2 * TAKER)


def test_sim_day_pairs_control_with_each_main_trade(monkeypatch):
    trades = sorted(_position_tape() + [(D0 + k * 30_000, 100.0, k % 2 == 0)
                                        for k in range(2 * 1440)])
    _patch(monkeypatch, trades)
    evs = [ev(T), ev(T + 60_000), ev(T + 120_000)]
    rows = tw.sim_day("X", DAY, evs, [T, T + 60_000, T + 120_000], ("main",), 7)
    main = [r for r in rows if r["var"] == "main"]
    ctrl = [r for r in rows if r["var"] == "ctrl"]
    assert [r["t"] for r in ctrl] == [r["t"] for r in main]
    for r in ctrl:
        minute = r["t_ctrl"] - r["t_ctrl"] % 60_000
        assert minute < T - 30 * 60_000 or minute > T + 120_000 + 30 * 60_000
        assert r["how"] in ("tp", "time", "cut")  # cut — только у самой полуночи


# ---------------------------------------------------------------- события по минуткам

def _kl(t: int, o: float, lo: float, c: float, qv: float) -> list[str]:
    return [str(t), str(o), str(o), str(lo), str(c), "1", str(t + 59_999), str(qv)]


def test_scan_rows_first_minute_uses_previous_month_and_24h_window():
    m0 = D0  # «первая минута месяца»
    prev = [_kl(m0 - (1441 - k) * 60_000, 100.0, 100.0, 100.0, 1e6) for k in range(1441)]
    tail = tw.month_tail(prev)
    assert len(tail) == 1441 and tail[-1][2] == 100.0
    rows = [_kl(m0, 100.0, 97.5, 98.0, 5e6),                  # −2.5% к закрытию прошлого месяца
            _kl(m0 + 60_000, 98.0, 96.5, 97.0, 1e6),          # −1.53% на монете ~25 млн
            _kl(m0 + 3 * 60_000, 97.0, 95.0, 96.0, 1e6)]      # дыра: прошлой минуты нет
    out = tw.scan_rows("X", rows, tail, 0.015)
    assert [e["t"] for e in out] == [m0, m0 + 60_000, m0 + 3 * 60_000]
    assert out[0]["prev_close"] == 100.0 and out[0]["drop"] == pytest.approx(-0.025)
    # окно [t − 24 ч, t): 1440 минут прошлого месяца по 1 млн
    assert out[0]["qvol_24h"] == 1440 * 1e6
    assert out[1]["qvol_24h"] == 1439 * 1e6 + 5e6
    assert out[2]["gap_s"] == 120 and "gap_s" not in out[1]


def test_scan_rows_keep_rule():
    rows = [_kl(D0, 100.0, 100.0, 100.0, 1e6),
            _kl(D0 + 60_000, 100.0, 98.4, 99.0, 1e6),   # −1.6% на монете 1 млн — не пишется
            _kl(D0 + 120_000, 99.0, 97.0, 98.0, 1e6)]   # −2.02% — пишется на любой монете
    out = tw.scan_rows("X", rows, [], 0.015)
    assert [e["t"] for e in out] == [D0 + 120_000]
    assert out[0]["qvol_24h"] == 2e6


# ---------------------------------------------------------------- выборка, вес, портфель

def _evs_plan():
    out = []
    for k in range(5):  # 5 дней-символов слоя A, 7 — слоя B, один месяц
        out.append({"sym": f"A{k}", "t": D0, "drop": -0.025, "qvol_24h": 150e6})
    for k in range(7):
        out.append({"sym": f"B{k}", "t": D0, "drop": -0.016, "qvol_24h": 30e6})
    out.append({"sym": "C", "t": D0, "drop": -0.016, "qvol_24h": 10e6})  # вне кадра
    return out


def test_plan_strata_and_weights():
    st = tw.strata(_evs_plan(), main_only=False)
    assert sorted(st.values()) == ["A"] * 5 + ["B"] * 7
    plan = tw.make_plan(st, 4, 7, main_only=False)
    p = plan[DAY[:7]]
    assert (len(p["keys"]["A"]), len(p["keys"]["B"])) == (2, 2)
    w = tw.plan_weights(plan)
    assert sorted(w.values()) == [2.5, 2.5, 3.5, 3.5]
    assert plan == tw.make_plan(st, 4, 7, main_only=False)  # зерно фиксировано
    big = tw.make_plan(st, 10, 7, main_only=False)[DAY[:7]]
    assert (len(big["keys"]["A"]), len(big["keys"]["B"])) == (5, 5)
    only = tw.make_plan(tw.strata(_evs_plan(), main_only=True), 4, 7, main_only=True)
    assert (len(only[DAY[:7]]["keys"]["A"]), len(only[DAY[:7]]["keys"]["B"])) == (4, 0)


def test_heavy_costs():
    r = {"var": "main", "how": "time", "net": 0.0}
    assert tw.heavy(r)["net"] == pytest.approx(-2 * SLIP)
    assert tw.heavy({**r, "how": "tp"})["net"] == pytest.approx(-SLIP)
    assert tw.heavy({**r, "var": "limit"})["net"] == pytest.approx(-SLIP)


def test_portfolio_slots_and_one_per_coin():
    rows = [{"sym": f"S{k}", "t_in": T, "t_out": T + 60_000, "how": "tp", "net": 0.01}
            for k in range(6)]
    rows += [{"sym": "S0", "t_in": T + 1000, "t_out": T + 2000, "how": "time", "net": -0.5},
             {"sym": "S9", "t_in": T + 60_000, "t_out": T + 70_000, "how": "time", "net": -0.2}]
    taken, by_slot, by_coin = tw.portfolio(rows)
    assert len(taken) == 6 and by_slot == 1 and by_coin == 1  # S9 входит, когда слоты свободны
    assert taken[-1]["sym"] == "S9"


def test_wstats_matches_crash_bounce_stats():
    rng = random.Random(3)
    rows = [{"day": f"2025-01-{rng.randint(1, 9):02d}", "net": rng.gauss(0, 0.01), "how": "tp"}
            for _ in range(200)]
    n, _, mean, se, _ = tw.wstats(rows)
    n2, mean2, se2, *_ = cb._stats(rows)
    assert (n, mean) == (n2, pytest.approx(mean2)) and se == pytest.approx(se2)
    _, _, mw, sew, _ = tw.wstats(rows, lambda r: 2.0)  # равные веса не меняют ничего
    assert mw == pytest.approx(mean) and sew == pytest.approx(se)
    assert math.isnan(tw.wstats([])[2])
