"""Подпись автомата и шорт-лист `scripts/open_automata.py` (п.9, 24.10.2026).

Пороги объявлены до пилота, классы — до итога полного скана; тесты держат их смысл:
таймер на убытках узнаётся (тип CryptosMX), мартингейл и перпы акций уходят в сторону,
скан после обрыва продолжает с места и не теряет портфель с ошибкой сети. Сети тесты не
трогают — `history` подменяется.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


oa = _load("open_automata")

HOUR0 = 497_222 * 3_600_000  # ровно начало часа: минута часа 0


def _pos(opened: int, dur_s: float, ret: float, side: str = "Long",
         symbol: str = "AAAUSDT") -> dict:
    px1 = 100.0 * (1 + ret) if side == "Long" else 100.0 * (1 - ret)
    return {"symbol": symbol, "side": side, "opened": opened,
            "closed": opened + int(dur_s * 1000), "avgCost": "100",
            "avgClosePrice": str(px1), "leverage": 1}


def _cryptosmx_like() -> list[dict]:
    """60 сделок за 30 дней в разные минуты: прибыль +1.5% за 10 с, убыток — ровно 7 мин."""
    rows = []
    for i in range(60):
        opened = HOUR0 + i * 12 * 3_600_000 + (i * 7 % 60) * 60_000
        if i % 3:
            rows.append(_pos(opened, 10, 0.015, symbol=f"C{i % 9}USDT"))
        else:
            rows.append(_pos(opened, 420, -0.02, symbol=f"C{i % 9}USDT"))
    return rows


def _martingale(symbol: str = "AAAUSDT") -> list[dict]:
    """40 сделок, все в плюс ровно на +1.0%, длительность разбросана (таймера нет)."""
    return [_pos(HOUR0 + i * 7 * 3_600_000 + (i * 11 % 60) * 60_000, 60 * 2 ** (i % 8), 0.01,
                 symbol=symbol) for i in range(40)]


def _on_minute(share_at_zero: float) -> list[dict]:
    """40 сделок; доля `share_at_zero` входит в минуту :00, исходы и длительности разные."""
    rows = []
    k = round(40 * share_at_zero)
    for i in range(40):
        minute = 0 if i < k else 1 + (i * 13 % 59)
        opened = HOUR0 + i * 5 * 3_600_000 + minute * 60_000
        rows.append(_pos(opened, 60 * 2 ** (i % 8), 0.01 * ((i % 5) - 2) + 0.003))
    return rows


def _row(pid: str, pos: list[dict], tradfi: frozenset[str] = frozenset()) -> dict:
    return oa.scan_row(pid, {"nickname": pid}, pos, len(pos), set(tradfi))


def test_timer_on_losses_is_cryptosmx_type():
    f = oa.features(_cryptosmx_like())
    status, hits = oa.classify(f)
    assert status == "eligible"
    assert f["timer_loss_share"] == 1.0 and f["timer_loss_mode_s"] == 420
    assert "T_timer_on_losses" in hits and "M_fixed_minute" not in hits
    assert oa.klass(f, hits) == "timer_T"  # таймер сильнее «одной цели»


def test_martingale_and_tradfi_go_aside():
    f = _row("m", _martingale())
    assert f["hits"] == ["P_fixed_target"] and f["win_share"] == 1.0
    assert oa.klass(f, f["hits"]) == "martingale"
    g = _row("x", _martingale("XAUUSDT"), frozenset({"XAUUSDT"}))
    assert g["tradfi_share"] == 1.0
    assert oa.klass(g, g["hits"]) == "tradfi"  # перпы акций — раньше любого другого класса


def test_minute_of_hour_event_and_weak():
    f = _row("e", _on_minute(1.0))
    assert f["top_minute"] == 0 and f["top_minute_share"] == 1.0
    assert oa.klass(f, f["hits"]) == "event_M"
    w = _row("w", _on_minute(0.3))
    assert "M_fixed_minute" in w["hits"]
    assert oa.klass(w, w["hits"]) == "M_weak"


def test_admission_too_few_and_hft():
    assert oa.classify(oa.features(_cryptosmx_like()[:29]))[0] == "too_few"
    hft = [_pos(HOUR0 + i * 200_000, 30, 0.001) for i in range(300)]  # 300 сделок за 17 ч
    assert oa.classify(oa.features(hft)) == ("mm_hft", [])
    assert oa.classify({"n": 0}) == ("too_few", [])


def test_heal_drops_half_written_line(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_bytes(b'{"pid": "1"}\n{"pid": "2')
    assert oa.heal(p) is True
    assert p.read_bytes() == b'{"pid": "1"}\n'
    assert oa.heal(p) is False


def test_scan_resumes_and_retries_errors(tmp_path, monkeypatch, capsys):
    pool = [{"leadPortfolioId": pid, "apiKeyTag": "API_KEY_TRADE", "nickname": pid}
            for pid in ("A", "B", "C")]
    pool.append({"leadPortfolioId": "D", "apiKeyTag": None, "nickname": "D"})
    (tmp_path / "bn_list.jsonl").write_text("\n".join(json.dumps(r) for r in pool) + "\n",
                                           encoding="utf-8")
    (tmp_path / "bn_tradfi_symbols.json").write_text(json.dumps({"symbols": []}),
                                                     encoding="utf-8")
    net_down = {"B"}

    def fake_history(pid, max_pages=10):
        assert max_pages == oa.SCAN_PAGES
        if pid in net_down:
            raise urllib.error.URLError("down")
        rows = _cryptosmx_like() if pid != "C" else _cryptosmx_like()[:5]
        return rows, len(rows)

    monkeypatch.setattr(oa, "history", fake_history)
    monkeypatch.setattr(oa, "RETRY_SLEEP_S", ())
    monkeypatch.setattr(oa.time, "sleep", lambda s: None)

    assert oa.cmd_scan(tmp_path) == 0
    scanned = oa.read_jsonl(tmp_path / "bn_scan.jsonl")
    assert {r["pid"] for r in scanned} == {"A", "C"}  # D без метки API в пул не входит
    assert [r["pid"] for r in oa.read_jsonl(tmp_path / "bn_scan_err.jsonl")] == ["B"]
    assert [r["pid"] for r in oa.read_jsonl(tmp_path / "bn_scan_pos.jsonl")] == ["A"]

    with (tmp_path / "bn_scan.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"pid": "Z", "n"')  # процесс убит посреди записи
    net_down.clear()
    assert oa.cmd_scan(tmp_path) == 0
    scanned = oa.read_jsonl(tmp_path / "bn_scan.jsonl")
    assert sorted(r["pid"] for r in scanned) == ["A", "B", "C"]  # B дописан, A и C не повторены
    assert "already done 2; to do 1" in capsys.readouterr().out


def test_shortlist_order_and_exclusions():
    rows = {
        "t": _row("t", _cryptosmx_like()),
        "e": _row("e", _on_minute(1.0)),
        "m": _row("m", _martingale()),
        oa.CRYPTOSMX: _row(oa.CRYPTOSMX, _cryptosmx_like()),
        oa.MIRROR: _row(oa.MIRROR, _on_minute(0.0)),
    }
    # копия бота «t» под другим pid: те же входы — сворачивается в «t»
    rows["t2"] = _row("t2", _cryptosmx_like())
    ent = {pid: oa.entries(oa.closed_rows(_cryptosmx_like() if pid in ("t", "t2", oa.CRYPTOSMX)
                                          else _on_minute(1.0)))
           for pid in rows}
    kept, agg = oa.shortlist(rows, lambda pids: {p: ent[p] for p in pids})
    assert [r["pid"] for r in kept] == [oa.MIRROR, "t", "e"]
    assert kept[1]["copies"] == ["t2"]
    assert kept[0]["klass"] == "no_hit"  # зеркало открывает очередь и без подписи
    assert agg["classes"]["martingale"] == 1
    assert oa.CRYPTOSMX not in {r["pid"] for r in kept}  # контроль, а не находка
