#!/usr/bin/env python
"""Автомат «купить обвал минуты»: что стало бы с ДЕПОЗИТОМ при пределе позиций и дневном стопе.

`crash_bounce_check.py` отвечает на вопрос «каков итог одной сделки» и считает капитал
бесконечным: позиция на каждую монету сразу. Здесь те же сделки ленты проходят через счёт:
сделки идут по времени входа, позиция открывается, только если есть свободный слот и не
сработал дневной стоп, итог ложится на депозит в момент выхода. Правила, сетка и критерии
объявлены владельцем ДО прогона: `docs/research/crash-bounce-capital-2026-10-04.md`.

    python scripts/crash_bounce_portfolio.py --out D [--day 2025-10-10] [--sample S]

`D` — каталог полного прогона (`simulate --min-drop 0.05 --side --vars limit5,mkt5_0.25s
--all-events --stress 2025-10-10T20:56`). `--day` — только эти сутки (ответ по каскаду, пока
прогон не кончился). `--sample` — каталог выборки 03.10: совпадающие сделки обязаны совпасть.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import fmean, stdev

VARIANTS = ("limit5|side", "mkt5_0.25s|side")
STRESS_VAR = "limit5_api|side"
SIZES = (0.01, 0.025, 0.05)
CAPS = (3, 5, 10, 20, None)  # None — без предела: контроль, это мерил прошлый замер
STOPS = (None, 0.02, 0.05, 0.10)
MAIN = (0.025, 10, 0.05)
# Заявки лимиток стоят заранее; снятие после заполнения последнего слота (или стопа) занимает
# время, и всё, что исполнилось за это время, открывается сверх предела.
CANCEL_MS = 1000
STRESS_T0 = int(datetime(2025, 10, 10, 20, 56, tzinfo=UTC).timestamp() * 1000)
STRESS_T1 = STRESS_T0 + 60 * 60_000
CASCADE_DAY = "2025-10-10"
PERIOD = (date(2025, 1, 1), date(2026, 9, 30))


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _hm(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%H:%M:%S") if ms else "—"


@dataclass
class Book:
    """Итог одного прогона счёта. Деньги — доли начального депозита (1.0)."""
    equity: float = 1.0
    day_pnl: dict[str, float] = field(default_factory=dict)     # закрыто за сутки UTC
    day_start: dict[str, float] = field(default_factory=dict)   # депозит на 00:00 UTC
    day_worst: dict[str, float] = field(default_factory=dict)   # худший момент суток, доля
    stop_at: dict[str, int] = field(default_factory=dict)       # когда сработал дневной стоп
    max_dd: float = 0.0          # по закрытым сделкам, от вершины
    max_dd_pess: float = 0.0     # с худшей ценой открытых позиций (оценка в плохую сторону)
    taken: int = 0
    overfill: int = 0
    cut_cap: int = 0
    cut_stop: int = 0
    cut_busy: int = 0
    cut_api: int = 0
    max_open: int = 0
    max_exposure: float = 0.0    # сумма размеров открытых позиций / депозит


def run_book(trades: list[dict], size: float, cap: int | None, stop: float | None,
             limit: bool, forced: list[dict] | None = None) -> Book:
    """Счёт по сделкам, отсортированным по (t_in, sym).

    `forced` — стресс «API лёг» у лимиток: заявки, стоявшие в T0, исполняются без предела и
    стопа, но только если в T0 заявки вообще стояли (был свободный слот и не было стопа) и
    монета не держалась открытой.
    """
    b = Book()
    open_h: list[tuple[int, int, str, float, float, float]] = []  # t_out, seq, sym, pnl, worst, amt
    open_syms: set[str] = set()
    open_worst = 0.0
    open_amt = 0.0
    peak = 1.0
    cur_day = None
    stopped_at: int | None = None
    full_at: int | None = None
    seq = 0

    def roll(t: int) -> None:
        nonlocal cur_day, stopped_at
        d = _day(t)
        if d != cur_day:
            cur_day, stopped_at = d, None
            b.day_start[d] = b.equity
            b.day_pnl.setdefault(d, 0.0)
            b.day_worst.setdefault(d, 0.0)

    def mark(t: int) -> None:
        """Худший момент: закрытый депозит плюс худшая цена всех открытых сразу."""
        nonlocal peak
        pess = b.equity + open_worst
        b.max_dd_pess = max(b.max_dd_pess, 1 - pess / peak)
        d = _day(t)
        b.day_worst[d] = min(b.day_worst.get(d, 0.0), pess / b.day_start[d] - 1)

    def close_until(t: int) -> None:
        nonlocal open_worst, open_amt, peak, stopped_at, full_at
        while open_h and open_h[0][0] <= t:
            t_out, _, sym, pnl, worst, amt = heapq.heappop(open_h)
            roll(t_out)
            open_syms.discard(sym)
            open_worst -= worst
            open_amt -= amt
            b.equity += pnl
            b.day_pnl[cur_day] += pnl
            peak = max(peak, b.equity)
            b.max_dd = max(b.max_dd, 1 - b.equity / peak)
            mark(t_out)
            if (stop is not None and stopped_at is None
                    and b.day_pnl[cur_day] <= -stop * b.day_start[cur_day]):
                stopped_at = t_out
                b.stop_at.setdefault(cur_day, t_out)

    def take(tr: dict) -> None:
        nonlocal open_worst, open_amt, seq, full_at
        amt = size * b.day_start[cur_day]
        worst = amt * min(tr["net"], tr["low"] / tr["entry"] - 1)
        seq += 1
        heapq.heappush(open_h, (tr["t_out"], seq, tr["sym"], amt * tr["net"], worst, amt))
        open_syms.add(tr["sym"])
        open_worst += worst
        open_amt += amt
        b.taken += 1
        if cap is not None and len(open_h) == cap:
            full_at = tr["t_in"]
        b.max_open = max(b.max_open, len(open_h))
        b.max_exposure = max(b.max_exposure, open_amt / max(b.equity, 1e-12))
        mark(tr["t_in"])

    forced_done = forced is None
    for tr in trades:
        if not forced_done and tr["t_in"] >= STRESS_T0:
            forced_done = True
            close_until(STRESS_T0)
            roll(STRESS_T0)
            resting = stopped_at is None and (cap is None or len(open_h) < cap)
            held = set(open_syms)
            for f in forced:
                close_until(f["t_in"])
                roll(f["t_in"])
                if resting and f["sym"] not in held and f["sym"] not in open_syms:
                    take(f)
                    b.overfill += cap is not None and len(open_h) > cap
        close_until(tr["t_in"])
        roll(tr["t_in"])
        if tr.get("api_blocked"):
            b.cut_api += 1
            continue
        if tr["sym"] in open_syms:
            b.cut_busy += 1
            continue
        over = False
        if stopped_at is not None:
            if not (limit and tr["t_in"] <= stopped_at + CANCEL_MS):
                b.cut_stop += 1
                continue
            over = True
        if cap is not None and len(open_h) >= cap:
            if not (limit and full_at is not None and tr["t_in"] <= full_at + CANCEL_MS):
                b.cut_cap += 1
                continue
            over = True
        take(tr)
        b.overfill += over
    close_until(2 ** 62)
    return b


def _with_stress(trades: list[dict], limit: bool) -> list[dict]:
    """Сделки суток каскада при простое API [T0, T1): входа нет, выход по таймеру — после T1."""
    out = []
    for tr in trades:
        if STRESS_T0 <= tr["t_in"] < STRESS_T1:
            out.append({**tr, "api_blocked": True})  # бот не смог войти, заявки — в forced
        elif tr["t_in"] < STRESS_T0 and "api_net" in tr:
            out.append({**tr, "t_out": tr["api_out"], "net": tr["api_net"],
                        "low": tr["api_low"], "how": tr["api_how"]})
        else:
            out.append(tr)
    return out


def _period_days() -> list[str]:
    d, out = PERIOD[0], []
    while d <= PERIOD[1]:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _daily(b: Book, days: list[str]) -> list[float]:
    return [b.day_pnl.get(d, 0.0) / b.day_start[d] if d in b.day_start else 0.0 for d in days]


def _months_plus(b: Book, days: list[str]) -> tuple[int, int]:
    m = defaultdict(lambda: 1.0)
    for d, r in zip(days, _daily(b, days), strict=True):
        m[d[:7]] *= 1 + r
    return sum(v > 1 for v in m.values()), len(m)


def _t(rets: list[float]) -> float:
    s = stdev(rets) if len(rets) > 1 else 0.0
    return fmean(rets) / (s / math.sqrt(len(rets))) if s else 0.0


def _cap(c: int | None) -> str:
    return "∞" if c is None else str(c)


def _stop(s: float | None) -> str:
    return "нет" if s is None else f"−{s * 100:g}%"


def load(out: Path) -> tuple[dict[str, list[dict]], list[dict], int]:
    """Сделки по вариантам. Прогон, оборванный посреди записи дня и перезапущенный, пишет
    день заново: дубли (вариант, символ, минута) схлопываются, оборванная строка пропускается."""
    by = defaultdict(dict)
    missing = set()
    with (out / "sim.jsonl").open() as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("missing"):
                missing.add((r["sym"], r["day"]))
            elif r["var"] in VARIANTS or r["var"] == STRESS_VAR:
                by[r["var"]][(r["sym"], r["t"])] = r
    rows = {v: sorted(d.values(), key=lambda r: (r["t_in"], r["sym"])) for v, d in by.items()}
    return rows, rows.pop(STRESS_VAR, []), len(missing)


def coverage(out: Path, days: set[str] | None) -> None:
    """Сколько дней-символов (обвал ≥5%) разыграно — по дням интереса или всего."""
    need = defaultdict(set)
    with (out / "events.jsonl").open() as fh:
        for line in fh:
            e = json.loads(line)
            if e["drop"] <= -0.05:
                need[_day(e["t"])].add(e["sym"])
    done = defaultdict(set)
    for k in (out / "sim_done.txt").read_text().split():
        s, d = k.rsplit(":", 1)
        done[d].add(s)
    keys = sorted(days) if days else sorted(need)
    n_need = sum(len(need[d]) for d in keys)
    n_done = sum(len(done[d] & need[d]) for d in keys)
    print(f"разыграно дней-символов: {n_done} из {n_need}"
          + ("" if n_done == n_need else "  — ПРОГОН НЕ ЗАКОНЧЕН, цифры неполные"))


def crosscheck(full: Path, sample: Path) -> None:
    """Сделки полного прогона против выборки 03.10 по одним и тем же (вариант, символ, минута)."""
    want = ("limit5", "mkt5_0.25s")
    new = {}
    with (full / "sim.jsonl").open() as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("var") in want:
                new[(r["var"], r["sym"], r["t"])] = r
    days = {tuple(k.rsplit(":", 1)) for k in (full / "sim_done.txt").read_text().split()}
    same = diff = absent = 0
    with (sample / "sim.jsonl").open() as fh:
        for line in fh:
            o = json.loads(line)
            if o.get("var") not in want or (o["sym"], o["day"]) not in days:
                continue
            n = new.get((o["var"], o["sym"], o["t"]))
            if n is None:
                absent += 1
            elif (n["t_out"] == o["t_out"]
                  and all(abs(n[f] - o[f]) < 1e-12 for f in ("entry", "exit", "net"))):
                same += 1
            else:
                diff += 1
    print(f"сверка с выборкой 03.10 (limit5, mkt5_0.25s без стороны, общие дни-символы): "
          f"совпало {same}, разошлось {diff}, нет в полном прогоне {absent}")


def cascade_table(by: dict[str, list[dict]], forced: list[dict]) -> None:
    print(f"\n## А. Сутки {CASCADE_DAY} (UTC): итог дня по закрытым сделкам / худший момент "
          f"дня с худшей ценой открытых, % депозита на начало суток")
    for var in VARIANTS:
        limit = var.startswith("limit")
        day = [r for r in by[var] if r["day"] == CASCADE_DAY]
        print(f"\n### {var}: сделок-кандидатов {len(day)}")
        print(f"{'предел':>6} {'стоп':>5} | " + " | ".join(f"{f'размер {s * 100:g}%':^19}"
                                                         for s in SIZES)
              + f" | при {MAIN[0] * 100:g}%: сделок перебор стоп в")
        for cap in CAPS:
            for stop in STOPS:
                cells, main = [], None
                for size in SIZES:
                    b = run_book(day, size, cap, stop, limit)
                    r = b.day_pnl.get(CASCADE_DAY, 0.0) / b.day_start.get(CASCADE_DAY, 1.0)
                    w = b.day_worst.get(CASCADE_DAY, 0.0)
                    cells.append(f"{r * 100:+8.2f} / {w * 100:+7.2f}")
                    if size == MAIN[0]:
                        main = b
                print(f"{_cap(cap):>6} {_stop(stop):>5} | " + " | ".join(cells)
                      + f" | {main.taken:>6} {main.overfill:>7} "
                      f"{_hm(main.stop_at.get(CASCADE_DAY))}")
    print(f"\n## Стресс «API лёг» {_hm(STRESS_T0)}–{_hm(STRESS_T1)} UTC, главные клетки "
          f"(размер {MAIN[0] * 100:g}%, предел {MAIN[1]}, стоп {_stop(MAIN[2])})")
    for var in VARIANTS:
        limit = var.startswith("limit")
        day = [r for r in by[var] if r["day"] == CASCADE_DAY]
        base = run_book(day, *MAIN, limit)
        st = run_book(_with_stress(day, limit), *MAIN, limit,
                      forced=sorted(forced, key=lambda r: (r["t_in"], r["sym"])) if limit else None)
        for lab, b in (("как есть", base), ("API лёг", st)):
            r = b.day_pnl.get(CASCADE_DAY, 0.0) / b.day_start.get(CASCADE_DAY, 1.0)
            print(f"{var:<17} {lab:<9} итог дня {r * 100:+7.2f}%, худший момент "
                  f"{b.day_worst.get(CASCADE_DAY, 0.0) * 100:+7.2f}%, сделок {b.taken}, сверх "
                  f"предела {b.overfill}, позиций сразу до {b.max_open}, задействовано до "
                  f"{b.max_exposure * 100:.0f}% депозита")
    print(f"(стоявших заявок лимиток, исполненных в простое: {len(forced)} монет — без "
          f"монет, не обвалившихся на ≥5% за минуту: те не качались)")


def period_table(by: dict[str, list[dict]]) -> dict:
    days = _period_days()
    res = {}
    print("\n## Б. Весь период 01.2025–09.2026, % депозита. «без 10.10» — тот же счёт без "
          "сделок суток каскада; «отрезано» — доля ВСЕХ сигналов, не взятых из-за предела/стопа")
    head = (f"{'предел':>6} {'стоп':>5} {'разм':>5} {'итог':>8} {'без10.10':>8} {'просад':>7} "
            f"{'сх.худш':>7} {'мес+':>6} {'худш.день':>9} {'t':>5} {'сделок':>6} "
            f"{'отрез.пред':>10} {'стоп':>5} {'перебор':>7}")
    for var in VARIANTS:
        limit = var.startswith("limit")
        rows = by[var]
        calm = [r for r in rows if r["day"] != CASCADE_DAY]
        print(f"\n### {var}: сделок-кандидатов {len(rows)}")
        print(head)
        for cap in CAPS:
            for stop in STOPS:
                for size in SIZES:
                    b = run_book(rows, size, cap, stop, limit)
                    c = run_book(calm, size, cap, stop, limit)
                    rets = _daily(b, days)
                    plus, nm = _months_plus(b, days)
                    tot = b.equity - 1
                    res[(var, size, cap, stop)] = {"total": tot, "t": _t(rets), "dd": b.max_dd}
                    n_all = len(rows)
                    print(f"{_cap(cap):>6} {_stop(stop):>5} {size * 100:>4g}% {tot * 100:>+8.2f} "
                          f"{(c.equity - 1) * 100:>+8.2f} {b.max_dd * 100:>6.2f}% "
                          f"{b.max_dd_pess * 100:>6.2f}% {f'{plus}/{nm}':>6} "
                          f"{min(rets) * 100:>+8.2f}% {_t(rets):>+5.1f} {b.taken:>6} "
                          f"{b.cut_cap / n_all:>10.1%} {b.cut_stop / n_all:>5.1%} {b.overfill:>7}")
    return res


def verdict(res: dict) -> None:
    size, cap, stop = MAIN
    print(f"\n## В. Вердикт по главной клетке: размер {size * 100:g}%, предел {cap}, стоп "
          f"{_stop(stop)} (объявлено до прогона)")
    for var in VARIANTS:
        m = res[(var, size, cap, stop)]
        c1 = m["total"] > 0 and m["t"] >= 2
        c2 = m["dd"] <= 0.05
        nb = [(var, size, 5, stop), (var, size, 20, stop), (var, 0.01, cap, stop),
              (var, 0.05, cap, stop), (var, size, cap, 0.02), (var, size, cap, 0.10)]
        c3 = all(res[k]["total"] > 0 for k in nb)
        nbs = ", ".join(f"{res[k]['total'] * 100:+.1f}" for k in nb)
        print(f"{var}: итог {m['total'] * 100:+.2f}% (t {m['t']:+.1f}) — {'да' if c1 else 'НЕТ'}; "
              f"просадка {m['dd'] * 100:.2f}% ≤ 5% — {'да' if c2 else 'НЕТ'}; плато (6 соседей "
              f"в плюсе) — {'да' if c3 else 'НЕТ'} [{nbs}] → "
              f"{'ПРОХОДИТ к проверке 2021–2024' if c1 and c2 and c3 else 'failed остаётся'}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--day", help="только сутки каскада (ответ, пока прогон не кончился)")
    ap.add_argument("--sample", type=Path, help="каталог выборки 03.10 для сверки")
    a = ap.parse_args()
    if a.day and a.day != CASCADE_DAY:
        ap.error(f"разбор одних суток сделан только для {CASCADE_DAY}")
    by, forced, missing = load(a.out)
    coverage(a.out, {a.day} if a.day else None)
    print(f"дней-символов без ленты aggTrades: {missing}")
    if a.sample:
        crosscheck(a.out, a.sample)
    cascade_table(by, forced)
    if not a.day:
        verdict(period_table(by))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
