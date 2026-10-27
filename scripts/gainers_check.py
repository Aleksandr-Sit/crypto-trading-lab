#!/usr/bin/env python
"""Шорт лидеров роста за сутки на альт-перпах Binance: проверка на истории (п.9, автомат №2).

Правила и вердикт заморожены ДО прогона — `docs/research/open-automata-dhz-2026-10-26.md`,
раздел 4 (коммит 988ee95); как скрипт разрешает неоднозначности таблицы — там же, подраздел
«Состояние прогона». Скрипт правила только исполняет; правка после прогона — новый кандидат.

Стадии (каждая при повторе продолжает с места остановки):

    python scripts/gainers_check.py fetch    --data D --from 2020-12 --to 2026-09 \
                                             --spill 2026-10-01,2026-10-02
    python scripts/gainers_check.py select   --data D --out D/is --from 2025-01 --to 2026-09
    python scripts/gainers_check.py fetch    --data D --funding-for D/is [--scope all|main]
    python scripts/gainers_check.py simulate --data D --out D/is [--scope all|main]
    python scripts/gainers_check.py report   --out D/is [--oos]

* `fetch` — месячные архивы 1h ВСЕХ USDT-перпов data.binance.vision (с делистнутыми, без
  `TRADIFI_PERPETUAL`): открытие, максимум, минимум, закрытие, оборот → `D/cache/1h/<месяц>.jsonl`;
  `--spill` — дневные архивы за дни после последнего месяца (удержание последних запусков).
  С `--funding-for OUT` — ставки `fundingRate` монет, попавших в списки OUT/lists.jsonl, за месяцы
  их удержаний → `D/cache/funding/<месяц>.jsonl` (нет архива — записывается `null`).
* `select` — на каждый час запуска 00/08/16 UTC: рост за 24 ч = закрытие бара T−1 ч к закрытию
  бара T−25 ч, оборот за сутки = сумма 24 баров T−24…T−1 ч, нужны оба бара и бар входа T → топ-20
  при обороте ≥ 5/10/25 млн USDT, случайные 10 фильтра 10 млн (зерно — `--seed` и время запуска:
  добавление месяцев не меняет уже посчитанных), в 08:00 — вся вселенная фильтра 10 млн.
* `simulate` — шорт каждой нужной монеты (`short_leg`) на 12/24/48 ч → `sim.jsonl`.
  `--scope all` — клетки `CELLS`; `--scope main` — только главная (вне выборки, без перебора).
* `report` — вердикт по пунктам раздела 4: итог запуска = среднее по монетам (в % порции =
  в % капитала, сумма порций = капитал), t по запускам и по неделям, месяцы, плато, контроль
  «случайные 10», хвост (худшие запуски, 10.10.2025, ликвидации, просадка кривой D0).

Исполнение (`short_leg`): шорт по открытию бара T × (1 − проскальзывание), откуп по закрытию
последнего бара удержания × (1 + проскальзывание); тейкер на обе стороны. Ликвидация ×3
изолированно: максимум часа ≥ вход × (1 + 1/3 − 0.5%) → −1/3 порции минус комиссия входа.
Фандинг — выплаты строго внутри (T, T + H) по архиву ставок: шорт получает положительную ставку.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from array import array
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import fmean, median

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grid4h_check import (  # noqa: E402 — общие помощники загрузки и дат
    ARCHIVE,
    TRADFI,
    _day,
    _dt,
    _get,
    _lines,
    _month,
    _months,
    _ms,
    _next_month,
    _prev_month,
    _sym_months,
    _tradfi,
    _ts,
    tstat,
    universe,
)

H1, D1 = 3_600_000, 86_400_000
HOURS = (0, 8, 16)
FILTERS = {"5": 5e6, "10": 10e6, "25": 25e6}  # оборот за 24 ч, USDT
MAIN_F = "10"
N_TOP, N_RND = 20, 10
HOLD_MAX = 48
COSTS = {"main": (0.0005, 0.0005), "heavy": (0.0005, 0.0020)}  # (тейкер, проскальзывание)
LEV, MM = 3, 0.005  # ×3 изолированно; ликвидация на уходе 1/3 − 0.5% против входа
NAN = float("nan")
CRASH = _ts("2025-10") + (9 * 24 + 21) * H1  # 10.10.2025 21:00 UTC

# клетка → (час запуска, выбор: фильтр | rnd | uni, N, удержание в часах)
CELLS: dict[str, tuple[int, str, int, int]] = {
    "main": (8, "10", 10, 24),
    "N5": (8, "10", 5, 24), "N20": (8, "10", 20, 24),
    "H12": (8, "10", 10, 12), "H48": (8, "10", 10, 48),
    "h00": (0, "10", 10, 24), "h16": (16, "10", 10, 24),
    "f5": (8, "5", 10, 24), "f25": (8, "25", 10, 24),
    "rnd10": (8, "rnd", 10, 24), "uni": (8, "uni", 0, 24),
}
PLATEAU = ("N5", "N20", "H12", "H48", "h00", "h16")
# значения монето-запуска: итог и ликвидация (главные издержки, тяжёлые), фандинг, выплат,
# ставки известны, обрыв данных (делистинг), ход цены до издержек
NET, LIQ, NET_H, LIQ_H, FUND, NPAY, KNOWN, CUT, GROSS = range(9)


def _read(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def _r8(x: bytes) -> float:
    return float(f"{float(x):.8g}")


# ---------------------------------------------------------------- fetch

def _kl_job(sym: str, period: str) -> tuple[str, str, list | None]:
    """Часовой архив монеты: `period` — месяц (ГГГГ-ММ) или день (ГГГГ-ММ-ДД)."""
    kind = "monthly" if len(period) == 7 else "daily"
    blob = _get(f"{ARCHIVE}/{kind}/klines/{sym}/1h/{sym}-1h-{period}.zip")
    if blob is None:
        return sym, period, None
    rows = {_ms(f[0]): [_ms(f[0]), _r8(f[1]), _r8(f[2]), _r8(f[3]), _r8(f[4]), round(float(f[7]))]
            for f in _lines(blob)}
    return sym, period, [rows[t] for t in sorted(rows)]


def _fund_job(sym: str, month: str) -> tuple[str, str, list | None]:
    """Ставки за месяц: [время выплаты, округлённое до минуты; ставка]. None — архива нет."""
    blob = _get(f"{ARCHIVE}/monthly/fundingRate/{sym}/{sym}-fundingRate-{month}.zip")
    if blob is None:
        return sym, month, None
    return sym, month, sorted([(_ms(f[0]) + 30_000) // 60_000 * 60_000, float(f[2])]
                              for f in _lines(blob))


def _download(jobs: list[tuple[str, str]], fn: Callable, dest: Path, workers: int) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    done_p = dest / "done.txt"
    done = set(done_p.read_text(encoding="utf-8").split()) if done_p.exists() else set()
    todo = [(s, p) for s, p in jobs if f"{s}:{p}" not in done]
    print(f"{dest.name}: архивов {len(jobs)}, к загрузке {len(todo)}", flush=True)
    fail, t0 = 0, time.time()
    files: dict[str, object] = {}
    with ThreadPoolExecutor(workers) as pool, done_p.open("a", encoding="utf-8") as fd:
        futs = [pool.submit(fn, s, p) for s, p in todo]
        for k, f in enumerate(as_completed(futs), 1):
            try:
                s, p, rows = f.result()
            except RuntimeError as e:
                print(f"  ПРОПУСК {e}", flush=True)
                fail += 1
                continue
            m = p[:7]
            if m not in files:
                files[m] = (dest / f"{m}.jsonl").open("a", encoding="utf-8")
            files[m].write(json.dumps({"s": s, "p": p, "b": rows}, separators=(",", ":")) + "\n")
            files[m].flush()
            fd.write(f"{s}:{p}\n")
            fd.flush()
            if k % 1000 == 0:
                el = (time.time() - t0) / 60
                print(f"  {k}/{len(todo)}  {el:.1f} мин, осталось ≈{el / k * (len(todo) - k):.0f}",
                      flush=True)
    for fh in files.values():
        fh.close()
    if fail:
        print(f"не скачалось {fail} — повторить тем же запуском", flush=True)
    return fail


def stage_fetch(data: Path, a: str | None, b: str | None, spill: list[str], workers: int,
                tradfi: Path, funding_for: Path | None, scope: str) -> int:
    cache = data / "cache"
    if funding_for is not None:
        need = needed(_read(funding_for / "lists.jsonl"), scope)
        pairs = {(s, m) for (s, t), holds in need.items()
                 for m in (_month(t), _month(t + max(holds) * H1 - 1))}
        return 1 if _download(sorted(pairs, key=lambda x: (x[1], x[0])), _fund_job,
                              cache / "funding", workers) else 0
    tf = _tradfi(tradfi)
    mp = cache / "months_1h.json"
    if mp.exists():
        avail = json.loads(mp.read_text(encoding="utf-8"))
    else:
        cache.mkdir(parents=True, exist_ok=True)
        syms = universe()
        print(f"вселенная: {len(syms)} USDT-перпов; листинг месяцев…", flush=True)
        with ThreadPoolExecutor(workers) as pool:
            avail = dict(zip(syms, pool.map(_sym_months, syms), strict=True))
        mp.write_text(json.dumps(avail), encoding="utf-8")
    syms = sorted(s for s in avail if s not in tf)
    print(f"монет {len(syms)} (TradFi исключено {len(avail) - len(syms)})", flush=True)
    jobs = [(s, m) for m in _months(a, b) for s in syms if m in avail[s]]
    jobs += [(s, d) for d in spill for s in syms if b in avail[s]]
    return 1 if _download(jobs, _kl_job, cache / "1h", workers) else 0


# ---------------------------------------------------------------- select

Series = dict[str, tuple[array, array, array, array, array]]


def load_1h(cache: Path, months: list[str], tf: set[str]) -> tuple[int, Series]:
    """Часовые бары подряд идущих месяцев → (начало сетки, монета → (open, high, low, close,
    оборот) по часам сетки; nan — бара нет)."""
    g0 = _ts(months[0])
    n = (_ts(_next_month(months[-1])) - g0) // H1
    out: Series = {}
    for m in months:
        p = cache / "1h" / f"{m}.jsonl"
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if not r["b"] or r["s"] in tf:
                    continue
                ser = out.get(r["s"])
                if ser is None:
                    ser = out[r["s"]] = tuple(array("d", [NAN]) * n for _ in range(5))
                for row in r["b"]:
                    i = (row[0] - g0) // H1
                    if 0 <= i < n:
                        for k in range(5):
                            ser[k][i] = row[k + 1]
    return g0, out


def select_at(t: int, g0: int, ser: Series, seed: int, uni: bool = False) -> dict:
    """Списки на момент запуска `t` (мс, начало часа)."""
    i = (t - g0) // H1
    elig: dict[str, list] = {f: [] for f in FILTERS}
    for s, (o, _h, _lo, c, q) in ser.items():
        c1, c25, o0 = c[i - 1], c[i - 25], o[i]
        if not (c1 > 0 and c25 > 0 and o0 > 0):  # nan — бара нет: монета не годится
            continue
        qv = sum(x for x in q[i - 24:i] if x == x)
        g = c1 / c25 - 1
        for f, lim in FILTERS.items():
            if qv >= lim:
                elig[f].append((g, s, qv))
    row: dict = {"t": t, "n": {}, "top": {}}
    for f, e in elig.items():
        e.sort(key=lambda x: (-x[0], x[1]))
        row["n"][f] = len(e)
        row["top"][f] = [[s, round(g, 6), round(q)] for g, s, q in e[:N_TOP]]
    names = sorted(s for _, s, _ in elig[MAIN_F])
    row["rnd"] = random.Random(f"{seed}:{t}").sample(names, min(N_RND, len(names)))
    if uni:
        row["uni"] = names
    return row


def stage_select(data: Path, out: Path, a: str, b: str, seed: int, tradfi: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    tf = _tradfi(tradfi)
    n = 0
    with (out / "lists.jsonl").open("w", encoding="utf-8") as fo:
        for m in _months(a, b):
            g0, ser = load_1h(data / "cache", [_prev_month(m), m], tf)
            t, end = _ts(m), _ts(_next_month(m))
            while t < end:
                for h in HOURS:
                    fo.write(json.dumps(select_at(t + h * H1, g0, ser, seed, h == 8),
                                        separators=(",", ":")) + "\n")
                    n += 1
                t += D1
            print(f"  {m}: монет в кэше {len(ser)}", flush=True)
    print(f"списки: {n} запусков → {out / 'lists.jsonl'}", flush=True)
    return 0


# ---------------------------------------------------------------- simulate

def pick(row: dict, sel: str, n: int) -> list[str]:
    if sel == "rnd":
        return row["rnd"]
    if sel == "uni":
        return row.get("uni", [])
    return [x[0] for x in row["top"][sel][:n]]


def needed(lists: list[dict], scope: str) -> dict[tuple[str, int], set[int]]:
    """(монета, запуск) → удержания, которые надо разыграть."""
    cells = CELLS if scope == "all" else {"main": CELLS["main"]}
    need: dict[tuple[str, int], set[int]] = defaultdict(set)
    for row in lists:
        hour = _dt(row["t"]).hour
        for c_hour, sel, n, hold in cells.values():
            if c_hour == hour:
                for s in pick(row, sel, n):
                    need[(s, row["t"])].add(hold)
    return need


def short_leg(bars: list[tuple[int, float, float, float]], pays: list[tuple[int, float]],
              slip: float, fee: float) -> tuple[float, bool, float, int]:
    """Шорт одной порции (номинал 1 по цене входа).

    `bars` — часовые бары окна удержания [(начало, open, high, close)] по порядку, первый — бар
    входа T, пропущенных часов в списке нет; `pays` — [(время, ставка)] выплат строго внутри
    (T, T + H). → (итог в долях порции, ликвидирована ли, фандинг, учтено выплат)."""
    t0 = bars[0][0]
    e = bars[0][1] * (1 - slip)  # рыночная продажа платит bid
    lim = e * (1 + 1 / LEV - MM)
    liq_t = next((t for t, _o, h, _c in bars if h >= lim), None)
    end = bars[-1][0] + H1
    fund, n = 0.0, 0
    for tp, r in pays:
        alive = tp <= liq_t if liq_t is not None else tp < end  # в начале часа ликвидации ещё жива
        if not (t0 < tp and alive):
            continue
        px = bars[0][1]
        for t, _o, _h, c in bars:  # цена на выплате — закрытие бара, кончившегося к ней
            if t + H1 > tp:
                break
            px = c
        fund += r * px / e  # шорт получает положительную ставку и платит отрицательную
        n += 1
    if liq_t is not None:
        return -1 / LEV - fee + fund, True, fund, n
    x = bars[-1][3] * (1 + slip)  # рыночная покупка платит ask
    return 1 - x / e - fee * (1 + x / e) + fund, False, fund, n


def load_funding(cache: Path, months: list[str]) -> tuple[dict[str, list], set[tuple[str, str]]]:
    """→ (монета → [(время, ставка)] по порядку, {(монета, месяц), за которые архив есть})."""
    rates: dict[str, list] = defaultdict(list)
    known: set[tuple[str, str]] = set()
    for m in months:
        p = cache / "funding" / f"{m}.jsonl"
        for r in (_read(p) if p.exists() else ()):
            if r["b"] is not None:
                known.add((r["s"], m))
                rates[r["s"]] += [tuple(x) for x in r["b"]]
    return {s: sorted(set(v)) for s, v in rates.items()}, known


def sim_one(s: str, t: int, holds: set[int], g0: int, ser: Series, rates: dict[str, list],
            known: set[tuple[str, str]]) -> dict:
    o, h, _lo, c, _q = ser[s]
    i = (t - g0) // H1
    if not o[i] == o[i]:
        return {"s": s, "t": t, "miss": 1}
    rec: dict = {"s": s, "t": t, "r": {}}
    ts_ = [x[0] for x in rates.get(s, ())]
    for hold in sorted(holds):
        bars = [(g0 + j * H1, o[j], h[j], c[j]) for j in range(i, min(i + hold, len(o)))
                if o[j] == o[j]]
        a, b = bisect_right(ts_, t), bisect_left(ts_, t + hold * H1)
        pays = rates[s][a:b] if s in rates else []
        kn = all((s, m) in known for m in {_month(t), _month(t + hold * H1 - 1)})
        v = [*short_leg(bars, pays, *COSTS["main"][::-1])]
        vh = short_leg(bars, pays, *COSTS["heavy"][::-1])
        rec["r"][str(hold)] = [round(v[0], 7), int(v[1]), round(vh[0], 7), int(vh[1]),
                               round(v[2], 7), v[3], int(kn),
                               int(bars[-1][0] != t + (hold - 1) * H1),
                               round(1 - bars[-1][3] / bars[0][1], 7)]
    return rec


def stage_simulate(data: Path, out: Path, scope: str, tradfi: Path) -> int:
    lists = _read(out / "lists.jsonl")
    need = needed(lists, scope)
    by_m: dict[str, dict] = defaultdict(dict)
    for (s, t), holds in need.items():
        by_m[_month(t)][(s, t)] = holds
    done_p, sim_p = out / "sim_done.txt", out / "sim.jsonl"
    done = set(done_p.read_text(encoding="utf-8").split()) if done_p.exists() else set()
    print(f"запусков {len(lists)}, монето-запусков {len(need)}, месяцев {len(by_m)}, "
          f"готово ранее {len(done)} (scope={scope})", flush=True)
    tf = _tradfi(tradfi)
    with sim_p.open("a", encoding="utf-8") as fs, done_p.open("a", encoding="utf-8") as fd:
        for m in sorted(by_m):
            if m in done:
                continue
            months = [m, _next_month(m)]
            g0, ser = load_1h(data / "cache", months, tf)
            rates, known = load_funding(data / "cache", months)
            for (s, t), holds in sorted(by_m[m].items(), key=lambda kv: (kv[0][1], kv[0][0])):
                fs.write(json.dumps(sim_one(s, t, holds, g0, ser, rates, known),
                                    separators=(",", ":")) + "\n")
            fs.flush()
            fd.write(m + "\n")
            fd.flush()
            print(f"  {m}: {len(by_m[m])}", flush=True)
    print("готово", flush=True)
    return 0


# ---------------------------------------------------------------- report

def launch_vals(lists: list[dict], sims: dict, cell: str, k: int = NET) -> dict[int, float]:
    """Запуск → среднее значения `k` по монетам выбора клетки (запуск без монет не входит)."""
    hour, sel, n, hold = CELLS[cell]
    out = {}
    for row in lists:
        if _dt(row["t"]).hour != hour:
            continue
        vals = [r["r"][str(hold)][k] for s in pick(row, sel, n)
                if (r := sims.get((s, row["t"]))) and str(hold) in r.get("r", {})]
        if vals:
            out[row["t"]] = fmean(vals)
    return out


def by_key(xs: dict[int, float], key: Callable[[int], object]) -> dict[object, list[float]]:
    d: dict[object, list[float]] = defaultdict(list)
    for t, v in sorted(xs.items()):
        d[key(t)].append(v)
    return d


def week(t: int) -> tuple[int, int]:
    return _dt(t).isocalendar()[:2]


def t_thin(xs: dict[int, float], hold: int) -> list[float]:
    """t по неперекрывающимся запускам: при удержании 48 ч — по каждому второму дню (обе
    чётности); иначе — по всем."""
    if hold <= 24:
        return [tstat(list(xs.values()))[2]]
    return [tstat([v for t, v in xs.items() if (t // D1) % 2 == p])[2] for p in (0, 1)]


def drawdown(xs: dict[int, float]) -> float:
    """Просадка кривой с фиксированным капиталом D0: накопленная сумма итогов запусков (доли
    D0), глубина от её максимума (стартовый ноль — тоже максимум), в долях D0."""
    cur = peak = dd = 0.0
    for _t, v in sorted(xs.items()):
        cur += v
        peak = max(peak, cur)
        dd = max(dd, peak - cur)
    return dd


HEAD = (f"{'клетка':<7}{'запусков':>9}{'ср.%':>8}{'шум%':>7}{'t':>11}{'t нед':>7}{'мес+':>7}"
        f"{'до изд.%':>9}{'фанд.%':>8}{'тяж.%':>8}{'t тяж':>7}{'ликв':>6}")


def cell_row(lists: list[dict], sims: dict, cell: str) -> tuple[str, float, float]:
    s = launch_vals(lists, sims, cell)
    if not s:
        return f"{cell:<7}{0:>9}", NAN, NAN
    hold = CELLS[cell][3]
    m, se, t = tstat(list(s.values()))
    ts_ = t_thin(s, hold)
    tw = tstat([fmean(v) for v in by_key(s, week).values()])[2]
    mo = by_key(s, _month)
    pos = sum(fmean(v) > 0 for v in mo.values())
    gross = fmean(launch_vals(lists, sims, cell, GROSS).values())
    fund = fmean(launch_vals(lists, sims, cell, FUND).values())
    hv = launch_vals(lists, sims, cell, NET_H)
    hm, _, ht = tstat(list(hv.values()))
    nliq = sum(launch_vals_count(lists, sims, cell, LIQ).values())
    tt = "/".join(f"{x:+.1f}" for x in ts_)
    return (f"{cell:<7}{len(s):>9}{m * 100:>+8.2f}{se * 100:>7.2f}{tt:>11}{tw:>+7.1f}"
            f"{f'{pos}/{len(mo)}':>7}{gross * 100:>+9.2f}{fund * 100:>+8.3f}{hm * 100:>+8.2f}"
            f"{ht:>+7.1f}{nliq:>6}"), m, (ts_[0] if len(ts_) == 1 else min(ts_))


def launch_vals_count(lists: list[dict], sims: dict, cell: str, k: int) -> dict[int, int]:
    """Запуск → сумма целого признака `k` по монетам выбора (например, ликвидаций)."""
    hour, sel, n, hold = CELLS[cell]
    out = {}
    for row in lists:
        if _dt(row["t"]).hour == hour:
            out[row["t"]] = sum(r["r"][str(hold)][k] for s in pick(row, sel, n)
                                if (r := sims.get((s, row["t"]))) and str(hold) in r.get("r", {}))
    return out


def coins(lists_by_t: dict[int, dict], sims: dict, cell: str, t: int) -> list[tuple]:
    """Монеты запуска клетки: (итог, монета, рост за 24 ч до входа), по возрастанию итога."""
    _hour, sel, n, hold = CELLS[cell]
    row = lists_by_t[t]
    grow = {x[0]: x[1] for f in FILTERS for x in row["top"][f]}
    return sorted((sims[(s, t)]["r"][str(hold)][NET], s, grow.get(s, NAN))
                  for s in pick(row, sel, n) if "r" in sims.get((s, t), {}))


def tail(lists: list[dict], sims: dict, s_main: dict[int, float]) -> tuple[float, float, int]:
    """Печать хвоста главной клетки → (худший запуск, просадка D0, ликвидаций)."""
    by_t = {row["t"]: row for row in lists}
    print("\n## худшие 5 запусков (запуск, итог в % капитала, три худшие монеты: итог % порции, "
          "рост за 24 ч до входа)")
    for t, v in sorted(s_main.items(), key=lambda kv: kv[1])[:5]:
        cs = coins(by_t, sims, "main", t)[:3]
        print(f"{_dt(t):%Y-%m-%d %H:%M}  {v * 100:+7.2f}  "
              + ", ".join(f"{s} {x * 100:+.1f} (рост {g * 100:+.0f}%)" for x, s, g in cs))
    print("\n## 10.10.2025: запуски, чьё окно содержит обвал 21:00 UTC (итог, % капитала)")
    for cell in ("main", "h00", "h16", "H12", "H48"):
        hold = CELLS[cell][3]
        for t, v in sorted(launch_vals(lists, sims, cell).items()):
            if t <= CRASH < t + hold * H1:
                cs = coins(by_t, sims, cell, t)
                (bx, bs, _), (wx, ws, _) = cs[-1], cs[0]
                print(f"{cell:<5} {_dt(t):%Y-%m-%d %H:%M}  {v * 100:+7.2f}  лучшая "
                      f"{bs} {bx * 100:+.1f}, худшая {ws} {wx * 100:+.1f}")
    liq = []
    for row in lists:
        if _dt(row["t"]).hour == 8:
            for x in row["top"][MAIN_F][:10]:
                r = sims.get((x[0], row["t"]), {}).get("r", {}).get("24")
                if r and r[LIQ]:
                    liq.append((row["t"], x[0], x[1], r[NET]))
    print(f"\n## ликвидации при ×3 (главная клетка): {len(liq)} монето-запусков")
    for t, s, g, v in liq:
        print(f"  {_dt(t):%Y-%m-%d %H:%M} {s}: рост за 24 ч до входа {g * 100:+.0f}%, "
              f"итог {v * 100:+.1f}% порции")
    worst = min(s_main.values())
    dd = drawdown(s_main)
    print(f"\n## худший запуск {worst * 100:+.2f}% капитала; просадка кривой D0 "
          f"{dd * 100:.1f}% D0; кривая D0 на конце {sum(s_main.values()) * 100:+.1f}% D0")
    return worst, dd, len(liq)


def stage_report(out: Path, oos: bool) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    lists = _read(out / "lists.jsonl")
    sims = {(r["s"], r["t"]): r for r in _read(out / "sim.jsonl")}
    recs = list(sims.values())
    main_rows = [row for row in lists if _dt(row["t"]).hour == 8]
    keys = {(x[0], row["t"]) for row in main_rows for x in row["top"][MAIN_F][:10]}
    mv = [sims[k]["r"]["24"] for k in keys if "r" in sims.get(k, {})]
    span = f"{_day(lists[0]['t'])} … {_day(lists[-1]['t'])}"
    print(f"# шорт лидеров роста за 24 ч — {'ВНЕ ВЫБОРКИ' if oos else 'выборка'} {span}")
    nel = [row["n"][MAIN_F] for row in main_rows]
    print(f"запусков 08:00 {len(main_rows)}; монет с оборотом ≥ 10 млн: медиана {median(nel)}, "
          f"мин {min(nel)}; запусков, где таких меньше 10: {sum(x < 10 for x in nel)}")
    grow = [x[1] for row in main_rows for x in row["top"][MAIN_F][:10]]
    tenth = [r["top"][MAIN_F][9][1] for r in main_rows if len(r["top"][MAIN_F]) >= 10]
    print(f"рост за 24 ч до входа у выбранных топ-10: медиана {median(grow) * 100:+.1f}%, "
          f"10-й в списке — медиана {median(tenth) * 100:+.1f}%")
    print(f"монето-запусков в sim {len(recs)}, без бара входа {sum('miss' in r for r in recs)}; "
          f"главная клетка {len(mv)}: обрыв данных (делистинг) {sum(v[CUT] for v in mv)}, "
          f"ставки фандинга неизвестны {sum(1 - v[KNOWN] for v in mv)}, с выплатами "
          f"{sum(v[NPAY] > 0 for v in mv) / len(mv):.0%}, фандинг в среднем "
          f"{fmean(v[FUND] for v in mv) * 100:+.3f}% порции, в плюсе "
          f"{sum(v[NET] > 0 for v in mv) / len(mv):.0%} монето-запусков")
    print("итог запуска — среднее по монетам, % порции = % капитала; «до изд.» — ход цены без "
          "издержек и фандинга; t у H48 — по каждому второму дню (две чётности)\n")
    print(HEAD)
    line, m_main, t_main = cell_row(lists, sims, "main")
    print(line)
    res = {"main": (m_main, t_main)}
    if not oos:
        for cell in CELLS:
            if cell != "main":
                line, m, t = cell_row(lists, sims, cell)
                res[cell] = (m, t)
                print(line)
    s_main = launch_vals(lists, sims, "main")
    print("\n## главная клетка по месяцам: ср. итог запуска, % капитала (запусков)")
    for mo, v in by_key(s_main, _month).items():
        print(f"{mo}  {fmean(v) * 100:+7.2f}  ({len(v)})")
    print("\n## по годам: ср. итог запуска, %")
    print("  ".join(f"{y} {fmean(v) * 100:+.2f} ({len(v)})"
                    for y, v in by_key(s_main, lambda t: _dt(t).year).items()))
    worst, dd, n_liq = tail(lists, sims, s_main)

    print(f"\n## ВЕРДИКТ по разделу 4 (заморожен 27.10.2026){' — вне выборки' if oos else ''}")
    ok_main = m_main > 0 and t_main >= 2
    tw = tstat([fmean(v) for v in by_key(s_main, week).values()])[2]
    print(f"* средний итог запуска {m_main * 100:+.2f}% капитала, t {t_main:+.1f} (по неделям "
          f"{tw:+.1f}): {'> 0 и t ≥ 2' if ok_main else 'НЕ (> 0 и t ≥ 2)'}")
    ok_tail = worst > -0.20 and dd <= 0.30
    print(f"* хвост: худший запуск {worst * 100:+.2f}% капитала (порог −20%), просадка D0 "
          f"{dd * 100:.1f}% (порог 30%), ликвидаций {n_liq} → "
          f"{'в пределах' if ok_tail else 'failed независимо от среднего'}")
    if oos:
        print(f"ИТОГ вне выборки: {'прошла' if ok_main and ok_tail else 'НЕ прошла'}")
        return 0
    mo = by_key(s_main, _month)
    pos = sum(fmean(v) > 0 for v in mo.values())
    ok_mo = pos >= len(mo) * 2 / 3
    print(f"* положительных месяцев {pos}/{len(mo)}: {'≥ 2/3' if ok_mo else '< 2/3'}")
    sign = m_main > 0
    ok_pl = all((res[c][0] > 0) == sign for c in PLATEAU)
    print("* плато (N 5/20, удержание 12/48 ч, час 00/16 — того же знака): "
          + ", ".join(f"{c} {res[c][0] * 100:+.2f}" for c in PLATEAU)
          + f" → {'да' if ok_pl else 'нет'}")
    rnd = launch_vals(lists, sims, "rnd10")
    diff = [s_main[t] - rnd[t] for t in s_main if t in rnd]
    dm, dse, dt = tstat(diff)
    ok_sel = dt > 2
    print(f"* выбор против случайных 10: разность {dm * 100:+.2f}% на запуск, шум {dse * 100:.2f}, "
          f"t {dt:+.1f} → "
          + ("выбор что-то даёт" if ok_sel else "выбор ничего не даёт (шорт альтов вообще)"))
    uni = launch_vals(lists, sims, "uni")
    du = [s_main[t] - uni[t] for t in s_main if t in uni]
    um, use, ut = tstat(du)
    print(f"  (справочно) против всей вселенной: разность {um * 100:+.2f}%, шум {use * 100:.2f}, "
          f"t {ut:+.1f}")
    verdict = ok_main and ok_mo and ok_pl and ok_sel and ok_tail
    print(f"ИТОГ выборки: {'эффект есть — дальше вне выборки' if verdict else 'failed'}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--data", type=Path, required=True)
    f.add_argument("--from", dest="a")
    f.add_argument("--to", dest="b")
    f.add_argument("--spill", default="", help="дни после --to через запятую (дневные архивы)")
    f.add_argument("--funding-for", type=Path, help="каталог с lists.jsonl: качать фандинг")
    f.add_argument("--scope", choices=("all", "main"), default="all")
    f.add_argument("--workers", type=int, default=16)
    f.add_argument("--tradfi", type=Path, default=TRADFI)
    s = sub.add_parser("select")
    s.add_argument("--data", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--from", dest="a", required=True)
    s.add_argument("--to", dest="b", required=True)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--tradfi", type=Path, default=TRADFI)
    m = sub.add_parser("simulate")
    m.add_argument("--data", type=Path, required=True)
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--scope", choices=("all", "main"), default="all")
    m.add_argument("--tradfi", type=Path, default=TRADFI)
    r = sub.add_parser("report")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--oos", action="store_true", help="вне выборки: только главная клетка")
    a = ap.parse_args()
    if a.cmd == "fetch":
        if a.funding_for is None and not (a.a and a.b):
            ap.error("fetch: нужны --from/--to или --funding-for")
        spill = [x for x in a.spill.split(",") if x]
        return stage_fetch(a.data, a.a, a.b, spill, a.workers, a.tradfi, a.funding_for, a.scope)
    if a.cmd == "select":
        return stage_select(a.data, a.out, a.a, a.b, a.seed, a.tradfi)
    if a.cmd == "simulate":
        return stage_simulate(a.data, a.out, a.scope, a.tradfi)
    return stage_report(a.out, a.oos)


if __name__ == "__main__":
    raise SystemExit(main())
