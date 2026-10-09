"""Бумажный бот автомата CryptosMX (`scripts/crash_bounce_paper.py`).

Главная проверка — та же, что К3 объявления 09.10.2026, только заранее: на одной и той же ленте
зеркало бота обязано дать РОВНО те сделки, что архивный розыгрыш `crash_bounce_check._play`.
Иначе сверка «бот против архива» мерила бы расхождение кода, а не связи и задержек.
"""

from __future__ import annotations

import importlib.util
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


paper = _load("crash_bounce_paper")
check = _load("crash_bounce_check")
portfolio = _load("crash_bounce_portfolio")

T0 = 1_791_000_000_000 // 60_000 * 60_000  # начало минуты, октябрь 2026


def _tape(seed: int, minutes: int = 40) -> list:
    """Лента одной монеты: блуждание, обвалы минуты на 5–12% с отскоком, сделки в одну мс,
    минуты без сделок, сторона агрессора случайна."""
    rnd = random.Random(seed)
    px, a, out = 100.0, 1, []
    for m in range(minutes):
        start = T0 + m * 60_000
        if rnd.random() < 0.1:
            continue  # минута без сделок
        crash = rnd.random() < 0.3
        n = rnd.randint(5, 40)
        times = sorted(start + rnd.randint(0, 59_999) for _ in range(n))
        for i, t in enumerate(times):
            if crash and i == n // 3:
                px *= 1 - rnd.uniform(0.05, 0.12)
            elif crash and i > n // 3:
                px *= 1 + rnd.uniform(-0.002, 0.006)
            else:
                px *= 1 + rnd.uniform(-0.003, 0.003)
            out.append(paper.Trade("X", a, round(px, 4), t, rnd.random() < 0.5, t + 100))
            a += 1
            if rnd.random() < 0.15:  # вторая сделка в ту же мс
                out.append(paper.Trade("X", a, round(px * 0.999, 4), t, True, t + 100))
                a += 1
    return out


def _archive_trades(tape: list) -> list[dict]:
    """Как архив: события по минутным свечам из той же ленты, затем `_play` со стороной."""
    ts, px, qty, sell = array("q"), array("d"), array("d"), array("b")
    for tr in tape:
        ts.append(tr.T)
        px.append(tr.px)
        qty.append(1.0)
        sell.append(tr.sell)
    close, cur, events = None, None, []
    last_close = None
    for tr in tape:
        m = tr.T // 60_000 * 60_000
        if m != cur:
            if cur is not None:
                last_close = close
            cur = m
            if last_close is not None:
                events.append({"t": m, "prev_close": last_close})
        close = tr.px
    out = []
    for ev in events:
        r = check._play(ts, px, qty, ev, 0.05, "limit", 0, sell)
        if r is not None:
            out.append({"t": ev["t"], **r})
    return out


@pytest.mark.parametrize("seed", range(8))
def test_mirror_equals_archive_play(seed):
    tape = _tape(seed)
    got = []
    eng = paper.Engine(got.append)
    for tr in tape:
        eng.on_trade(tr)
    bot = {r["t"]: r for r in got if r["k"] == "exit" and r["acct"] == "m"}
    arch = {r["t"]: r for r in _archive_trades(tape)}
    assert arch, "лента без единого события — тест ничего не проверяет"
    assert bot.keys() == arch.keys()
    for t, a in arch.items():
        b = bot[t]
        assert (b["entry"], b["exit"], b["t_in"], b["t_out"], b["how"]) == \
               (a["entry"], a["exit"], a["t_in"], a["t_out"], a["how"])
        assert b["net"] == pytest.approx(a["net"], abs=1e-15)
        assert b["low"] == pytest.approx(a["low"], abs=1e-15)


def test_fill_journaled_before_exit():
    """Решение — в журнал в момент исполнения, исход — отдельной строкой позже."""
    got = []
    eng = paper.Engine(got.append)
    for tr in _tape(3):
        eng.on_trade(tr)
    fills = [i for i, r in enumerate(got) if r["k"] == "fill" and r["acct"] == "m"]
    exits = {(r["sym"], r["t"]): i for i, r in enumerate(got) if r["k"] == "exit"}
    assert fills
    for i in fills:
        key = (got[i]["sym"], got[i]["t"])
        assert key not in exits or exits[key] > i


