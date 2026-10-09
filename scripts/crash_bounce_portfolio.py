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

Проверка вне выборки 2021–2024 (объявлена 08.10.2026):

    python scripts/crash_bounce_portfolio.py --out D2 --period 2021-01-01:2024-12-31 --fixed \\
        --cascades 2021-05-19,2022-05-09,2022-05-10,2022-05-11,2022-05-12,\\
2022-11-08,2022-11-09,2024-08-05

`--fixed` — на стратегию выделен фиксированный депозит D0, в 00:00 UTC всё сверх D0 выводится,
после убытков счёт не пополняется; клетка 1% × 3 без стопа. Сетка со сложным процентом
печатается для справки, вердикт — по фиксированному депозиту. Таблица А и стресс «API лёг»
есть только у 10.10.2025 и печатаются, только если эти сутки в периоде.

Замер вперёд (объявлен 09.10.2026, `docs/research/crash-bounce-paper-2026-10-09.md`):

    python scripts/crash_bounce_portfolio.py --out D --forward 2026-10-10 \\
        --period 2026-10-10:ВЧЕРА [--json итог.json]

Только `limit5|side`, та же клетка, что `--fixed`; критерии К1 (итог > 0 и t по дням ≥ 1) и К2
(падение ниже D0 ≤ 5%), итоги по неделям окна, сколько осталось до 12 недель и 300 сделок.

`--top-k 100` — проверка на истории правила выбора монет для счёта «×1» (на субсчёте ×1 при 1%
на заявку помещается ≈100 заявок): в сутки D берутся только 100 монет с наибольшим числом минут
обвала ≥5% за 30 суток D−32…D−3 (столько архива есть у бота в 00:00 UTC), при равенстве — по
обороту за сутки из последнего события монеты. Приближения: всегда 100 монет (на деле
100 − открытые позиции) и без задержки перестановки заявок.
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
# Проверка вне выборки 2021–2024: клетка объявлена 08.10.2026 до прогона, вывод сверх D0.
FIXED_CELL = (0.01, 3, None)
FIXED_MAX_DROP = 0.05
# Замер вперёд (объявлен 09.10.2026): вердикт не раньше 12 недель и 300 сделок; К1 — итог > 0
# и t по дням ≥ 1, К2 — падение баланса ниже D0 ≤ 5%.
FWD_VAR = "limit5|side"
FWD_WEEKS, FWD_TRADES, FWD_MIN_T = 12, 300, 1.0
# Клетки замера вперёд: (размер, предел, порог К2). Главная — первая; две покрупнее объявлены
# владельцем 09.10.2026 до окна, каждая судится отдельно своим К2.
FWD_CELLS = ((0.01, 3, FIXED_MAX_DROP), (0.025, 3, 0.10), (0.05, 3, 0.15))


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
    # только при выводе сверх D0 (`fixed`), D0 = 1.0
    withdrawn: float = 0.0
    min_bal: float = 1.0         # наименьший баланс по закрытым сделкам
    min_bal_pess: float = 1.0    # то же с худшей ценой открытых позиций
    day_low: dict[str, float] = field(default_factory=dict)  # наименьший закрытый баланс суток
    day_taken: dict[str, int] = field(default_factory=dict)  # сделок, открытых за сутки
    day_over: dict[str, int] = field(default_factory=dict)   # из них сверх предела


