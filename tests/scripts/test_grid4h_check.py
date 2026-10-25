"""Сетка 4 ч на альт-перпах (`scripts/grid4h_check.py`): исполнение по замороженным правилам.

Правила — `docs/research/open-automata-anno-2026-10-25.md`, раздел 4: лимитка исполняется
только при проходе СТРОГО за уровень; после исполнения — противоположная на соседнем уровне
и следующая докупка; в минуте, где задето и то и другое, — худший для нас порядок; по
истечении жизни — рынок по закрытию минуты; ликвидация — уход на 20% против средней.
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


g4 = _load("grid4h_check")
G = 1 / (1 - 0.03)  # шаг 3%: вверх ×1/0.97, вниз ×0.97 — так же, как в скрипте
DN = G ** -1.0  # уровень на шаг ниже старта
START = 120_000  # минута hh:02 при границе t0 = 0


def _bars(rows: list[tuple[float, float, float, float]]):
    """(open, high, low, close) по минутам начиная с hh:02 границы 0."""
    t, o, h, lo, c = array("q"), array("d"), array("d"), array("d"), array("d")
    for i, (a, b, x, d) in enumerate(rows):
        t.append(START + i * 60_000)
        o.append(a)
        h.append(b)
        lo.append(x)
        c.append(d)
    return t, o, h, lo, c


def _play(rows, life=238, k=10, off=1.0, marks=False):
    return g4.play(*_bars(rows), 0, 0, 0.03, k, life, off, marks)


FLAT = (1.0, 1.0, 1.0, 1.0)


def test_round_trip_short_then_cover():
    v, _ = _play([FLAT, (1.0, G * 1.001, 1.0, G), (G, G, 0.999, 1.0), FLAT], life=4)
    gross, mk, close_n, nf, mp, _loss, p, cut = v
    assert gross == pytest.approx(G - 1)  # продали на +1 шаг, откупили на старте
    assert mk == pytest.approx(G + 1)
    assert (close_n, nf, mp, p, cut) == (0, 2, 1, 0, 0)


def test_touching_level_is_not_a_fill():
    v, _ = _play([FLAT, (1.0, G, DN, 1.0), FLAT])  # максимум ровно на уровне — очередь не наша
    assert v[3] == 0 and v[0] == 0


def test_market_close_at_end_of_life_with_costs():
    v, _ = _play([FLAT, (1.0, 1.0, 0.969, 0.98), (0.98, 0.99, 0.975, 0.98), FLAT], life=3)
    gross, mk, close_n, nf, _mp, _loss, p, cut = v
    assert (nf, p, cut) == (1, 1, 0)  # купили на −3%, позиция жива до конца жизни
    assert gross == pytest.approx(0.98 - DN)  # закрытие рынком по закрытию 3-й минуты
    assert close_n == pytest.approx(0.98)
    assert g4.net(v) == pytest.approx(0.98 - DN - 0.0002 * DN - 0.001 * 0.98)
    assert g4.net(v, "heavy") == pytest.approx(0.98 - DN - 0.0004 * DN - 0.003 * 0.98)


def test_data_ending_early_is_marked_cut():
    v, _ = _play([FLAT, FLAT], life=238)
    assert v[7] == 1


def test_worst_order_when_add_and_target_in_one_minute():
    # шорт 1 на +1 шаг; затем минута задевает и докупку (+2 шага), и цель (старт), закрытие
    # между +1 и +2: путь «сначала вниз» оставляет шорт 2 — он хуже, его и берём
    cl = G * 1.01
    v, _ = _play([FLAT, (1.0, G * 1.001, 1.0, G), (G, G * G * 1.001, 0.99, cl)], life=3)
    gross, _mk, _cn, nf, mp, _loss, p, _cut = v
    assert p == -2 and mp == 2 and nf == 4  # шорт; откуп на старте; шорт +1; шорт +2
    assert gross == pytest.approx((G - 1) + G + G * G - 2 * cl)


def test_levels_cap_stops_adding():
    rows = [FLAT] + [(G ** i, G ** (i + 1) * 1.001, G ** i, G ** (i + 1)) for i in range(5)]
    v, _ = _play(rows, k=2)
    assert v[6] == -2 and v[4] == 2  # дальше двух порций шорт не растёт
    avg = (G + G * G) / 2
    assert v[5] == pytest.approx(G ** 5 * 1.001 / avg - 1)  # худший уход — от средней


def test_loss_measured_against_average_entry():
    rows = [FLAT, (1.0, 1.0, DN * DN * 0.999, 0.95), (0.94, 0.94, 0.92, 0.93)]
    v, _ = _play(rows, life=3)
    avg = (DN + DN * DN) / 2
    assert v[6] == 2  # две докупки вниз, третья (−8.7%) не задета
    assert v[5] == pytest.approx(1 - 0.92 / avg)
    assert v[5] < g4.LIQ  # 3.7% от средней — не ликвидация при ×5


def test_half_step_start():
    half = G ** 0.5
    v, _ = _play([FLAT, (1.0, half * 1.001, 1.0, half), (half, half, 0.97 ** 0.5 * 0.999, 0.99)],
                 off=0.5)
    assert v[3] == 2 and v[6] == 0  # шорт на +0.5 шага, откуп на −0.5: ровно шаг
    assert v[0] == pytest.approx(half - 0.97 ** 0.5)


def test_after_target_both_sides_reopen():
    # шорт +1 → откуп на старте → ниже на шаг открывается лонг (сетка по обе стороны)
    rows = [FLAT, (1.0, G * 1.001, 1.0, G), (G, G, 0.999, 1.0), (1.0, 1.0, 0.969, 0.97)]
    v, _ = _play(rows, life=4)
    assert v[6] == 1 and v[3] == 3


def test_hourly_marks_for_funding_check():
    rows = [FLAT] * 10 + [(1.0, G * 1.001, 1.0, G)] + [(G, G, G, G)] * 227
    v, marks = _play(rows, marks=True)
    assert [m[0] for m in marks] == [3_600_000, 7_200_000, 10_800_000]  # не на границе 4 ч
    assert all(m[1] == -1 for m in marks)
    assert marks[0][2] == pytest.approx(G)
    assert v[6] == -1


def test_blocks_aggregate_hours_into_4h():
    h = g4.H1
    rows = [(0, 2.0, 1.0, 10.0), (h, 3.0, 1.5, 5.0), (4 * h, 1.0, 0.5, 1.0)]
    assert g4.blocks(rows) == [[0, 3.0, 1.0, 15.0, 2], [4 * h, 1.0, 0.5, 1.0, 1]]


def test_select_at_ranks_range_among_liquid_and_random_is_reproducible():
    t, h4 = 10 * g4.H4, g4.H4
    bs = {}
    for i, s in enumerate(["AUSDT", "BUSDT", "CUSDT", "DUSDT"]):
        hi = 1.0 + 0.01 * (i + 1)  # размах растёт с номером
        qv = 1e6 if s == "DUSDT" else 2e6  # D: 6 блоков × 1 млн — выше порога 5 млн
        bs[s] = {t - j * h4: (hi, 1.0, qv, 4) for j in range(1, 7)}
    bs["EUSDT"] = {t - h4: (2.0, 1.0, 9e6, 4)}  # 9 млн за один блок — проходит порог 5 млн
    bs["FUSDT"] = {t - 2 * h4: (3.0, 1.0, 9e6, 4)}  # прошлой свечи нет — не участвует
    r = g4.select_at(t, bs, seed=7, top=3, rnd=2)
    assert r["n"] == 5
    assert [x[0] for x in r["top"]] == ["EUSDT", "DUSDT", "CUSDT"]
    assert r["top"][1][2] == 6_000_000
    assert r["rnd"] == g4.select_at(t, bs, seed=7, top=3, rnd=2)["rnd"]
    assert set(r["rnd"]) <= {"AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"} and len(r["rnd"]) == 2
    low = g4.select_at(t, bs, seed=7, min_qv=1e7)
    assert low["n"] == 3 and set(low["rnd"]) == {"AUSDT", "BUSDT", "CUSDT"}  # D, E < 10 млн


def test_launches_scope():
    t0 = g4._ts("2025-03")
    top = [[f"C{i}USDT", 0.1, 1e7] for i in range(20)]
    lists = [{"t": t0, "n": 30, "top": top, "rnd": ["C3USDT", "XUSDT"]}]
    j = g4.launches_from(lists, "all")
    assert j[("C3USDT", "2025-03")][t0] is True  # топ-10: все клетки
    assert j[("C15USDT", "2025-03")][t0] is False  # места 11–20: главная
    assert j[("XUSDT", "2025-03")][t0] is False  # случайная: главная
    m = g4.launches_from(lists, "main")
    assert len(m) == 10 and not any(v[t0] for v in m.values())


def test_cells_grid_is_complete():
    assert len(g4.CELLS) == 23
    assert g4.CELLS["main"] == (0.03, 10, 238, 1.0)
    assert g4.CELLS["s3h8"] == (0.03, 10, 478, 1.0)
    assert set(g4.PLATEAU) <= set(g4.CELLS)