def _book_online(rows: list[dict]) -> paper.LiveBook:
    """LiveBook в порядке времени, как его увидит бот: выходы до входа того же момента."""
    b = paper.LiveBook()
    open_ = []
    for tr in sorted(rows, key=lambda r: (r["t_in"], r["sym"])):
        for o in sorted([o for o in open_ if o["t_out"] <= tr["t_in"]], key=lambda o: o["t_out"]):
            open_.remove(o)
            b.close(o)
        if tr["sym"] in b.open:
            continue
        ok, over = b.can_fill(tr["t_in"])
        if not ok:
            continue
        pos = paper.Pos("x1", tr["sym"], tr["t"], tr["entry"], tr["t_in"], 0, tr["low"])
        b.take(pos, over)
        open_.append({**tr, "amt": pos.amt})
    for o in sorted(open_, key=lambda o: o["t_out"]):
        b.close(o)
    return b


def test_livebook_matches_run_book():
    rnd = random.Random(1)
    rows = []
    for _ in range(3000):
        t_in = T0 + rnd.randint(0, 20 * 86_400_000)
        rows.append({"sym": f"S{rnd.randint(0, 40)}", "t": t_in // 60_000 * 60_000,
                     "t_in": t_in, "t_out": t_in + rnd.randint(1_000, 420_000),
                     "entry": 1.0, "low": 0.9, "net": rnd.gauss(0.004, 0.02)})
    ref = portfolio.run_book(sorted(rows, key=lambda r: (r["t_in"], r["sym"])),
                             0.01, 3, None, True, fixed=True)
    got = _book_online(rows)
    assert sum(got.day_taken.values()) == ref.taken
    assert got.withdrawn + got.equity == pytest.approx(ref.withdrawn + ref.equity, abs=1e-12)
    assert got.min_bal == pytest.approx(ref.min_bal, abs=1e-12)


def _tr(a, px, t, sell=False, sym="X"):
    return paper.Trade(sym, a, px, t, sell, t + 50)


def _ready_engine(syms=("X",)):
    """×1 на этих монетах: сделка №1 по 100 в конце минуты T0, снимок уровня минуты T0+1
    (95) снят. Номера сделок у каждой монеты свои — следующий №2."""
    got = []
    eng = paper.Engine(got.append, list(syms))
    for s in syms:
        eng.on_trade(_tr(1, 100.0, T0 + 59_000, sym=s))
    eng.tick(T0 + 60_000 + paper.WAIT_MS)
    return eng, got


def test_x1_level_switch_delay_uses_old_order():
    """До перестановки (WAIT+SWITCH после начала минуты) стоит заявка прошлой минуты."""
    eng, got = _ready_engine()
    m1 = T0 + 60_000
    eng.on_trade(_tr(2, 96.0, m1 + 59_000))        # выше 95 — без исполнения; закрытие m1 = 96
    eng.tick(m1 + 60_000 + paper.WAIT_MS)          # уровень m2 = 91.2
    m2 = m1 + 60_000
    # 92 < 95 (старая заявка), но > 91.2 (новая): до перестановки — исполнение по 95
    eng.on_trade(_tr(3, 92.0, m2 + paper.WAIT_MS + paper.SWITCH_MS - 10))
    x1 = [r for r in got if r["k"] == "fill" and r["acct"] == "x1"]
    assert len(x1) == 1 and x1[0]["entry"] == pytest.approx(95.0)
    assert not [r for r in got if r["k"] == "fill" and r["acct"] == "m"]  # у архива 91.2


