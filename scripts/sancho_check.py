#!/usr/bin/env python
"""Таблица сделок SANCHO D.T.: внутренняя согласованность самоотчёта.

П.8 плана трейдеров, четвёртый пункт порядка (`teaching-traders-2026-10-03.md`). Правила
проверки и вердикта объявлены до прогона — `docs/research/sancho-2026-10-11.md` (коммит
`08d5e77`); здесь только их исполнение.

Источник — снимок листов в HTML-виде (`htmlview`, то, что видит читатель, вместе со ссылками
«Пост №»); CSV из `gviz` — только сверка разбора. Сеть нужна лишь для П5 (`--public`): даты
открытых постов `t.me/SANCHO_DT/N` со страницы `?embed=1`, кэш рядом со снимком.

    py -3 scripts/sancho_check.py --snap ../lab-data/sancho/2026-10-11 [--public]
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.request
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime
from math import prod, sqrt
from pathlib import Path
from statistics import fmean, median

START_DEP = 10_000.0
SEASONS = [f"s{k}" for k in range(1, 9)]
OPEN = {"Активна", "Ожидание"}
PRIVATE = "2339428387"
PUBLIC = "SANCHO_DT"

# допуски из документа
TOL_WR, TOL_RES, TOL_RR, TOL_DEP_REL, TOL_DEP_ROW = 0.05, 0.5, 0.01, 0.005, 2.0
MAX_P2, MAX_P3_MISS, MAX_P4_CHRONO, MAX_P4_INNER = 0.01, 0.01, 0.02, 0.01


# -- разбор снимка -----------------------------------------------------------------------


def num(s: str) -> float | None:
    s = s.replace("\xa0", "").replace(" ", "").replace("$", "").replace(",", ".").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def link(cell: str) -> tuple[str, int] | None:
    m = re.search(r"q=https://t\.me/(?:c/(\d+)|([A-Za-z_]+))/(\d+)", cell)
    if not m:
        return None
    return (m.group(1) or m.group(2), int(m.group(3)))


def text(cell: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()


def parse_sheet(path: Path) -> tuple[dict, list[dict]]:
    """Шапка итогов и строки-сделки листа (только первые 15 колонок — данные)."""
    h = path.read_text(encoding="utf-8")
    head: dict = {}
    rows: list[dict] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
        m = re.search(r'<th id="\d+R(\d+)"', tr)
        if not m:
            continue
        line = int(m.group(1)) + 1  # номер строки листа, как у читателя
        cells: list[str] = []
        for attrs, body in re.findall(r"<td([^>]*)>(.*?)</td>", tr, re.S):
            span = re.search(r'colspan="(\d+)"', attrs)
            if "rowspan" in attrs and len(cells) < 15:
                raise SystemExit(f"{path.name}:{line}: rowspan в колонках данных")
            cells.extend([body] * (int(span.group(1)) if span else 1))
        if len(cells) < 15:
            continue
        t = [text(c) for c in cells[:15]]
        tot = re.search(r"Всего:\s*(\d+)", t[0])
        if tot:
            head = {"n": int(tot.group(1)), "rr": num(t[4]), "wr": num(t[5]),
                    "res": num(t[6]), "dep": num(t[14])}
            continue
        if not t[0] or not re.fullmatch(r"\d\d\.\d\d\.\d{4}", t[1]):
            continue
        rows.append({
            "line": line, "pair": t[0], "date": datetime.strptime(t[1], "%d.%m.%Y").date(),
            "side": t[2], "risk": num(t[3]), "rr": num(t[4]), "out": t[5],
            "res": num(t[6]), "res_raw": t[6], "status": t[7],
            "posts": [link(c) for c in cells[8:14]], "dep": num(t[14]),
        })
    return head, rows


def gviz_rows(path: Path) -> list[tuple[str, str, str]]:
    with path.open(encoding="utf-8") as f:
        out = []
        for r in csv.reader(f):
            if len(r) > 6 and re.fullmatch(r"\d\d\.\d\d\.\d{4}", r[1] or ""):
                out.append((r[0], r[1], r[6]))
        return out


# -- П1: шапка ↔ строки ------------------------------------------------------------------


def p1(head: dict, rows: list[dict], name: str, start_dep: float | None) -> list[str]:
    """Возвращает список несовпавших величин (пустой — пройдено)."""
    bad: list[str] = []
    out = Counter(r["out"] for r in rows)
    st = Counter(r["status"] for r in rows)
    n_def = {
        "(а) все": len(rows),
        "(б) без Активна/Ожидание": sum(r["status"] not in OPEN for r in rows),
        "(в) без Активна/Ожидание/Неактивна":
            sum(r["status"] not in OPEN | {"Неактивна"} for r in rows),
    }
    print(f"  [{name}] строк {len(rows)}; исходы {dict(out)}; статусы {dict(st)}")
    hit = [k for k, v in n_def.items() if v == head["n"]]
    print(f"    Всего: шапка {head['n']}, определения {n_def} -> {hit or 'НЕТ'}")
    if not hit:
        bad.append("Всего")

    w, l_, be = out["✓"], out["✗"], out["б/у"]
    wr_def = {"✓/(✓+✗)": 100 * w / max(w + l_, 1), "✓/(✓+✗+б/у)": 100 * w / max(w + l_ + be, 1)}
    hit = [k for k, v in wr_def.items() if abs(v - head["wr"]) <= TOL_WR]
    print(f"    WR: шапка {head['wr']}, " + ", ".join(f"{k} {v:.2f}" for k, v in wr_def.items())
          + f" -> {hit or 'НЕТ'}")
    if not hit:
        bad.append("WR")

    s = sum(r["res"] or 0 for r in rows)
    ok = abs(s - head["res"]) <= TOL_RES
    print(f"    Результат: шапка {head['res']}, сумма колонки {s:.2f} -> {'да' if ok else 'НЕТ'}")
    if not ok:
        bad.append("Результат")

    rr_all = [r["rr"] for r in rows if r["rr"] is not None]
    rr_w = [r["rr"] for r in rows if r["rr"] is not None and r["out"] == "✓"]
    rr_def = {"все": fmean(rr_all) if rr_all else 0.0, "только ✓": fmean(rr_w) if rr_w else 0.0}
    hit = [k for k, v in rr_def.items() if abs(v - head["rr"]) <= TOL_RR]
    print(f"    RR: шапка {head['rr']}, " + ", ".join(f"{k} {v:.3f}" for k, v in rr_def.items())
          + f" -> {hit or 'НЕТ'}")
    if not hit:
        bad.append("RR")

    if start_dep is not None:
        dep = start_dep * prod(1 + (r["res"] or 0) / 100 for r in rows)
        ok = abs(dep / head["dep"] - 1) <= TOL_DEP_REL
        print(f"    Депозит: шапка {head['dep']:.0f}, сложный процент от {start_dep:.0f} -> "
              f"{dep:.0f} ({100 * (dep / head['dep'] - 1):+.2f}%) -> {'да' if ok else 'НЕТ'}")
        if not ok:
            bad.append("Депозит")
    return bad


# -- П2: строки ↔ легенда ----------------------------------------------------------------


def p2(rows: list[dict], name: str, start_dep: float | None) -> tuple[int, int]:
    viol: dict[int, list[str]] = defaultdict(list)
    overlap = 0
    prev = start_dep
    for r in rows:
        o, x, risk, why = r["out"], r["res"], r["risk"] or 1.0, viol[r["line"]]
        if r["res_raw"] and x is None:
            why.append(f"результат не число «{r['res_raw']}»")
        if o == "✓":
            if x is None or x <= 0:
                why.append(f"✓, результат {x}")
            elif 0 < x <= 0.3:
                overlap += 1
            if x is not None and r["rr"] is not None and x > r["rr"] * risk + 0.01:
                why.append(f"✓ {x} > RR {r['rr']} × риск {risk}")
            if x is not None and r["rr"] is None and r["status"] == "Закрыта":
                why.append("✓ без RR")
        elif o == "✗":
            if x is None or x >= 0 or x < -1.1 * risk:
                why.append(f"✗, результат {x}")
        elif o == "б/у":
            if x is not None and not -0.1 <= x <= 0.3:
                why.append(f"б/у, результат {x}")
        elif o == "б/о":
            if x is not None:
                why.append(f"б/о, результат {x}")
        elif o:
            why.append(f"исход «{o}» вне легенды")
        st = r["status"]
        if o in ("✓", "✗", "б/у") and st not in ("Закрыта", "Активна"):
            why.append(f"{o} при статусе {st}")
        if o in ("✗", "б/у") and st == "Активна":
            why.append(f"{o} при статусе Активна")
        if o == "б/о" and st != "Неактивна":
            why.append(f"б/о при статусе {st}")
        if st == "Неактивна" and o != "б/о":
            why.append(f"Неактивна с исходом «{o}»")
        if st == "Закрыта" and o not in ("✓", "✗", "б/у"):
            why.append(f"Закрыта с исходом «{o}»")
        if st == "Ожидание" and (o or x is not None):
            why.append(f"Ожидание с исходом «{o}» {x}")
        if prev is not None and r["dep"] is not None:
            want = prev * (1 + (x or 0) / 100)
            if abs(r["dep"] - want) > TOL_DEP_ROW:
                why.append(f"депозит {r['dep']:.0f} против {want:.0f} ({r['dep'] - want:+.0f}$)")
        prev = r["dep"] if r["dep"] is not None else prev
    bad = {k: v for k, v in viol.items() if v}
    print(f"  [{name}] нарушений П2: {len(bad)} из {len(rows)} строк "
          f"({100 * len(bad) / max(len(rows), 1):.2f}%); ✓ с результатом в (0; 0.3]: {overlap}")
    for line, why in list(bad.items())[:40]:
        print(f"    строка {line}: {'; '.join(why)}")
    if len(bad) > 40:
        print(f"    … ещё {len(bad) - 40}")
    return len(bad), len(rows)


# -- П3: полная история ↔ сезоны ---------------------------------------------------------


def key(r: dict) -> tuple:
    return (r["pair"], r["date"], r["side"], r["posts"][0])


def p3(full_head: dict, full: list[dict], seasons: dict[str, tuple[dict, list[dict]]]) -> bool:
    ok = True
    by_full: dict[tuple, list[dict]] = defaultdict(list)
    for r in full:
        by_full[key(r)].append(r)
    by_seas: dict[tuple, list[tuple[str, dict]]] = defaultdict(list)
    for s, (_, rows) in seasons.items():
        for r in rows:
            by_seas[key(r)].append((s, r))
    miss_f, miss_s, diff = [], [], []
    for k in by_full.keys() | by_seas.keys():
        a, b = by_full.get(k, []), by_seas.get(k, [])
        for i in range(max(len(a), len(b))):
            if i >= len(b):
                if a[i]["status"] not in OPEN:
                    miss_s.append(a[i])
            elif i >= len(a):
                if b[i][1]["status"] not in OPEN:
                    miss_f.append(b[i])
            else:
                fa, (sn, fb) = a[i], b[i]
                d = [f for f in ("rr", "out", "res", "risk", "status") if fa[f] != fb[f]]
                if d:
                    diff.append((fa["line"], sn, fb["line"],
                                 {f: (fa[f], fb[f]) for f in d}))
    n_s = sum(len(rows) for _, rows in seasons.values())
    bad_rows = len(miss_f) + len(miss_s) + len(diff)
    share = bad_rows / max(len(full), 1)
    print(f"  строк: full {len(full)}, сезоны {n_s}; нет в сезонах {len(miss_s)}, "
          f"нет в full {len(miss_f)}, поля расходятся {len(diff)} ({100 * share:.2f}%)")
    for r in miss_s[:15]:
        print(f"    нет в сезонах: full:{r['line']} {r['pair']} {r['date']} {r['side']} "
              f"{r['out']} {r['res']} {r['status']}")
    for s, r in miss_f[:15]:
        print(f"    нет в full: {s}:{r['line']} {r['pair']} {r['date']} {r['side']} "
              f"{r['out']} {r['res']} {r['status']}")
    for fl, sn, sl, d in diff[:15]:
        print(f"    расходится full:{fl} ↔ {sn}:{sl}: {d}")
    if share > MAX_P3_MISS:
        ok = False

    s_res = sum(h["res"] for h, _ in seasons.values())
    eq = abs(s_res - full_head["res"]) <= TOL_RES
    print(f"  сумма шапок сезонов {s_res:.2f} против full {full_head['res']} -> "
          f"{'да' if eq else 'НЕТ'}")
    ok &= eq
    s_n = sum(h["n"] for h, _ in seasons.values())
    print(f"  сумма «Всего» сезонов {s_n} против full {full_head['n']} (справочно)")

    prev_end: float | None = None
    for s, (h, rows) in seasons.items():
        first = next((r for r in rows if r["dep"] is not None), None)
        start = first["dep"] / (1 + (first["res"] or 0) / 100) if first else None
        end = rows[-1]["dep"] if rows else None
        note = ""
        if prev_end is not None and start is not None:
            gap = start - prev_end
            note = f", разрыв с прошлым сезоном {gap:+.0f}$"
            if abs(gap) > TOL_DEP_ROW:
                ok = False
                note += " НЕТ"
        print(f"  {s}: {rows[0]['date'] if rows else '-'}…{rows[-1]['date'] if rows else '-'}, "
              f"старт ≈{start:.0f}$, конец {end:.0f}$, шапка депозита {h['dep']:.0f}$" + note)
        prev_end = end

    for s, (h, rows) in seasons.items():
        start = None
        first = next((r for r in rows if r["dep"] is not None), None)
        if first:
            start = first["dep"] / (1 + (first["res"] or 0) / 100)
        bad = p1(h, rows, s, start)
        if bad:
            ok = False
            print(f"    -> {s}: не сошлось {bad}")
    return ok


# -- П4: хронология постов ---------------------------------------------------------------


def lnds(xs: list[int]) -> list[int]:
    """Индексы самой длинной неубывающей подпоследовательности."""
    tails: list[int] = []
    tail_idx: list[int] = []
    parent = [-1] * len(xs)
    for i, x in enumerate(xs):
        j = bisect_right(tails, x)
        if j == len(tails):
            tails.append(x)
            tail_idx.append(i)
        else:
            tails[j] = x
            tail_idx[j] = i
        parent[i] = tail_idx[j - 1] if j else -1
    out, k = [], tail_idx[-1] if tail_idx else -1
    while k != -1:
        out.append(k)
        k = parent[k]
    return out[::-1]


def p4(full: list[dict]) -> bool:
    ok = True
    n = len(full)
    chrono_bad = 0
    for ch in (PRIVATE, PUBLIC):
        rs = sorted((r for r in full if r["posts"][0] and r["posts"][0][0] == ch),
                    key=lambda r: (r["date"], r["line"]))
        ids = [r["posts"][0][1] for r in rs]
        keep = set(lnds(ids))
        out = [rs[i] for i in range(len(rs)) if i not in keep]
        chrono_bad += len(out)
        print(f"  канал {ch}: сделок с «Пост №1» {len(rs)}, вне хронологии {len(out)}")
        for r in out[:25]:
            i = rs.index(r)
            nb = [rs[j] for j in range(max(0, i - 2), min(len(rs), i + 3))]
            ctx = ", ".join(f"{x['date']:%d.%m.%y}#{x['posts'][0][1]}" for x in nb)
            print(f"    строка {r['line']} {r['pair']} {r['date']} пост {r['posts'][0][1]}  "
                  f"[{ctx}]")
        if len(out) > 25:
            print(f"    … ещё {len(out) - 25}")
    no_post = sum(1 for r in full if not r["posts"][0])
    print(f"  без ссылки «Пост №1»: {no_post}")
    share = chrono_bad / max(n, 1)
    print(f"  вне хронологии всего {chrono_bad} ({100 * share:.2f}% строк, порог "
          f"{100 * MAX_P4_CHRONO:.0f}%)")
    ok &= share <= MAX_P4_CHRONO

    inner = []
    for r in full:
        by_ch: dict[str, list[int]] = defaultdict(list)
        for p in r["posts"]:
            if p:
                by_ch[p[0]].append(p[1])
        if any(any(b <= a for a, b in zip(v, v[1:], strict=False)) for v in by_ch.values()):
            inner.append(r)
    share_i = len(inner) / max(n, 1)
    print(f"  порядок постов внутри сделки нарушен: {len(inner)} ({100 * share_i:.2f}%)")
    for r in inner[:15]:
        print(f"    строка {r['line']} {r['pair']} {r['date']}: {r['posts']}")
    ok &= share_i <= MAX_P4_INNER

    dup = Counter((r["pair"], r["date"], r["side"]) for r in full)
    dups = {k: v for k, v in dup.items() if v > 1}
    print(f"  справочно: одинаковые (пара, дата, направление): {len(dups)}"
          + (f" — {list(dups.items())[:8]}" if dups else ""))
    p1c = Counter(r["posts"][0] for r in full if r["posts"][0])
    shared = {k: v for k, v in p1c.items() if v > 1}
    print(f"  справочно: один «Пост №1» у разных сделок: {len(shared)}"
          + (f" — {list(shared.items())[:8]}" if shared else ""))
    back = sum(1 for a, b in zip(full, full[1:], strict=False) if b["date"] < a["date"])
    print(f"  справочно: строк листа с датой раньше предыдущей: {back}")
    return ok


# -- П5: даты открытых постов (справочно) ------------------------------------------------


def public_date(n: int, cache: dict) -> str | None:
    k = str(n)
    if k not in cache:
        url = f"https://t.me/{PUBLIC}/{n}?embed=1&mode=tme"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            h = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        except OSError as e:
            print(f"    пост {n}: {e}", file=sys.stderr)
            return None
        m = re.search(r'<time[^>]*datetime="([^"]+)"', h)
        cache[k] = m.group(1) if m else ""
        time.sleep(0.3)
    return cache[k] or None


def p5(full: list[dict], snap: Path) -> None:
    path = snap / "public_posts.json"
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    checked = late_first = early_next = missing = 0
    shown = 0
    try:
        for r in full:
            for i, p in enumerate(r["posts"]):
                if not p or p[0] != PUBLIC:
                    continue
                ts = public_date(p[1], cache)
                if not ts:
                    missing += 1
                    continue
                checked += 1
                d = datetime.fromisoformat(ts).date()
                lag = (d - r["date"]).days
                bad = (i == 0 and lag > 1) or (i > 0 and lag < 0)
                late_first += i == 0 and lag > 1
                early_next += i > 0 and lag < 0
                if bad and shown < 20:
                    shown += 1
                    print(f"    строка {r['line']} {r['pair']} {r['date']} пост №{i + 1} "
                          f"({p[1]}) опубликован {d} ({lag:+d} д)")
    finally:
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"  открытых ссылок проверено {checked}, без даты {missing}; «Пост №1» позже сделки "
          f">1 д: {late_first}; последующий пост раньше даты сделки: {early_next}")


# -- П6: что таблица говорит о себе (справочно) ------------------------------------------


def block_stats(pairs: list[tuple[int, float]]) -> tuple[float, float, int]:
    """Среднее по сделкам, 2σ по блокам, число блоков (как в `brandt_check.py`)."""
    sums: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for blk, x in pairs:
        sums[blk][0] += x
        sums[blk][1] += 1
    nb = len(sums)
    total = sum(cnt for _, cnt in sums.values())
    if nb < 2 or not total:
        return (fmean(x for _, x in pairs) if pairs else 0.0), float("inf"), nb
    m = sum(s for s, _ in sums.values()) / total
    resid = sum((s - m * cnt) ** 2 for s, cnt in sums.values())
    return m, 2 * sqrt(resid * nb / (nb - 1)) / total, nb


def blk(d: date) -> int:
    return d.year * 12 + d.month


def p6(full: list[dict], seasons: dict[str, tuple[dict, list[dict]]]) -> None:
    closed = [r for r in full if r["status"] == "Закрыта"]
    tr = [(blk(r["date"]), r["res"] or 0.0) for r in closed]
    m, s2, nb = block_stats(tr)
    print(f"  закрытых {len(closed)}: среднее {m:+.3f}% ± {s2:.3f} (2σ, {nb} мес.), "
          f"сумма {sum(x for _, x in tr):+.1f}%")
    wins = sorted(r["res"] for r in closed if r["out"] == "✓" and r["res"] is not None)
    loss = [r["res"] for r in closed if r["out"] == "✗" and r["res"] is not None]
    if wins:
        q = [wins[int(p * (len(wins) - 1))] for p in (0.1, 0.25, 0.5, 0.75, 0.9)]
        print(f"  ✓ {len(wins)}: медиана {median(wins):.2f}, квантили 10/25/50/75/90 {q}, "
              f"макс {wins[-1]}")
    if loss:
        exact = sum(abs(x + 1) < 1e-9 for x in loss)
        other = Counter(x for x in loss if abs(x + 1) >= 1e-9)
        print(f"  ✗ {len(loss)}: ровно −1: {exact}, другие {dict(other)}")
    rr = [r["rr"] for r in closed if r["out"] == "✓" and r["rr"] and r["res"] is not None]
    used = [r["res"] / r["rr"] for r in closed if r["out"] == "✓" and r["rr"] and r["res"]]
    if used:
        print(f"  ✓: результат / RR — медиана {median(used):.2f} (доля объявленного хода, "
              f"взятая в среднем), RR ✓ медиана {median(rr):.2f}")
    allsig = len(full)
    bo = sum(r["out"] == "б/о" for r in full)
    print(f"  б/о {bo} из {allsig} сигналов ({100 * bo / max(allsig, 1):.0f}%)")
    comp = START_DEP * prod(1 + (r["res"] or 0) / 100 for r in full)
    print(f"  сумма % {sum(r['res'] or 0 for r in full):+.1f} против сложного процента "
          f"{100 * (comp / START_DEP - 1):+.1f}%")
    for s, (_, rows) in seasons.items():
        c = [(blk(r["date"]), r["res"] or 0.0) for r in rows if r["status"] == "Закрыта"]
        m, s2, nb = block_stats(c)
        w = sum(r["out"] == "✓" for r in rows)
        lo = sum(r["out"] == "✗" for r in rows)
        print(f"    {s}: {rows[0]['date']}…{rows[-1]['date']}, закрытых {len(c)}, ✓{w}/✗{lo}, "
              f"сумма {sum(x for _, x in c):+.1f}, среднее {m:+.3f} ± {s2:.3f}")
    by_month: dict[str, list[float]] = defaultdict(list)
    for r in closed:
        by_month[f"{r['date']:%Y-%m}"].append(r["res"] or 0.0)
    neg = [k for k, v in by_month.items() if sum(v) < 0]
    print(f"  месяцев с суммой < 0: {len(neg)} из {len(by_month)} {neg}")


# -- main --------------------------------------------------------------------------------


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--snap", type=Path, required=True)
    ap.add_argument("--public", action="store_true", help="П5: даты открытых постов (сеть)")
    a = ap.parse_args()

    full_head, full = parse_sheet(a.snap / "full.html")
    seasons = {s: parse_sheet(a.snap / f"{s}.html") for s in SEASONS}
    print(f"Снимок {a.snap}: full {len(full)} строк, шапка {full_head}")

    g = gviz_rows(a.snap / "full.csv")
    mine = [(r["pair"], f"{r['date']:%d.%m.%Y}", r["res_raw"]) for r in full]
    mism = sum(x != y for x, y in zip(g, mine, strict=False)) + abs(len(g) - len(mine))
    print(f"Сверка разбора с gviz: строк {len(g)} против {len(mine)}, расхождений {mism}")

    verdict: dict[str, bool] = {}
    print("\nП1. Шапка ↔ строки (full)")
    verdict["П1"] = not p1(full_head, full, "full", START_DEP)
    print("\nП2. Строки ↔ легенда")
    nb, nr = p2(full, "full", START_DEP)
    verdict["П2"] = nb / max(nr, 1) <= MAX_P2
    for s, (_, rows) in seasons.items():
        p2(rows, s, None)
    print("\nП3. Полная история ↔ сезоны")
    verdict["П3"] = p3(full_head, full, seasons)
    print("\nП4. Хронология постов")
    verdict["П4"] = p4(full)
    if a.public:
        print("\nП5. Даты открытых постов (справочно)")
        p5(full, a.snap)
    print("\nП6. Что таблица говорит о себе (справочно)")
    p6(full, seasons)

    print("\nВердикт:", ", ".join(f"{k} {'пройден' if v else 'НЕ пройден'}"
                                  for k, v in verdict.items()))
    print("ТАБЛИЦА", "СОГЛАСОВАНА" if all(verdict.values()) else "НЕ СОГЛАСОВАНА")


if __name__ == "__main__":
    main()