def run_book(trades: list[dict], size: float, cap: int | None, stop: float | None,
             limit: bool, forced: list[dict] | None = None, fixed: bool = False) -> Book:
    """Счёт по сделкам, отсортированным по (t_in, sym).

    `fixed` — фиксированный депозит D0 = 1.0: в 00:00 UTC всё сверх D0 выводится (`withdrawn`),
    пополнения нет. Размер — доля депозита на 00:00 после вывода, то есть доля × min(баланс, D0).
    Итог = выведенное + конечный баланс − D0 = сумма итогов сделок.

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
        nonlocal cur_day, stopped_at, peak
        d = _day(t)
        if d != cur_day:
            cur_day, stopped_at = d, None
            if fixed and b.equity > 1.0:
                # Баланс по закрытым сделкам между последним событием прошлых суток и первым
                # событием этих не меняется: вывод здесь равен выводу ровно в 00:00 UTC.
                b.withdrawn += b.equity - 1.0
                b.equity = peak = 1.0
            b.day_start[d] = b.equity
            b.day_low[d] = b.equity
            b.day_pnl.setdefault(d, 0.0)
            b.day_worst.setdefault(d, 0.0)

    def mark(t: int) -> None:
        """Худший момент: закрытый депозит плюс худшая цена всех открытых сразу."""
        nonlocal peak
        pess = b.equity + open_worst
        b.max_dd_pess = max(b.max_dd_pess, 1 - pess / peak)
        b.min_bal_pess = min(b.min_bal_pess, pess)
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
            b.min_bal = min(b.min_bal, b.equity)
            b.day_low[cur_day] = min(b.day_low[cur_day], b.equity)
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
        b.day_taken[cur_day] = b.day_taken.get(cur_day, 0) + 1
        if cap is not None and len(open_h) > cap:
            b.day_over[cur_day] = b.day_over.get(cur_day, 0) + 1
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


def _period_days(period: tuple[date, date] = PERIOD) -> list[str]:
    d, out = period[0], []
    while d <= period[1]:
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


def period_table(by: dict[str, list[dict]], period: tuple[date, date] = PERIOD,
                 cascades: tuple[str, ...] = (CASCADE_DAY,)) -> dict:
    days = _period_days(period)
    res = {}
    if cascades == (CASCADE_DAY,):
        print("\n## Б. Весь период 01.2025–09.2026, % депозита. «без 10.10» — тот же счёт без "
              "сделок суток каскада; «отрезано» — доля ВСЕХ сигналов, не взятых из-за "
              "предела/стопа")
        no_cas = "без10.10"
    else:
        print(f"\n## Справка: весь период {period[0]:%m.%Y}–{period[1]:%m.%Y}, сложный процент, "
              f"% депозита. Клетку по этой таблице не выбираем. «безкаск» — тот же счёт без "
              f"сделок суток каскадов; «отрезано» — доля ВСЕХ сигналов, не взятых из-за "
              f"предела/стопа")
        no_cas = "безкаск"
    head = (f"{'предел':>6} {'стоп':>5} {'разм':>5} {'итог':>8} {no_cas:>8} {'просад':>7} "
            f"{'сх.худш':>7} {'мес+':>6} {'худш.день':>9} {'t':>5} {'сделок':>6} "
            f"{'отрез.пред':>10} {'стоп':>5} {'перебор':>7}")
    for var in VARIANTS:
        limit = var.startswith("limit")
        rows = by[var]
        calm = [r for r in rows if r["day"] not in cascades]
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


def fixed_table(by: dict[str, list[dict]], period: tuple[date, date],
                cascades: tuple[str, ...]) -> None:
    """Проверка вне выборки: фиксированный депозит D0, вывод сверх D0, клетка `FIXED_CELL`.
    Деньги — доли D0; итог дня — сумма закрытых за сутки сделок в долях D0 (их сумма = итог)."""
    size, cap, stop = FIXED_CELL
    days = _period_days(period)
    print(f"\n## Фиксированный депозит D0 {period[0]:%m.%Y}–{period[1]:%m.%Y}: в 00:00 UTC всё "
          f"сверх D0 выводится, пополнения нет; размер {size * 100:g}% × min(баланс, D0), "
          f"предел {_cap(cap)}, стоп {_stop(stop)} (объявлено 08.10.2026 до прогона). "
          f"Деньги — % D0")
    for var in VARIANTS:
        rows = by.get(var, [])
        b = run_book(rows, size, cap, stop, var.startswith("limit"), fixed=True)
        pnl = [b.day_pnl.get(d, 0.0) for d in days]
        total = b.withdrawn + b.equity - 1.0
        months = defaultdict(float)
        for d, r in zip(days, pnl, strict=True):
            months[d[:7]] += r
        drop = 1.0 - b.min_bal
        print(f"\n### {var}: сделок-кандидатов {len(rows)}, взято {b.taken}, отрезано пределом "
              f"{b.cut_cap}, монета уже в позиции {b.cut_busy}, сверх предела {b.overfill}, "
              f"позиций сразу до {b.max_open}")
        print(f"итог {total * 100:+.2f}% D0 = выведено {b.withdrawn * 100:.2f}% + конечный баланс "
              f"{b.equity * 100:.2f}% − 100%; t по дням {_t(pnl):+.2f}; месяцев в плюсе "
              f"{sum(v > 0 for v in months.values())} из {len(months)}; худший день "
              f"{min(pnl) * 100:+.2f}%")
        print(f"наименьший баланс по закрытым {b.min_bal * 100:.2f}% (падение ниже D0 "
              f"{drop * 100:.2f}%), с худшей ценой открытых {b.min_bal_pess * 100:.2f}%")
        print(f"{'год':>6} {'итог':>8} {'t':>6} {'мес+':>6} {'худш.день':>9} {'мин.баланс':>10} "
              f"{'сделок':>6}")
        for y in sorted({d[:4] for d in days}):
            yd = [d for d in days if d[:4] == y]
            yp = [b.day_pnl.get(d, 0.0) for d in yd]
            ym = [v for m, v in months.items() if m[:4] == y]
            low = min((b.day_low[d] for d in yd if d in b.day_low), default=1.0)
            print(f"{y:>6} {sum(yp) * 100:>+7.2f}% {_t(yp):>+6.2f} "
                  f"{f'{sum(v > 0 for v in ym)}/{len(ym)}':>6} {min(yp) * 100:>+8.2f}% "
                  f"{low * 100:>9.2f}% {sum(b.day_taken.get(d, 0) for d in yd):>6}")
        print(f"{'сутки каскада':>13} {'итог дня':>9} {'худш.момент':>11} {'баланс 00:00':>12} "
              f"{'сделок':>6} {'сверх':>5}")
        for d in cascades:
            st = b.day_start.get(d, 1.0)
            print(f"{d:>13} {b.day_pnl.get(d, 0.0) * 100:>+8.2f}% "
                  f"{b.day_worst.get(d, 0.0) * st * 100:>+10.2f}% {st * 100:>11.2f}% "
                  f"{b.day_taken.get(d, 0):>6} {b.day_over.get(d, 0):>5}")
        c1 = total > 0 and _t(pnl) >= 2
        c2 = drop <= FIXED_MAX_DROP
        print(f"ВЕРДИКТ {var}: итог > 0 и t ≥ 2 — {'да' if c1 else 'НЕТ'}; падение ниже D0 "
              f"{drop * 100:.2f}% ≤ {FIXED_MAX_DROP * 100:g}% — {'да' if c2 else 'НЕТ'} → "
              f"{'ПРОШЁЛ' if c1 and c2 else 'НЕ прошёл'}")


def top_k_days(events: Path, k: int, window: int = 30, lag: int = 3) -> dict[str, set[str]]:
    """Сутки → k монет с наибольшим числом минут обвала ≥5% за `window` суток
    D−lag−window+1…D−lag; при равенстве — по обороту за сутки из последнего события монеты
    до конца окна."""
    per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    turnover: dict[str, list[tuple[int, float]]] = defaultdict(list)
    with events.open() as fh:
        for line in fh:
            e = json.loads(line)
            if e["drop"] <= -0.05:
                per_day[_day(e["t"])][e["sym"]] += 1
            if e.get("qvol_24h") is not None:
                turnover[e["sym"]].append((e["t"], e["qvol_24h"]))
    if not per_day:
        return {}
    for v in turnover.values():
        v.sort()
    first, last = date.fromisoformat(min(per_day)), date.fromisoformat(max(per_day))
    out = {}
    d = first + timedelta(days=window + lag - 1)
    while d <= last + timedelta(days=lag):
        lo, hi = d - timedelta(days=lag + window - 1), d - timedelta(days=lag)
        cnt: dict[str, int] = defaultdict(int)
        for dd in _period_days((lo, hi)):
            for s, n in per_day.get(dd, {}).items():
                cnt[s] += n
        hi_ms = int(datetime(hi.year, hi.month, hi.day, tzinfo=UTC).timestamp() * 1000) + 86_400_000

        def last_turnover(s: str, hi_ms: int = hi_ms) -> float:
            vals = [v for t, v in turnover.get(s, []) if t < hi_ms]
            return vals[-1] if vals else 0.0

        ranked = sorted(cnt, key=lambda s: (-cnt[s], -last_turnover(s), s))
        out[d.isoformat()] = set(ranked[:k])
        d += timedelta(days=1)
    return out


def forward_table(rows: list[dict], period: tuple[date, date], start: date,
                  json_path: Path | None) -> None:
    """Замер вперёд: клетки `FWD_CELLS`, вывод сверх D0, критерии К1/К2 (объявлено 09.10.2026).
    Верх JSON — главная клетка (как до 09.10, его читает отчёт слоя А), все — в `cells`."""
    cells = [_forward_cell(rows, period, start, *c) for c in FWD_CELLS]
    if json_path:
        json_path.write_text(json.dumps({**cells[0], "cells": cells}, ensure_ascii=False,
                                        indent=1), encoding="utf-8")


def _forward_cell(rows: list[dict], period: tuple[date, date], start: date,
                  size: float, cap: int, max_drop: float) -> dict:
    days = _period_days(period)
    b = run_book(rows, size, cap, None, True, fixed=True)
    pnl = [b.day_pnl.get(d, 0.0) for d in days]
    total = b.withdrawn + b.equity - 1.0
    t = _t(pnl)
    drop = 1.0 - b.min_bal
    n_days = (period[1] - start).days + 1
    print(f"\n## Замер вперёд {FWD_VAR}: окно с {start:%d.%m.%Y}, посчитано по "
          f"{period[1]:%d.%m.%Y} ({n_days} сут. = {n_days / 7:.1f} нед.); D0, вывод сверх D0, "
          f"{size * 100:g}% × min(баланс, D0), предел {_cap(cap)}, без стопа. Деньги — % D0")
    print(f"итог {total * 100:+.2f}% D0 (выведено {b.withdrawn * 100:.2f}%, баланс "
          f"{b.equity * 100:.2f}%); t по дням {t:+.2f}; сделок {b.taken} (сверх предела "
          f"{b.overfill}, отрезано пределом {b.cut_cap}, монета в позиции {b.cut_busy}); "
          f"позиций сразу до {b.max_open}; худший день {min(pnl, default=0.0) * 100:+.2f}%")
    print(f"падение ниже D0 по закрытым {drop * 100:.2f}%, с худшей ценой открытых "
          f"{(1 - b.min_bal_pess) * 100:.2f}%")
    print(f"{'неделя с':>10} {'итог':>8} {'сделок':>6} {'сверх':>5} {'худш.день':>9} "
          f"{'мин.баланс':>10}")
    weekly = []
    for i in range(0, len(days), 7):
        wd = days[i:i + 7]
        wp = [b.day_pnl.get(d, 0.0) for d in wd]
        low = min((b.day_low[d] for d in wd if d in b.day_low), default=None)
        n = sum(b.day_taken.get(d, 0) for d in wd)
        over = sum(b.day_over.get(d, 0) for d in wd)
        weekly.append({"from": wd[0], "pnl": sum(wp), "trades": n, "over": over})
        low_s = f"{low * 100:9.2f}%" if low is not None else f"{'—':>10}"
        print(f"{wd[0]:>10} {sum(wp) * 100:>+7.2f}% {n:>6} {over:>5} {min(wp) * 100:>+8.2f}% "
              f"{low_s}")
    k1 = total > 0 and t >= FWD_MIN_T
    k2 = drop <= max_drop
    ready = n_days >= FWD_WEEKS * 7 and b.taken >= FWD_TRADES
    print(f"К1 итог > 0 и t ≥ {FWD_MIN_T:g} — {'да' if k1 else 'нет'}; К2 падение ниже D0 "
          f"{drop * 100:.2f}% ≤ {max_drop * 100:g}% — {'да' if k2 else 'НЕТ'}")
    print("ВЕРДИКТ: " + ("можно выносить (К3–К5 — по боту)" if ready else
          f"рано — до вердикта {max(0, FWD_WEEKS * 7 - n_days)} сут. и "
          f"{max(0, FWD_TRADES - b.taken)} сделок (К2 следится и сейчас)"))
    return {
        "var": FWD_VAR, "size": size, "cap": cap, "max_drop": max_drop,
        "start": start.isoformat(), "through": period[1].isoformat(),
        "days": n_days, "total": total, "t": t, "trades": b.taken, "overfill": b.overfill,
        "drop": drop, "drop_pess": 1 - b.min_bal_pess, "worst_day": min(pnl, default=0.0),
        "last_day": {"day": days[-1], "pnl": pnl[-1],
                     "trades": b.day_taken.get(days[-1], 0)} if days else None,
        "weekly": weekly, "k1": k1, "k2": k2, "ready": ready,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--day", help="только сутки каскада (ответ, пока прогон не кончился)")
    ap.add_argument("--sample", type=Path, help="каталог выборки 03.10 для сверки")
    ap.add_argument("--period", default=f"{PERIOD[0]}:{PERIOD[1]}",
                    help="период счёта ГГГГ-ММ-ДД:ГГГГ-ММ-ДД (по умолчанию 2025-01-01:2026-09-30)")
    ap.add_argument("--cascades", default=CASCADE_DAY,
                    help="сутки каскадов через запятую: отдельные строки и счёт «без каскадов»")
    ap.add_argument("--fixed", action="store_true",
                    help="фиксированный депозит с выводом сверх D0 (проверка 2021–2024)")
    ap.add_argument("--forward", help="замер вперёд: начало окна ГГГГ-ММ-ДД (только limit5|side)")
    ap.add_argument("--json", type=Path, help="с --forward: итог в JSON (сообщение в бот)")
    ap.add_argument("--top-k", type=int, help="только k монет по частоте обвалов за 30 суток")
    a = ap.parse_args()
    if a.day and a.day != CASCADE_DAY:
        ap.error(f"разбор одних суток сделан только для {CASCADE_DAY}")
    period = tuple(date.fromisoformat(x) for x in a.period.split(":"))
    cascades = tuple(a.cascades.split(","))
    by, forced, missing = load(a.out)
    lo, hi = period[0].isoformat(), period[1].isoformat()
    by = {v: [r for r in rows if lo <= r["day"] <= hi] for v, rows in by.items()}
    if a.top_k:
        top = top_k_days(a.out / "events.jsonl", a.top_k)
        n0 = {v: len(r) for v, r in by.items()}
        by = {v: [r for r in rows if r["sym"] in top.get(r["day"], set())]
              for v, rows in by.items()}
        no_top = [d for d in _period_days(period) if d not in top]
        print(f"--top-k {a.top_k}: сделок-кандидатов осталось "
              + ", ".join(f"{v} {len(by[v])} из {n0[v]}" for v in by)
              + (f"; суток без выбора (нет 30 суток истории) {len(no_top)}" if no_top else ""))
    if a.forward:
        coverage(a.out, set(_period_days(period)))
        print(f"дней-символов без ленты aggTrades: {missing}")
        forward_table(by.get(FWD_VAR, []), period, date.fromisoformat(a.forward), a.json)
        return 0
    coverage(a.out, {a.day} if a.day else None)
    print(f"дней-символов без ленты aggTrades: {missing}")
    if a.sample:
        crosscheck(a.out, a.sample)
    if lo <= CASCADE_DAY <= hi:
        cascade_table(by, forced)
    if not a.day:
        res = period_table(by, period, cascades)
        if a.fixed:
            fixed_table(by, period, cascades)
        else:
            verdict(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