def test_x1_no_fill_after_switch_above_new_level():
    eng, got = _ready_engine()
    m1 = T0 + 60_000
    eng.on_trade(_tr(2, 96.0, m1 + 59_000))
    eng.tick(m1 + 60_000 + paper.WAIT_MS)
    eng.on_trade(_tr(3, 92.0, m1 + 60_000 + paper.WAIT_MS + paper.SWITCH_MS + 1))
    assert not [r for r in got if r["k"] == "fill" and r["acct"] == "x1"]


def test_x1_snapshot_on_liquid_coin():
    """Сделки новой минуты пришли раньше такта — уровень всё равно от закрытия ПРОШЛОЙ минуты.
    (Регрессия: снимок брался только у монет без сделок в новой минуте, и ликвидные монеты
    счёт ×1 не торговал бы никогда.)"""
    got = []
    eng = paper.Engine(got.append, ["X"])
    eng.on_trade(_tr(1, 100.0, T0 + 59_000))
    eng.on_trade(_tr(2, 101.0, T0 + 60_010))      # сделка новой минуты до такта
    eng.tick(T0 + 60_000 + paper.WAIT_MS)
    assert eng.st["X"].snap[T0 + 60_000] == pytest.approx(95.0)


def test_x1_slots_full_cancel_window():
    """Третий слот заполнен — остальные заявки снимаются CANCEL_MS: внутри окна — сверх
    предела, после — исполнения нет."""
    syms = [f"S{i}" for i in range(6)]
    eng, got = _ready_engine(syms)
    t = T0 + 60_000 + paper.WAIT_MS + paper.SWITCH_MS + 100
    for i, s in enumerate(syms[:3]):
        eng.on_trade(_tr(2, 94.0, t + i, sym=s))
    eng.on_trade(_tr(2, 94.0, t + 2 + paper.CANCEL_MS - 1, sym=syms[3]))   # в окне снятия
    eng.on_trade(_tr(2, 94.0, t + 2 + paper.CANCEL_MS + 1, sym=syms[4]))   # после
    x1 = [r for r in got if r["k"] == "fill" and r["acct"] == "x1"]
    assert [r["sym"] for r in x1] == syms[:4]
    assert [r["over"] for r in x1] == [False, False, False, True]


def test_x1_only_ranked_coins_and_room_shrinks_with_positions():
    ranked = [f"S{i}" for i in range(paper.ORDERS + 5)]
    eng = paper.Engine(lambda r: None, ranked)
    assert len(eng.eligible) == paper.ORDERS and "S100" not in eng.eligible
    eng.book.open["S0"] = paper.Pos("x1", "S0", 0, 1.0, 0, 0, 1.0)
    eng._recalc_eligible(0)
    # монета в позиции — без заявки, и заявок на одну меньше: деньги заняты позицией
    assert "S0" not in eng.eligible and len(eng.eligible) == paper.ORDERS - 1


def test_gap_blocks_minute_and_is_journaled():
    got = []
    eng = paper.Engine(got.append)
    eng.on_trade(_tr(1, 100.0, T0 + 59_000))
    eng.on_trade(_tr(5, 99.0, T0 + 60_500))       # номера 2–4 пропущены
    eng.on_trade(_tr(6, 90.0, T0 + 61_000))       # обвал в минуте с дырой — закрытию не верим
    assert [r for r in got if r["k"] == "gap"][0]["from_a"] == 2
    assert not [r for r in got if r["k"] == "fill"]
    eng.on_trade(_tr(7, 90.0, T0 + 119_000))
    eng.on_trade(_tr(8, 85.0, T0 + 120_100))      # следующая минута: закрытие 90 → уровень 85.5
    assert [r["acct"] for r in got if r["k"] == "fill"] == ["m"]


def test_duplicate_ids_ignored():
    """Соединения внахлёст при переподключении: повтор номера не обрабатывается дважды."""
    got = []
    eng = paper.Engine(got.append)
    for tr in (_tr(1, 100.0, T0 + 59_000), _tr(2, 94.0, T0 + 60_100), _tr(2, 94.0, T0 + 60_100)):
        eng.on_trade(tr)
    assert len([r for r in got if r["k"] == "fill"]) == 1
    assert not [r for r in got if r["k"] == "gap"]
