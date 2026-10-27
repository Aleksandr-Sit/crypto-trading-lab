#!/usr/bin/env python
"""Рыночная покупка обвала −2% на ликвидных USDT-перпах Binance: проверка на истории (п.9, №3).

Правило двойников CryptosMX (`docs/research/open-automata-twins-2026-10-27.md`): цена прошла на
2% ниже закрытия прошлой минуты у монеты с оборотом за сутки ≥ 100 млн USDT → рыночная покупка
через 0.25 с → цель +1.4% от входа (сделка строго выше), иначе через 7 мин продажа по bid.
Правила и вердикт заморожены ДО прогона (раздел 4, коммит 748de73); как скрипт решает
неоднозначности исполнения — подраздел «Состояние прогона» там же. Скрипт правила только
исполняет; правка после прогона — новый кандидат, а не эта проверка.

Стадии (каждая при повторе продолжает с места остановки):

    python scripts/twins_check.py events   --out D/is  --from 2025-01 --to 2026-09 --drop 0.015
    python scripts/twins_check.py simulate --out D/is  --per-month 60
    python scripts/twins_check.py tail     --out D/is  --days 2025-10-10,...
    python scripts/twins_check.py report   --out D/is
    # вне выборки — только главная клетка, без перебора:
    python scripts/twins_check.py events   --out D/oos --from 2021-01 --to 2024-12 --drop 0.02
    python scripts/twins_check.py simulate --out D/oos --per-month 60 --main-only
    python scripts/twins_check.py report   --out D/oos --oos

* `events` — минутки ВСЕЙ вселенной USDT-перпов data.binance.vision (с делистнутыми, без
  `TRADIFI_PERPETUAL`), по монете месяц за месяцем: опора — закрытие прошлой минуты (у первой
  минуты месяца — последняя минута прошлого), оборот за 24 ч — сумма оборота минут
  [t − 24 ч, t). Пишутся события ≥ `--drop` на монетах с оборотом ≥ 20 млн и все события ≥ 2%
  (вокруг них контроль не берёт минуты) → `events.jsonl`.
* `simulate` — выборка дней-символов поровну по месяцам (`plan.json`, зерно фиксировано) из
  кадра «событие ≥1.5% на монете ≥20 млн», в два слоя: A — день-символ с событием главной
  клетки, B — остальной кадр. По каждому качается дневная лента aggTrades, и на ОДНОЙ ленте
  разыгрываются главная клетка, соседи (`CELLS`) и контроль главной клетки → `sim.jsonl`.
* `tail` — хвост: ВСЕ дни-символы главной клетки в выбранные дни, каждое событие независимо
  (портфель «до 5 позиций» собирается из них в `report`) → `tail.jsonl`.
* `report` — вердикт по пунктам раздела 4: взвешенное среднее «как у бота» (вес дня-символа —
  обратная вероятность попасть в выборку), шум по дням, месяцы, соседи, контроль парно,
  хвост; `--oos` — только главная клетка.

Исполнение — тот же розыгрыш по ленте, что `crash_bounce_check._play` со стороной сделки (тест
`test_play_matches_crash_bounce` держит совпадение): рыночная покупка — первая сделка
покупателя-агрессора (ask) через задержку после первой сделки ≤ уровня, лимитка — сделка строго
ниже уровня, цель — сделка строго выше, таймер — первая продажа по bid через 7 мин.
Комиссии VIP0: мейкер 0.02%, тейкер 0.05%; тяжёлые — ещё 0.05% на каждую рыночную сторону.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean, median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crash_bounce_check import (  # noqa: E402 — тот же розыгрыш, что у бумажного бота
    ARCHIVE,
    HOLD_S,
    MAKER,
    TAKER,
    _day,
    _first,
    _get,
    _months,
    _rows,
    _trades,
    universe,
)

TRADFI = Path(__file__).resolve().parents[2] / "lab-data/open-automata/bn_tradfi_symbols.json"
DAY_MS, MIN_MS = 86_400_000, 60_000
SLIP = 0.0005  # тяжёлые издержки: проскальзывание на каждую рыночную сторону
ALL_DROP = 0.02  # события ≥2% пишутся на любой монете: контроль обходит их ±30 мин
FRAME_DROP, FRAME_Q = 0.015, 20e6  # кадр выборки: событие ≥1.5% на монете ≥ 20 млн
CTRL_GAP = 30 * MIN_MS
SLOTS, STAKE, TAIL_LOSS = 5, 0.01, -0.10  # хвост: до 5 позиций, 1% D0 на сделку, порог дня
STRESS_DAY = "2025-10-10"

MAIN = "main"
# клетка → (порог обвала, оборот за 24 ч до минуты, вход, задержка с, цель)
CELLS: dict[str, tuple[float, float, str, float, float]] = {
    MAIN: (0.02, 100e6, "market", 0.25, 0.014),
    "drop1.5": (0.015, 100e6, "market", 0.25, 0.014),
    "drop3": (0.03, 100e6, "market", 0.25, 0.014),
    "q20": (0.02, 20e6, "market", 0.25, 0.014),
    "q50": (0.02, 50e6, "market", 0.25, 0.014),
    "lag0.1": (0.02, 100e6, "market", 0.1, 0.014),
    "lag1": (0.02, 100e6, "market", 1, 0.014),
    "limit": (0.02, 100e6, "limit", 0, 0.014),
    "tp1.0": (0.02, 100e6, "market", 0.25, 0.010),
    "tp2.0": (0.02, 100e6, "market", 0.25, 0.020),
}
HEAVY = "heavy"  # главная клетка с проскальзыванием: сосед по издержкам, считается в report
LABEL = {MAIN: "главная: 2%, 100 млн, рынок 0.25 с, цель 1.4%",
         "drop1.5": "порог 1.5%", "drop3": "порог 3%", "q20": "оборот ≥ 20 млн",
         "q50": "оборот ≥ 50 млн", "lag0.1": "рынок через 0.1 с", "lag1": "рынок через 1 с",
         "limit": "лимитка на уровне", "tp1.0": "цель +1.0%", "tp2.0": "цель +2.0%",
         HEAVY: "тяжёлые издержки"}


# ---------------------------------------------------------------- события

def _ms(x) -> int:
    v = int(x)
    return v // 1000 if v > 10 ** 14 else v  # микросекунды в новых архивах — на всякий случай


def _prev_month(m: str) -> str:
    y, mm = map(int, m.split("-"))
    return f"{y - 1:04d}-12" if mm == 1 else f"{y:04d}-{mm - 1:02d}"


def _d0(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def _next_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _tradfi(path: Path) -> set[str]:
    if not path.exists():
        print(f"ВНИМАНИЕ: нет справочника TradFi {path} — перпы акций не исключены", flush=True)
        return set()
    return set(json.loads(path.read_text(encoding="utf-8"))["symbols"])


def month_tail(rows: list[list[str]]) -> list[tuple[int, float, float]]:
    """Последние сутки месяца: (время, оборот, закрытие) — опора первой минуты следующего
    месяца и начало его окна оборота."""
    if not rows:
        return []
    last = _ms(rows[-1][0])
    return [(t, float(r[7]), float(r[4])) for r in rows if (t := _ms(r[0])) >= last - DAY_MS]


def scan_rows(sym: str, rows: list[list[str]], tail: list[tuple[int, float, float]],
              drop: float, keep_q: float = FRAME_Q) -> list[dict]:
    """События месяца: минимум минуты ≤ закрытие прошлой минуты × (1 − drop).

    Прошлая минута — прошлая строка архива (у первой минуты месяца — последняя строка `tail`).
    Оборот за 24 ч до минуты — сумма оборота минут со временем в [t − 24 ч, t): до листинга
    торгов не было, поэтому у молодой монеты это сумма того, что есть. Пишутся события ≥ drop
    на монетах с оборотом ≥ keep_q и все события ≥ ALL_DROP."""
    ts = [x[0] for x in tail]
    qv = [x[1] for x in tail]
    pc, pt = (tail[-1][2], tail[-1][0]) if tail else (None, None)
    j, s, out = 0, sum(qv), []
    for r in rows:
        t = _ms(r[0])
        while j < len(ts) and ts[j] < t - DAY_MS:
            s -= qv[j]
            j += 1
        lo, c, q = float(r[3]), float(r[4]), float(r[7])
        if pc:
            d = lo / pc - 1
            if d <= -drop and (s >= keep_q or d <= -ALL_DROP):
                e = {"sym": sym, "t": t, "prev_close": pc, "open": float(r[1]), "low": lo,
                     "close": c, "drop": d, "qvol_min": q, "qvol_24h": round(s)}
                if t - pt != MIN_MS:
                    e["gap_s"] = (t - pt) // 1000  # прошлой минуты в архиве нет
                out.append(e)
        ts.append(t)
        qv.append(q)
        s += q
        pc, pt = c, t
    return out


def _scan_symbol(sym: str, months: list[str], drop: float) -> tuple[str, list]:
    """Месяцы монеты по порядку: хвост прошлого месяца берётся из уже скачанного, у первого
    месяца задания — отдельной загрузкой. → [(месяц, события | None при сбое сети)]."""
    out, tail, have = [], [], None  # have — месяц, чей хвост лежит в tail
    for m in months:
        url = f"{ARCHIVE}/monthly/klines/{sym}/1m/{sym}-1m-{{}}.zip"
        try:
            if have != _prev_month(m):
                blob = _get(url.format(_prev_month(m)))
                tail = month_tail(_rows(blob)) if blob else []
            blob = _get(url.format(m))
        except RuntimeError as e:
            print(f"  ПРОПУСК {e}", flush=True)
            out.append((m, None))
            have = None
            continue
        if blob is None:  # монеты в этом месяце не было
            out.append((m, []))
            tail, have = [], m
            continue
        rows = _rows(blob)
        out.append((m, scan_rows(sym, rows, tail, drop)))
        tail, have = month_tail(rows), m
    return sym, out


def stage_events(out: Path, a: str, b: str, drop: float, workers: int,
                 tradfi: Path = TRADFI) -> int:
    out.mkdir(parents=True, exist_ok=True)
    meta_p, done_p, ev_p = out / "events_meta.json", out / "events_done.txt", out / "events.jsonl"
    if meta_p.exists() and json.loads(meta_p.read_text(encoding="utf-8"))["drop"] != drop:
        raise SystemExit(f"{meta_p}: порог другой — новый каталог или тот же --drop")
    done = set(done_p.read_text(encoding="utf-8").split()) if done_p.exists() else set()
    tf = _tradfi(tradfi)
    allsyms = universe("monthly")
    syms = [s for s in allsyms if s not in tf]
    meta_p.write_text(json.dumps({"drop": drop, "from": a, "to": b, "symbols": len(syms),
                                  "tradfi_excluded": len(allsyms) - len(syms)}), encoding="utf-8")
    months = _months(a, b)
    jobs = {s: ms for s in syms if (ms := [m for m in months if f"{s}:{m}" not in done])}
    print(f"символов {len(syms)} (TradFi исключено {len(allsyms) - len(syms)}), "
          f"монет с работой {len(jobs)}, готово ранее {len(done)}", flush=True)
    n_ev = n_fail = 0
    # отметки — в UTF-8: у части монет имена иероглифами (cp1251 Windows их не пишет)
    with (ProcessPoolExecutor(workers) as pool, ev_p.open("a", encoding="utf-8") as fe,
          done_p.open("a", encoding="utf-8") as fd):
        futs = {pool.submit(_scan_symbol, s, ms, drop): s for s, ms in jobs.items()}
        for k, f in enumerate(as_completed(futs), 1):
            try:
                sym, res = f.result()
            except Exception as e:  # noqa: BLE001 — процесс упал: монета остаётся несделанной
                print(f"  ПРОПУСК {futs[f]}: {type(e).__name__} {e}", flush=True)
                n_fail += 1
                continue
            for m, ev in res:
                if ev is None:
                    n_fail += 1
                    continue  # не помечать готовым: повтор возьмёт снова
                for e in ev:
                    fe.write(json.dumps(e) + "\n")
                n_ev += len(ev)
                fd.write(f"{sym}:{m}\n")
            fe.flush()
            fd.flush()
            if k % 50 == 0:
                print(f"  {k}/{len(jobs)} монет  событий {n_ev}", flush=True)
    print(f"готово: событий {n_ev}" + (f", не скачалось {n_fail} — повторить тем же запуском"
                                        if n_fail else ""), flush=True)
    return 1 if n_fail else 0


# ---------------------------------------------------------------- розыгрыш по ленте

def _exit(ts, px, sell, e_i: int, entry: float, fee_in: float, tp: float) -> dict:
    """Выход позиции, купленной сделкой e_i по цене entry: цель — сделка строго выше
    entry × (1 + tp), иначе через HOLD_S первая продажа по bid. Лента кончилась — `cut`."""
    t_in = ts[e_i]
    tgt, k, end = entry * (1 + tp), e_i + 1, t_in + HOLD_S * 1000
    low = min(entry, px[e_i])
    while k < len(ts) and ts[k] < end:
        if px[k] > tgt:
            return {"entry": entry, "exit": tgt, "t_in": t_in, "t_out": ts[k], "how": "tp",
                    "net": tp - fee_in - MAKER, "low": low}
        low = min(low, px[k])
        k += 1
    while k < len(ts) and not sell[k]:
        k += 1
    if k >= len(ts):
        return {"entry": entry, "t_in": t_in, "how": "cut"}
    x = px[k]
    return {"entry": entry, "exit": x, "t_in": t_in, "t_out": ts[k], "how": "time",
            "net": x / entry - 1 - fee_in - TAKER, "low": min(low, x)}


def play(ts, px, sell, ev: dict, drop: float, mode: str, lag: float, tp: float,
         start: int | None = None) -> dict | None:
    """Одна сделка по событию (минута `ev["t"]`). None — уровень в минуте не пройден.

    `start` — с какого момента искать касание (позиция по монете закрылась внутри минуты):
    бот проверяет условие на каждой сделке, и первая сделка ≤ уровня после выхода — вход.
    Рынок: касание — сделка ≤ уровня, вход — первая покупка по ask через `lag`. Лимитка:
    касание — сделка строго ниже уровня, вход по уровню, мейкер."""
    level = ev["prev_close"] * (1 - drop)
    m1 = ev["t"] + MIN_MS
    i = _first(ts, ev["t"] if start is None else start)
    trig = None
    while i < len(ts) and ts[i] < m1:
        if (px[i] < level) if mode == "limit" else (px[i] <= level):
            trig = i
            break
        i += 1
    if trig is None:
        return None
    if mode == "limit":
        return _exit(ts, px, sell, trig, level, MAKER, tp)
    e_i = _first(ts, ts[trig] + round(lag * 1000), trig + (1 if lag == 0 else 0))
    while e_i < len(ts) and sell[e_i]:
        e_i += 1
    if e_i >= len(ts):
        return {"how": "cut"}
    return _exit(ts, px, sell, e_i, px[e_i], TAKER, tp)


def control_minutes(lo_m: int, hi_m: int, excl: list[int]) -> list[int]:
    """Минуты [lo_m, hi_m] (начала, мс), не ближе 30 мин к событию ≥2% монеты (`excl`)."""
    ex, out, j = sorted(excl), [], 0
    for m in range(lo_m, hi_m + 1, MIN_MS):
        while j < len(ex) and ex[j] < m - CTRL_GAP:
            j += 1
        if j < len(ex) and ex[j] <= m + CTRL_GAP:
            continue
        out.append(m)
    return out


def control_moment(allowed: list[int], seed: int, sym: str, t_ev: int) -> int | None:
    """Случайная минута из `allowed` и случайная миллисекунда в ней; зерно — событие."""
    if not allowed:
        return None
    rng = random.Random(f"{seed}:ctrl:{sym}:{t_ev}")
    return rng.choice(allowed) + rng.randrange(MIN_MS)


def control_play(ts, px, sell, t0: int, tp: float) -> dict:
    """Рыночная покупка в момент t0 (первая покупка по ask), те же выходы, что у клетки."""
    e_i = _first(ts, t0)
    while e_i < len(ts) and sell[e_i]:
        e_i += 1
    if e_i >= len(ts):
        return {"how": "cut"}
    return _exit(ts, px, sell, e_i, px[e_i], TAKER, tp)


def in_cell(e: dict, cell: tuple) -> bool:
    return e["drop"] <= -cell[0] and e["qvol_24h"] >= cell[1]


def sim_day(sym: str, day: str, evs: list[dict], excl: list[int], names: tuple[str, ...],
            seed: int, ctrl: bool = True, independent: bool = False) -> list[dict]:
    """Все клетки `names` и контроль главной на одной ленте дня-символа.

    Одна позиция на монету и клетку: касание во время позиции пропускается, после выхода —
    ищется заново с первой сделки после выхода (в той же минуте тоже). `independent` — каждое
    событие отдельно (хвост: портфель отбирает сам). Контроль — на каждую сделку главной
    клетки: случайная минута того же дня вне ±30 мин от событий ≥2% (`excl`), случайная
    миллисекунда в ней, рыночная покупка, те же выходы."""
    ts, px, _, sell = _trades(sym, day)
    if not ts:
        return [{"sym": sym, "day": day, "missing": True}]
    d0 = _d0(day)
    lo_m = max(d0, ts[0] // MIN_MS * MIN_MS)
    hi_m = min(d0 + DAY_MS - MIN_MS, ts[-1] // MIN_MS * MIN_MS)
    ctrl_t: dict[int, int | None] = {}
    if ctrl:
        allowed = control_minutes(lo_m, hi_m, excl)
        ctrl_t = {e["t"]: control_moment(allowed, seed, sym, e["t"])
                  for e in evs if in_cell(e, CELLS[MAIN])}
    need = max([e["t"] + MIN_MS for e in evs] + [t for t in ctrl_t.values() if t is not None])
    if need + HOLD_S * 1000 + MIN_MS > ts[-1]:
        t2, p2, _, s2 = _trades(sym, _next_day(day))  # сделка заходит за полночь
        ts, px, sell = ts + t2, px + p2, sell + s2
    rows = []
    for name in names:
        cell = CELLS[name]
        drop, _, mode, lag, tp = cell
        busy = 0
        for e in sorted(evs, key=lambda x: x["t"]):
            if not in_cell(e, cell):
                continue
            start = e["t"] if independent else max(e["t"], busy + 1)
            if start >= e["t"] + MIN_MS:
                continue  # позиция открыта всю минуту события
            r = play(ts, px, sell, e, drop, mode, lag, tp, start)
            if r is None:
                continue
            rows.append({"sym": sym, "day": day, "var": name, "t": e["t"], "drop_min": e["drop"],
                         "qvol_24h": e["qvol_24h"], **r})
            if r["how"] == "cut":
                continue
            busy = r["t_out"]
            if name == MAIN and ctrl:
                c = ctrl_t.get(e["t"])
                rc = control_play(ts, px, sell, c, tp) if c is not None else {"how": "none"}
                rows.append({"sym": sym, "day": day, "var": "ctrl", "t": e["t"], "t_ctrl": c,
                             **rc})
    return rows


# ---------------------------------------------------------------- выборка

def load_events(out: Path) -> list[dict]:
    with (out / "events.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(x) for x in fh]


def strata(evs: list[dict], main_only: bool) -> dict[tuple[str, str], str]:
    """День-символ → слой: A — есть событие главной клетки, B — остальной кадр
    (событие ≥1.5% на монете ≥ 20 млн). `main_only` — только A (вне выборки)."""
    st: dict[tuple[str, str], str] = {}
    for e in evs:
        k = (e["sym"], _day(e["t"]))
        if in_cell(e, CELLS[MAIN]):
            st[k] = "A"
        elif not main_only and e["drop"] <= -FRAME_DROP and e["qvol_24h"] >= FRAME_Q:
            st.setdefault(k, "B")
    return st


def make_plan(st: dict[tuple[str, str], str], per_month: int, seed: int,
              main_only: bool) -> dict:
    """Поровну по месяцам: `per_month` дней-символов, слой A — половина (меньше, если A
    столько нет; больше, если не хватает B), своё зерно на месяц и слой — добавление месяцев
    не меняет уже выбранных. Вес — обратная вероятность попасть в выборку: N/n слоя месяца."""
    months: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"A": [], "B": []})
    for (sym, day), h in st.items():
        months[day[:7]][h].append(f"{sym}:{day}")
    plan = {}
    for m, g in sorted(months.items()):
        pop_a, pop_b = sorted(g["A"]), sorted(g["B"])
        if main_only:
            n_a, n_b = min(len(pop_a), per_month), 0
        else:
            n_a = min(len(pop_a), max(per_month // 2, per_month - len(pop_b)))
            n_b = min(len(pop_b), per_month - n_a)
        plan[m] = {"N": {"A": len(pop_a), "B": len(pop_b)},
                   "keys": {"A": sorted(random.Random(f"{seed}:{m}:A").sample(pop_a, n_a)),
                            "B": sorted(random.Random(f"{seed}:{m}:B").sample(pop_b, n_b))}}
    return plan


def plan_weights(plan: dict) -> dict[tuple[str, str], float]:
    w = {}
    for p in plan.values():
        for h in ("A", "B"):
            for k in p["keys"][h]:
                sym, day = k.rsplit(":", 1)
                w[(sym, day)] = p["N"][h] / len(p["keys"][h])
    return w


def _excl_index(evs: list[dict]) -> dict[str, list[int]]:
    ex = defaultdict(list)
    for e in evs:
        if e["drop"] <= -ALL_DROP:
            ex[e["sym"]].append(e["t"])
    return {s: sorted(v) for s, v in ex.items()}


def _excl(index: dict[str, list[int]], sym: str, day: str) -> list[int]:
    v, d0 = index.get(sym, []), _d0(day)
    return v[bisect_left(v, d0 - CTRL_GAP):bisect_right(v, d0 + DAY_MS + CTRL_GAP)]


def _run(jobs, worker_args, workers: int, res_p: Path, done_p: Path) -> int:
    """Один проход по дням-символам → сколько не удалось (не помечены готовыми)."""
    failed = 0
    with (ProcessPoolExecutor(workers) as pool, res_p.open("a", encoding="utf-8") as fs,
          done_p.open("a", encoding="utf-8") as fd):
        futs = {pool.submit(sim_day, s, d, *worker_args(s, d)): (s, d) for s, d in jobs}
        for k, f in enumerate(as_completed(futs), 1):
            s, d = futs[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001 — сеть или память: день не теряется
                print(f"  ПРОПУСК {s} {d}: {type(e).__name__} {e}", flush=True)
                failed += 1
                continue
            for r in res:
                fs.write(json.dumps(r) + "\n")
            fs.flush()
            fd.write(f"{s}:{d}\n")
            fd.flush()
            if k % 20 == 0:
                print(f"  {k}/{len(jobs)}  {datetime.now():%H:%M}", flush=True)
    return failed


def _passes(jobs_left, run_once, passes: int) -> None:
    for p in range(1, passes + 1):
        jobs = jobs_left()
        print(f"проход {p}/{passes}: осталось {len(jobs)}", flush=True)
        if not jobs:
            break
        failed = run_once(jobs)
        if not failed or failed == len(jobs) or p == passes:
            if failed:
                print(f"не скачалось {failed} — повторить тем же запуском", flush=True)
            break
        print(f"не скачалось {failed}; следующий проход через 5 мин", flush=True)
        time.sleep(300)
    print("готово", flush=True)


def stage_simulate(out: Path, workers: int, per_month: int, seed: int, main_only: bool,
                   passes: int) -> int:
    meta = json.loads((out / "events_meta.json").read_text(encoding="utf-8"))
    if not main_only and meta["drop"] > FRAME_DROP:
        raise SystemExit(f"события с порога {meta['drop']}: соседу 1.5% нужен --drop 0.015")
    evs = load_events(out)
    st = strata(evs, main_only)
    plan = make_plan(st, per_month, seed, main_only)
    plan_p = out / "plan.json"
    if plan_p.exists() and json.loads(plan_p.read_text(encoding="utf-8")) != plan:
        raise SystemExit("выборка не совпадает с plan.json: события изменились — новый каталог")
    plan_p.write_text(json.dumps(plan), encoding="utf-8")
    keep = set(plan_weights(plan))
    by = defaultdict(list)
    for e in evs:
        if (k := (e["sym"], _day(e["t"]))) in keep:
            by[k].append(e)
    ex = _excl_index(evs)
    names = (MAIN,) if main_only else tuple(CELLS)
    n_a = sum(h == "A" for h in st.values())
    print(f"событий {len(evs)}, дней-символов в кадре {len(st)} (A {n_a}), в выборке {len(keep)}",
          flush=True)
    done_p, sim_p = out / "sim_done.txt", out / "sim.jsonl"

    def left():
        done = set(done_p.read_text(encoding="utf-8").split()) if done_p.exists() else set()
        return [k for k in sorted(keep, key=lambda k: (k[1], k[0])) if f"{k[0]}:{k[1]}" not in done]

    _passes(left, lambda jobs: _run(jobs, lambda s, d: (by[(s, d)], _excl(ex, s, d), names,
                                                        seed, True, False),
                                    workers, sim_p, done_p), passes)
    return 0


def tail_days(evs: list[dict], top: int, deep: int, a: str = "", b: str = "9") -> list[str]:
    """10.10.2025 (если в окне) + `top` дней с наибольшим числом событий главной клетки +
    `deep` дней с наибольшим числом её событий ≥5% (ширина и глубина обвала — где портфель
    теряет больше всего)."""
    cnt, cnt_deep = defaultdict(int), defaultdict(int)
    for e in evs:
        if in_cell(e, CELLS[MAIN]) and a <= (d := _day(e["t"])) <= b:
            cnt[d] += 1
            cnt_deep[d] += e["drop"] <= -0.05
    days = {STRESS_DAY} & set(cnt)
    days |= {d for d, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:top]}
    days |= {d for d, _ in sorted(cnt_deep.items(), key=lambda kv: (-kv[1], kv[0]))[:deep]}
    return sorted(days)


def stage_tail(out: Path, days: list[str], workers: int, seed: int, passes: int) -> int:
    evs = load_events(out)
    by = defaultdict(list)
    for e in evs:
        if (d := _day(e["t"])) in days and in_cell(e, CELLS[MAIN]):
            by[(e["sym"], d)].append(e)
    print(f"дни хвоста {', '.join(days)}: дней-символов {len(by)}", flush=True)
    done_p, res_p = out / "tail_done.txt", out / "tail.jsonl"

    def left():
        done = set(done_p.read_text(encoding="utf-8").split()) if done_p.exists() else set()
        return [k for k in sorted(by, key=lambda k: (k[1], k[0])) if f"{k[0]}:{k[1]}" not in done]

    _passes(left, lambda jobs: _run(jobs, lambda s, d: (by[(s, d)], [], (MAIN,), seed, False,
                                                        True),
                                    workers, res_p, done_p), passes)
    return 0


# ---------------------------------------------------------------- отчёт

def wstats(rows: list[dict], w=None) -> tuple[int, int, float, float, float]:
    """n, дней, среднее, шум (кластерная ошибка по дням), t; `w(row)` — вес (нет — 1)."""
    if not rows:
        return 0, 0, math.nan, math.nan, math.nan
    wt = [w(r) if w else 1.0 for r in rows]
    tw = sum(wt)
    mean = sum(x * r["net"] for x, r in zip(wt, rows, strict=True)) / tw
    days = defaultdict(float)
    for x, r in zip(wt, rows, strict=True):
        days[r["day"]] += x * (r["net"] - mean)
    g = len(days)
    if g < 2:
        return len(rows), g, mean, math.nan, math.nan
    se = math.sqrt(sum(v * v for v in days.values()) * g / (g - 1)) / tw
    return len(rows), g, mean, se, (mean / se if se else math.nan)


def heavy(r: dict) -> dict:
    """Главные издержки → тяжёлые: +SLIP на рыночный вход и на выход по таймеру."""
    mkt_in = CELLS[r["var"]][2] == "market" if r["var"] in CELLS else True
    return {**r, "net": r["net"] - SLIP * mkt_in - SLIP * (r["how"] == "time")}


def portfolio(rows: list[dict], slots: int = SLOTS) -> tuple[list[dict], int, int]:
    """Портфель «до `slots` позиций, одна на монету» из независимо разыгранных событий:
    по времени входа; занято — пропуск. → (взятые, пропущено за слоты, за монету)."""
    rows = sorted((r for r in rows if r["how"] in ("tp", "time")),
                  key=lambda r: (r["t_in"], r["sym"]))
    open_: dict[str, int] = {}
    taken, by_slot, by_coin = [], 0, 0
    for r in rows:
        for s in [s for s, t in open_.items() if t <= r["t_in"]]:
            del open_[s]
        if r["sym"] in open_:
            by_coin += 1
        elif len(open_) >= slots:
            by_slot += 1
        else:
            open_[r["sym"]] = r["t_out"]
            taken.append(r)
    return taken, by_slot, by_coin


def _pct(x: float, nd: int = 3) -> str:
    return "—" if x is None or math.isnan(x) else f"{x * 100:+.{nd}f}"


def _t(x: float) -> str:
    return "—" if x is None or math.isnan(x) else f"{x:+.1f}"


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(x) for x in fh]


def stage_report(out: Path, oos: bool) -> int:
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    w = plan_weights(plan)
    sims = _load(out / "sim.jsonl")
    done = set((out / "sim_done.txt").read_text(encoding="utf-8").split())
    miss = [s for s in sims if s.get("missing")]
    sims = [s for s in sims if not s.get("missing")]
    cut = defaultdict(int)
    by = defaultdict(list)
    for s in sims:
        if s["how"] == "cut":
            cut[s["var"]] += 1
        elif s["how"] != "none":
            by[s["var"]].append(s)
    by[HEAVY] = [heavy(r) for r in by[MAIN]]

    def wf(r):
        return w[(r["sym"], r["day"])]

    meta = json.loads((out / "events_meta.json").read_text(encoding="utf-8"))
    n_a = sum(p["N"]["A"] for p in plan.values())
    n_b = sum(p["N"]["B"] for p in plan.values())
    s_a = sum(len(p["keys"]["A"]) for p in plan.values())
    s_b = sum(len(p["keys"]["B"]) for p in plan.values())
    print(f"# Проверка «обвал −2% рынком на ликвидных» ({'вне выборки' if oos else 'выборка'}) "
          f"{min(plan)}…{max(plan)}")
    print(f"вселенная: {meta['symbols']} USDT-перпов с делистнутыми (TradFi исключено "
          f"{meta['tradfi_excluded']}); события с порога {meta['drop']:.3f}")
    print(f"дней-символов в кадре: слой A (событие главной клетки) {n_a}, слой B {n_b}; "
          f"в выборке A {s_a}, B {s_b}; разобрано {len(done)}; без ленты {len(miss)}")
    print("недоигранных сделок (лента кончилась раньше выхода): "
          + (", ".join(f"{k} {v}" for k, v in sorted(cut.items())) or "0"))
    print("веса слоя A по месяцам: " + ", ".join(
        f"{m[2:]} {p['N']['A'] / max(len(p['keys']['A']), 1):.1f}"
        for m, p in sorted(plan.items())))

    names = (MAIN, HEAVY) if oos else (MAIN, *[n for n in CELLS if n != MAIN], HEAVY)
    print("\n## клетки: итог сделки после издержек, % (шум — по дням; «взвеш.» — как у бота)")
    print(f"{'клетка':<9} {'':<44} {'n':>6} {'дней':>5} {'ср.':>7} {'t':>5} | {'взвеш.':>7} "
          f"{'шум':>6} {'t':>5} {'плюс':>5} {'цель':>5}")
    res = {}
    for n in names:
        rows = by[n]
        k, g, m, _, t = wstats(rows)
        _, _, mw, sew, tw = wstats(rows, wf)
        res[n] = (mw, tw, k)
        win = sum(r["net"] > 0 for r in rows) / k if k else math.nan
        tp = sum(r["how"] == "tp" for r in rows) / k if k else math.nan
        share = (f"{win:>5.0%} {tp:>5.0%}" if k else f"{'—':>5} {'—':>5}")
        print(f"{n:<9} {LABEL[n]:<44} {k:>6} {g:>5} {_pct(m):>7} {_t(t):>5} | {_pct(mw):>7} "
              f"{_pct(sew):>6} {_t(tw):>5} {share}")

    main = by[MAIN]
    months = defaultdict(list)
    for r in main:
        months[r["day"][:7]].append(r)
    print("\n## главная клетка по месяцам (среднее, %; в скобках сделок)")
    pos = 0
    for m in sorted(plan):
        rows = months.get(m, [])
        if rows:
            k, g, mean, se, t = wstats(rows)
            pos += mean > 0
            print(f"{m}  {_pct(mean):>7} ({k:>4}, дней {g:>3})  t {_t(t)}")
        else:
            print(f"{m}  нет сделок")
    n_m = len(months)
    print(f"месяцев в плюсе: {pos} из {n_m} с сделками (месяцев в окне {len(plan)})")

    by_year = defaultdict(list)
    for r in main:
        by_year[r["day"][:4]].append(r)
    print("по годам (взвешенно): " + ", ".join(
        f"{y} {_pct(wstats(v, wf)[2], 2)} ({len(v)})" for y, v in sorted(by_year.items())))
    calm = [r for r in main if not r["day"].startswith("2025-10")]
    if len(calm) != len(main):
        _, _, mc, _, tc = wstats(calm, wf)
        print(f"без октября 2025 (взвешенно): {_pct(mc)}%, t {_t(tc)}")
    hows = defaultdict(int)
    for r in main:
        hows[r["how"]] += 1
    if main:
        print(f"выходы: цель {hows['tp']}, таймер {hows['time']}; медиана итога "
              f"{_pct(median(r['net'] for r in main))}%, худшая сделка "
              f"{_pct(min(r['net'] for r in main), 2)}%")

    # контроль: парно по одним и тем же событиям, шум разности по дням
    ctrl = {(r["sym"], r["t"]): r for r in by["ctrl"]}
    pairs = [(r, ctrl[(r["sym"], r["t"])]) for r in main if (r["sym"], r["t"]) in ctrl]
    diff = [{"sym": a["sym"], "day": a["day"], "net": a["net"] - c["net"]} for a, c in pairs]
    no_ctrl = sum(1 for s in sims if s["var"] == "ctrl" and s["how"] in ("none", "cut"))
    _, _, mc, _, tc = wstats([c for _, c in pairs], wf)
    k, g, md, sed, td = wstats(diff, wf)
    _, _, mdu, _, tdu = wstats(diff)
    print("\n## контроль: случайная минута того же дня вне ±30 мин от событий ≥2%, "
          "рыночная покупка в случайную секунду, те же выходы")
    print(f"пар {k} (дней {g}; без контроля {no_ctrl}); контроль взвешенно {_pct(mc)}% "
          f"(t {_t(tc)})")
    print(f"главная − контроль: взвешенно {_pct(md)} п. ± {_pct(sed)} (t {_t(td)}); "
          f"без весов {_pct(mdu)} п. (t {_t(tdu)})")

    print("\n## хвост")
    stress = [r for r in main if r["day"] == STRESS_DAY]
    if stress:
        print(f"{STRESS_DAY} в выборке: главная — {len(stress)} сделок, сумма "
              f"{_pct(sum(r['net'] for r in stress), 1)}%, среднее "
              f"{_pct(fmean(r['net'] for r in stress), 2)}%, худшая "
              f"{_pct(min(r['net'] for r in stress), 2)}%")
        for n in names[1:]:
            v = [r for r in by[n] if r["day"] == STRESS_DAY]
            if v:
                print(f"    {n:<8} {len(v):>4} сделок, сумма {_pct(sum(r['net'] for r in v), 1)}%")
    day_sum = defaultdict(list)
    for r in main:
        day_sum[r["day"]].append(r["net"])
    worst = sorted(day_sum.items(), key=lambda kv: sum(kv[1]))[:5]
    print("пять худших дней выборки (главная, сумма итогов сделок, %): " + "; ".join(
        f"{d} {_pct(sum(v), 1)} ({len(v)})" for d, v in worst))

    tail = [r for r in _load(out / "tail.jsonl") if not r.get("missing")]
    tail_ok = None
    if tail:
        print(f"\nпортфель «до {SLOTS} позиций, {STAKE:.0%} D0 на сделку» на днях хвоста "
              f"(все дни-символы главной клетки, ×1):")
        print(f"{'день':<11} {'событий':>7} {'взято':>6} {'слоты':>6} {'монета':>6} "
              f"{'день, % D0':>10} {'худший момент':>13} {'худшая сделка':>13}")
        per_day = defaultdict(list)
        for r in tail:
            per_day[r["day"]].append(r)
        worst_day = math.inf
        for d in sorted(per_day):
            v = per_day[d]
            taken, by_slot, by_coin = portfolio(v)
            pnl = sum(r["net"] for r in taken) * STAKE
            run = low = 0.0
            for r in sorted(taken, key=lambda r: r["t_out"]):
                run += r["net"] * STAKE
                low = min(low, run)
            worst_day = min(worst_day, pnl)
            wt = min((r["net"] for r in taken), default=math.nan)
            print(f"{d:<11} {len(v):>7} {len(taken):>6} {by_slot:>6} {by_coin:>6} "
                  f"{_pct(pnl, 2):>10} {_pct(low, 2):>13} {_pct(wt, 1):>13}")
        tail_ok = worst_day > TAIL_LOSS
        print(f"худший день {_pct(worst_day, 2)}% D0 — порог {TAIL_LOSS:.0%} D0: "
              f"{'в пределах' if tail_ok else 'FAILED'}")

    print("\n## вердикт (раздел 4)")
    mw, tw, _ = res[MAIN]
    ok_mean = mw > 0 and tw >= 2
    if oos:
        print(f"вне выборки: итог {_pct(mw)}% (t {_t(tw)}) — "
              f"{'прошла' if ok_mean else 'не прошла'} (нужно > 0 и t ≥ 2)")
        return 0
    ok_months = n_m > 0 and pos >= 2 * n_m / 3
    nb = [n for n in names if n != MAIN]
    ok_plateau = all(res[n][0] > 0 for n in nb) if mw > 0 else False
    same_sign = all(not math.isnan(res[n][0]) and (res[n][0] > 0) == (mw > 0) for n in nb)
    ok_ctrl = md > 0 and td > 2
    print(f"1. итог > 0 и t ≥ 2 (взвешенно): {_pct(mw)}%, t {_t(tw)} — "
          f"{'да' if ok_mean else 'нет'}")
    print(f"2. месяцев в плюсе ≥ 2/3: {pos} из {n_m} — {'да' if ok_months else 'нет'}")
    print(f"3. соседи того же знака: {', '.join(f'{n} {_pct(res[n][0], 2)}' for n in nb)} — "
          f"{'да' if same_sign else 'нет'}")
    print(f"4. обходит контроль больше чем на 2σ: {_pct(md)} п., t {_t(td)} — "
          f"{'да' if ok_ctrl else 'нет'}")
    tail_txt = ("нет расчёта портфеля" if tail_ok is None
                else "в пределах" if tail_ok else "FAILED")
    print(f"5. хвост: {tail_txt}")
    passed = ok_mean and ok_months and ok_plateau and ok_ctrl and tail_ok is True
    print(f"ИТОГ выборки: {'passed (дальше — вне выборки)' if passed else 'failed'}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("events")
    e.add_argument("--out", type=Path, required=True)
    e.add_argument("--from", dest="a", required=True)
    e.add_argument("--to", dest="b", required=True)
    e.add_argument("--drop", type=float, default=FRAME_DROP)
    e.add_argument("--workers", type=int, default=8)
    e.add_argument("--tradfi", type=Path, default=TRADFI)
    s = sub.add_parser("simulate")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--per-month", type=int, default=60)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--main-only", action="store_true", help="только главная клетка (вне выборки)")
    s.add_argument("--passes", type=int, default=3)
    t = sub.add_parser("tail")
    t.add_argument("--out", type=Path, required=True)
    t.add_argument("--days", help="дни через запятую; без них — по --top/--deep")
    t.add_argument("--top", type=int, default=5)
    t.add_argument("--deep", type=int, default=5)
    t.add_argument("--list", action="store_true", help="только напечатать дни и выйти")
    t.add_argument("--seed", type=int, default=7)
    t.add_argument("--workers", type=int, default=6)
    t.add_argument("--passes", type=int, default=3)
    r = sub.add_parser("report")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--oos", action="store_true")
    a = ap.parse_args()
    if a.cmd == "events":
        return stage_events(a.out, a.a, a.b, a.drop, a.workers, a.tradfi)
    if a.cmd == "simulate":
        return stage_simulate(a.out, a.workers, a.per_month, a.seed, a.main_only, a.passes)
    if a.cmd == "tail":
        if a.days:
            days = a.days.split(",")
        else:
            evs = load_events(a.out)
            days = tail_days(evs, a.top, a.deep)
            cnt = defaultdict(lambda: [0, 0])
            for ev in evs:
                if in_cell(ev, CELLS[MAIN]) and _day(ev["t"]) in days:
                    cnt[_day(ev["t"])][0] += 1
                    cnt[_day(ev["t"])][1] += ev["drop"] <= -0.05
            for d in days:
                print(f"{d}: событий главной клетки {cnt[d][0]}, из них ≥5% {cnt[d][1]}")
        if a.list:
            return 0
        return stage_tail(a.out, days, a.workers, a.seed, a.passes)
    return stage_report(a.out, a.oos)


if __name__ == "__main__":
    raise SystemExit(main())
