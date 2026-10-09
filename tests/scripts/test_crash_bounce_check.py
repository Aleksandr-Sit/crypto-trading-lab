"""Розыгрыш дня `crash_bounce_check._sim_day` в строгом режиме замера вперёд.

Строгий режим не помечает день готовым, пока не выложена нужная лента, — иначе свежие сутки,
разобранные раньше архива, теряли бы сделки молча. Обратная беда: лента, которой не будет
никогда (монету сняли с торгов), вешала бы слой А навсегда — PUMPBTCUSDT 05.10.2026.
"""

from __future__ import annotations

import importlib.util
import sys
from array import array
from datetime import UTC, datetime
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


check = _load("crash_bounce_check")

DAY = "2026-10-05"
D0 = int(datetime(2026, 10, 5, tzinfo=UTC).timestamp() * 1000)


def _tape(t_from: int, t_to: int) -> tuple[array, array, array, array]:
    """Сделка раз в 5 с по 1.0, стороны чередуются."""
    ts, px, qty, sell = array("q"), array("d"), array("d"), array("b")
    for i, t in enumerate(range(t_from, t_to, 5_000)):
        ts.append(t)
        px.append(1.0)
        qty.append(1.0)
        sell.append(i % 2)
    return ts, px, qty, sell


def _ev(t: int) -> dict:
    return {"sym": "X", "t": t, "prev_close": 1.0, "open": 1.0, "low": 0.9, "close": 0.95,
            "drop": -0.10, "qvol_min": 1.0, "qvol_24h": None}


def _patch(monkeypatch, tape_day):
    monkeypatch.setattr(check, "_trades",
                        lambda sym, day: tape_day if day == DAY else _tape(0, 0))


def test_delisted_mid_day_does_not_wait_forever(monkeypatch):
    # обвал 09:15, торги кончились 09:20 — как у PUMPBTCUSDT; ленты 06.10 нет и не будет
    t_ev = D0 + (9 * 60 + 15) * 60_000
    _patch(monkeypatch, _tape(D0, t_ev + 5 * 60_000))
    rows = check._sim_day("X", DAY, [_ev(t_ev)], side=True, all_events=True,
                          names=("limit5",), strict_since=DAY)
    assert isinstance(rows, list)  # день разобран, а не «повторить позже»


def test_crossing_midnight_still_waits_for_next_tape(monkeypatch):
    # обвал 23:58: сделке нужны минуты следующих суток — без их ленты день не готов
    t_ev = D0 + (23 * 60 + 58) * 60_000
    _patch(monkeypatch, _tape(D0 + 23 * 3_600_000, D0 + 86_400_000))
    with pytest.raises(RuntimeError, match="стык суток"):
        check._sim_day("X", DAY, [_ev(t_ev)], side=True, all_events=True,
                       names=("limit5",), strict_since=DAY)
