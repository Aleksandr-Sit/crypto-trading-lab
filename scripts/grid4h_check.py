#!/usr/bin/env python
"""Нейтральная сетка на 4 часа на альт-перпах Binance: проверка на истории (п.9, автомат №1).

Правила и вердикт заморожены ДО прогона — `docs/research/open-automata-anno-2026-10-25.md`,
раздел 4 (коммит 90a5ab8). Скрипт их только исполняет; правка правил после прогона — новый
кандидат, а не эта проверка.

Стадии (каждая дописывает свои файлы и при повторе продолжает с места остановки):

    python scripts/grid4h_check.py select   --out D --from 2025-01 --to 2026-09
    python scripts/grid4h_check.py simulate --out D [--scope all|main]
    python scripts/grid4h_check.py report   --out D [--oos]

* `select` — часовые месячные архивы ВСЕХ USDT-перпов data.binance.vision (с делистнутыми,
  без `TRADIFI_PERPETUAL`) → 4-часовые блоки в кэше (`--cache`, по умолчанию D/../cache) → на
  каждой границе 00/04/…/20 UTC размах прошлой 4-часовой свечи (макс high / мин low − 1) и
  оборот за 24 ч (quote volume 24 часовых баров) → `lists.jsonl`: топ-20 по размаху среди
  оборота ≥ 5 млн USDT и контроль — случайные 10 с тем же фильтром (зерно — `--seed` и
  граница: добавление месяцев не меняет выборку уже посчитанных).
* `simulate` — минутные архивы только тех монет-дней, что попали в списки (монета с 12+
  нужными днями в месяце — месячный архив, иначе дневные). Сетка — `play`. Итог запуска — в
  долях порции, построчно в `sim.jsonl`. `--scope all` — главная клетка и все соседи
  (`CELLS`) для топ-10, главная клетка для мест 11–20 и случайных 10; `--scope main` —
  только главная клетка у топ-10 (вне выборки, без перебора).
* `report` — вердикт по пунктам раздела 4: t по запускам (все монеты одной границы — одно
  наблюдение) и по дням, месяцы, плато, контроль выбора, худшие 5 запусков, 10.10.2025,
  ликвидация при ×5 (уход на 20% против средней), сверка фандинга внутри окна.

Механика сетки (`play`), всё — по таблице раздела 4:

* старт — открытие минуты hh:02 (P0); лимитки на ±1 шаг (соседняя клетка: ±0.5 шага);
* уровни геометрические, как у бота: вниз ×(1 − шаг), вверх ×1/(1 − шаг); порция — одно и то
  же количество монет (1/P0), до `levels` порций в сторону;
* после каждого исполнения — противоположная лимитка на соседнем уровне и следующая
  докупка: заявки всегда на уровень выше и ниже последнего исполнения;
* лимитка исполняется, только если цена прошла СТРОГО за уровень (очередь не наша);
* если в минуте задето хоть что-то, разыгрываются оба пути «открытие → максимум → минимум
  → закрытие» и «открытие → минимум → максимум → закрытие», берётся худший для нас по итогу
  на закрытии минуты (докупка и цель в одной минуте — сначала худшее);
* жизнь «N ч» = N·60 − 2 мин (главная 238): рынок по закрытию последней минуты, до границы.

Издержки: мейкер 0.02% на исполнение лимитки, тейкер 0.05% + 0.05% проскальзывание на
рыночное закрытие; тяжёлые — 0.04% и 0.10% + 0.20%. Фандинг в итог не входит (позиции нет на
границе 4 ч); сверка — по архиву ставок: выплаты ВНУТРИ окна у монет с часовым фандингом.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from array import array
from bisect import bisect_left
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, stdev

ARCHIVE = "https://data.binance.vision/data/futures/um"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
UA = {"User-Agent": "Mozilla/5.0 crypto-trading-lab"}
TRADFI = Path(__file__).resolve().parents[2] / "lab-data/open-automata/bn_tradfi_symbols.json"

H1, H4 = 3_600_000, 14_400_000
START_MS = 120_000  # вход от открытия минуты hh:02
MIN_QV = 5e6  # оборот за 24 ч, USDT
N_TOP, N_RND = 20, 10
DAILY_MAX = 12  # нужных дней в месяце меньше — дневные архивы, иначе месячный
COSTS = {"main": (0.0002, 0.0005 + 0.0005), "heavy": (0.0004, 0.0010 + 0.0020)}
LIQ = 0.20  # ×5: уход на 20% против средней цены входа
INF = float("inf")

# клетка → (шаг, уровней в сторону, жизнь в минутах, отступ стартовых лимиток в шагах)
CELLS: dict[str, tuple[float, int, int, float]] = {"main": (0.03, 10, 238, 1.0)}
for _s in (0.02, 0.025, 0.03, 0.035, 0.04):
    for _h in (1, 2, 4, 8):
        if (_s, _h) != (0.03, 4):
            CELLS[f"s{_s * 100:g}h{_h}"] = (_s, 10, _h * 60 - 2, 1.0)
CELLS |= {"start0.5": (0.03, 10, 238, 0.5), "lev5": (0.03, 5, 238, 1.0),
          "lev15": (0.03, 15, 238, 1.0)}
FUND_CELLS = ("main", "s3h8")  # у них считается фандинг внутри окна (сверка)
PLATEAU = [("main" if (s, h) == ("3", 4) else f"s{s}h{h}")
           for s in ("2.5", "3", "3.5") for h in (2, 4, 8)]


# ---------------------------------------------------------------- сеть и архивы

def _get(url: str, tries: int = 5) -> bytes | None:
    url = urllib.parse.quote(url, safe=":/?=&%")  # есть символы-иероглифы
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 ** k)
        except Exception:  # noqa: BLE001 — сеть: повтор
            time.sleep(2 ** k)
    raise RuntimeError(f"не скачалось: {url}")


def universe() -> list[str]:
    """Все USDT-перпы из каталога месячных архивов, включая делистнутые; без срочных (`_`)."""
    out, marker = [], ""
    while True:
        xml = _get(f"{LISTING}?delimiter=/&prefix=data/futures/um/monthly/klines/"
                   f"&marker={marker}").decode()
        out += re.findall(r"<Prefix>data/futures/um/monthly/klines/([^/<]+)/</Prefix>", xml)
        if "<IsTruncated>true</IsTruncated>" not in xml:
            break
        marker = re.findall(r"<NextMarker>([^<]+)</NextMarker>", xml)[-1]
    return sorted({s for s in out if s.endswith("USDT") and "_" not in s})


def _sym_months(sym: str) -> list[str]:
    """Месяцы, за которые у монеты есть часовой месячный архив (одним запросом листинга)."""
    xml = _get(f"{LISTING}?prefix=data/futures/um/monthly/klines/{sym}/1h/").decode()
    return sorted(set(re.findall(rf"{re.escape(sym)}-1h-(\d{{4}}-\d{{2}})\.zip</Key>", xml)))


def _lines(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob)) as z, z.open(z.namelist()[0]) as fh:
        for line in fh:
            f = line.split(b",")
            if f[0].strip().isdigit():  # у новых архивов есть заголовок
                yield f


def _ms(x: bytes) -> int:
    v = int(x)
    return v // 1000 if v > 10 ** 14 else v  # микросекунды в новых архивах — на всякий случай


def _months(a: str, b: str) -> list[str]:
    y, m = map(int, a.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _prev_month(m: str) -> str:
    y, mm = map(int, m.split("-"))
    return f"{y - 1:04d}-12" if mm == 1 else f"{y:04d}-{mm - 1:02d}"


def _next_month(m: str) -> str:
    y, mm = map(int, m.split("-"))
    return f"{y + 1:04d}-01" if mm == 12 else f"{y:04d}-{mm + 1:02d}"


def _ts(m: str) -> int:
    return int(datetime.strptime(m, "%Y-%m").replace(tzinfo=UTC).timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _day(ms: int) -> str:
    return _dt(ms).strftime("%Y-%m-%d")


def _month(ms: int) -> str:
    return _dt(ms).strftime("%Y-%m")


# ---------------------------------------------------------------- select

def blocks(rows) -> list[list]:
    """Часовые бары (время, high, low, quote volume) → 4-часовые блоки UTC
    [начало, макс high, мин low, сумма оборота, число часов]."""
    acc: dict[int, list] = {}
    for t, hi, lo, qv in rows:
        b = t // H4 * H4
        x = acc.get(b)
        if x is None:
            acc[b] = [b, hi, lo, qv, 1]
        else:
            x[1], x[2], x[3], x[4] = max(x[1], hi), min(x[2], lo), x[3] + qv, x[4] + 1
    return [acc[b] for b in sorted(acc)]


def _h4_job(sym: str, month: str) -> tuple[str, str, list | None]:
    blob = _get(f"{ARCHIVE}/monthly/klines/{sym}/1h/{sym}-1h-{month}.zip")
    if blob is None:
        return sym, month, None
    rows = [(_ms(f[0]), float(f[2]), float(f[3]), float(f[7])) for f in _lines(blob)]
    return sym, month, [[b, float(f"{hi:.8g}"), float(f"{lo:.8g}"), round(qv), n]
                        for b, hi, lo, qv, n in blocks(rows)]


def select_at(t: int, bs: dict[str, dict[int, tuple]], seed: int, top: int = N_TOP,
              rnd: int = N_RND, min_qv: float = MIN_QV) -> dict:
    """Списки на границе `t` (мс): прошлая 4-часовая свеча — блок [t − 4 ч, t), оборот —
    шесть блоков [t − 24 ч, t). `bs`: монета → {начало блока: (high, low, оборот, часов)}."""
    elig = []
    for s, b in bs.items():
        cur = b.get(t - H4)
        if cur is None or cur[1] <= 0:
            continue
        qv = sum(b[x][2] for x in range(t - 6 * H4, t, H4) if x in b)
        if qv >= min_qv:
            elig.append((cur[0] / cur[1] - 1, s, qv))
    elig.sort(key=lambda e: (-e[0], e[1]))
    names = sorted(s for _, s, _ in elig)
    pick = random.Random(f"{seed}:{t}").sample(names, min(rnd, len(names)))
    return {"t": t, "n": len(elig), "top": [[s, round(r, 6), round(q)] for r, s, q in elig[:top]],
            "rnd": pick}


def _tradfi(path: Path) -> set[str]:
    if not path.exists():
        print(f"ВНИМАНИЕ: нет справочника TradFi {path} — перпы акций не исключены", flush=True)
        return set()
    return set(json.loads(path.read_text(encoding="utf-8"))["symbols"])


def stage_select(out: Path, cache: Path, a: str, b: str, workers: int, seed: int,
                 tradfi: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    (cache / "h4").mkdir(parents=True, exist_ok=True)
    months = _months(_prev_month(a), b)  # прошлый месяц — оборот за сутки у первых границ
    tf = _tradfi(tradfi)
    mp = cache / "months_1h.json"
    if mp.exists():
        avail = json.loads(mp.read_text(encoding="utf-8"))
    else:
        syms = universe()
        print(f"вселенная: {len(syms)} USDT-перпов; листинг месяцев…", flush=True)
        with ThreadPoolExecutor(workers) as pool:
            avail = dict(zip(syms, pool.map(_sym_months, syms), strict=True))
        mp.write_text(json.dumps(avail), encoding="utf-8")
    syms = sorted(s for s in avail if s not in tf)
    print(f"монет {len(syms)} (без TradFi: исключено {len(avail) - len(syms)})", flush=True)
    done_p = cache / "h4" / "done.txt"
    done = set(done_p.read_text(encoding="utf-8").split()) if done_p.exists() else set()
    jobs = [(s, m) for m in months for s in syms if m in avail[s] and f"{s}:{m}" not in done]
    print(f"месяцев {len(months)}, часовых архивов к загрузке {len(jobs)} "
          f"(готово ранее {len(done)})", flush=True)
    fail, t0 = 0, time.time()
    files: dict[str, io.TextIOWrapper] = {}
    with ThreadPoolExecutor(workers) as pool, done_p.open("a", encoding="utf-8") as fd:
        futs = [pool.submit(_h4_job, s, m) for s, m in jobs]
        for k, f in enumerate(as_completed(futs), 1):
            try:
                sym, month, bl = f.result()
            except RuntimeError as e:
                print(f"  ПРОПУСК {e}", flush=True)
                fail += 1
                continue
            if bl:
                if month not in files:
                    files[month] = (cache / "h4" / f"{month}.jsonl").open("a", encoding="utf-8")
                files[month].write(json.dumps({"s": sym, "b": bl}) + "\n")
                files[month].flush()
            fd.write(f"{sym}:{month}\n")
            fd.flush()
            if k % 500 == 0:
                print(f"  {k}/{len(jobs)}  {(time.time() - t0) / 60:.1f} мин", flush=True)
    for fh in files.values():
        fh.close()
    if fail:
        print(f"не скачалось {fail} — повторить тем же запуском; списки не пишутся", flush=True)
        return 1
    n = 0
    with (out / "lists.jsonl").open("w", encoding="utf-8") as fo:
        for m in _months(a, b):
            bs: dict[str, dict[int, tuple]] = defaultdict(dict)
            for mm in (_prev_month(m), m):
                p = cache / "h4" / f"{mm}.jsonl"
                for line in (p.read_text(encoding="utf-8").splitlines() if p.exists() else ()):
                    r = json.loads(line)
                    if r["s"] in tf:
                        continue
                    for blk in r["b"]:
                        bs[r["s"]][blk[0]] = tuple(blk[1:])
            t, end = _ts(m), _ts(_next_month(m))
            while t < end:
                fo.write(json.dumps(select_at(t, bs, seed)) + "\n")
                n += 1
                t += H4
    print(f"списки: {n} границ → {out / 'lists.jsonl'}", flush=True)
    return 0


# ---------------------------------------------------------------- сетка

def _leg(st: list, to: float, up: bool, g: float, k: int) -> None:
    """Цена идёт монотонно до `to`; исполняются заявки по пути. Состояние `st` =
    [позиция в порциях (+ лонг), показатель последнего исполнения c, отступ d до следующих
    заявок, средняя цена входа, деньги, оборот лимиток, исполнений, макс |позиция|,
    худший уход против средней]. Цены — в долях P0."""
    p, c, d, avg, cash, mk, nf, mp, loss = st
    if up:
        while p > -k:
            s = g ** (c + d)
            if not to > s:
                break
            if p < 0:
                loss = max(loss, s / avg - 1)  # шорт держался, пока цена шла до уровня
            cash += s
            mk += s
            nf += 1
            if p < 0:
                avg = (avg * -p + s) / (1 - p)
            elif p == 0:
                avg = s
            p -= 1
            if p == 0:
                avg = 0.0
            c, d, mp = c + d, 1.0, max(mp, -p)
        if p < 0:
            loss = max(loss, to / avg - 1)
    else:
        while p < k:
            s = g ** (c - d)
            if not to < s:
                break
            if p > 0:
                loss = max(loss, 1 - s / avg)
            cash -= s
            mk += s
            nf += 1
            if p > 0:
                avg = (avg * p + s) / (p + 1)
            elif p == 0:
                avg = s
            p += 1
            if p == 0:
                avg = 0.0
            c, d, mp = c - d, 1.0, max(mp, p)
        if p > 0:
            loss = max(loss, 1 - to / avg)
    st[:] = [p, c, d, avg, cash, mk, nf, mp, loss]


def _minute(st: list, op: float, hi: float, lo: float, cl: float, g: float, k: int) -> list:
    """Минута с исполнениями: оба порядка экстремумов, берётся худший по итогу на закрытии."""
    a, b = list(st), list(st)
    _leg(a, hi, True, g, k)
    _leg(a, lo, False, g, k)
    _leg(a, cl, True, g, k)
    _leg(b, lo, False, g, k)
    _leg(b, hi, True, g, k)
    _leg(b, cl, False, g, k)
    return b if b[4] + b[0] * cl < a[4] + a[0] * cl else a


def play(t, o, h, lo_, c, i0: int, t0: int, step: float, k: int, life: int,
         off: float = 1.0, marks: bool = False) -> tuple[list, list]:
    """Одна сетка: минутные бары монеты (массивы по времени), `i0` — индекс минуты старта,
    `t0` — граница 4 ч (мс), жизнь `life` минут от старта.

    → ([итог до издержек, оборот лимиток, оборот закрытия рынком, исполнений,
    макс |позиция|, худший уход против средней, позиция на закрытии, обрыв данных],
    [(время выплаты, позиция, цена), …] на целых часах внутри окна — если `marks`).
    Всё в долях порции (порция — 1/P0 монеты, то есть 1 единица номинала по цене старта)."""
    g = 1 / (1 - step)
    inv = 1 / o[i0]
    end = t[i0] + life * 60_000
    st = [0, 0.0, off, 0.0, 0.0, 0.0, 0, 0, 0.0]
    up, dn = g ** off, g ** -off
    out_marks = []
    i, last, n = i0, i0, len(t)
    while i < n and t[i] < end:
        hi, lo = h[i] * inv, lo_[i] * inv
        if hi > up or lo < dn:
            st = _minute(st, o[i] * inv, hi, lo, c[i] * inv, g, k)
            p = st[0]
            up = g ** (st[1] + st[2]) if p > -k else INF
            dn = g ** (st[1] - st[2]) if p < k else -1.0
        elif st[0]:
            p, avg = st[0], st[3]
            x = 1 - lo / avg if p > 0 else hi / avg - 1
            if x > st[8]:
                st[8] = x
        if marks and (t[i] + 60_000 - t0) % H1 == 0 and t[i] + 60_000 < end:
            out_marks.append((t[i] + 60_000, st[0], c[i] * inv))
        last = i
        i += 1
    cl = c[last] * inv
    p = st[0]
    cut = t[last] + 60_000 < end  # данные кончились раньше (делистинг): закрытие по последней
    return [st[4] + p * cl, st[5], abs(p) * cl, st[6], st[7], st[8], p, int(cut)], out_marks


def net(v: list, costs: str = "main") -> float:
    mk, tk = COSTS[costs]
    return v[0] - mk * v[1] - tk * v[2]


# ---------------------------------------------------------------- simulate

def _bars(blobs: list[bytes]) -> tuple[array, array, array, array, array]:
    rows = sorted((_ms(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[4]))
                  for blob in blobs for f in _lines(blob))
    t, o, h, lo, c = array("q"), array("d"), array("d"), array("d"), array("d")
    for r in rows:
        t.append(r[0])
        o.append(r[1])
        h.append(r[2])
        lo.append(r[3])
        c.append(r[4])
    return t, o, h, lo, c


def _funding(sym: str, month: str) -> dict[int, float] | None:
    """Ставки фандинга за месяц: минута выплаты → ставка. None — архива нет."""
    blob = _get(f"{ARCHIVE}/monthly/fundingRate/{sym}/{sym}-fundingRate-{month}.zip")
    if blob is None:
        return None
    return {(_ms(f[0]) + 30_000) // 60_000 * 60_000: float(f[2]) for f in _lines(blob)}


def _sim_job(sym: str, month: str, launches: dict[int, bool]) -> tuple[str, str, list[dict]]:
    """Все запуски монеты за месяц. `launches`: граница → True (все клетки) / False (главная)."""
    lmax = max(v[2] for v in CELLS.values())
    days = set()
    for t0, full in launches.items():
        a = t0 + START_MS
        days |= {_day(a), _day(a + (lmax if full else CELLS["main"][2]) * 60_000 - 1)}
    inside = sorted(d for d in days if d.startswith(month))
    spill = sorted(d for d in days if not d.startswith(month))
    blobs = []
    if len(inside) >= DAILY_MAX:
        blob = _get(f"{ARCHIVE}/monthly/klines/{sym}/1m/{sym}-1m-{month}.zip")
        if blob is not None:
            blobs.append(blob)
            inside = []
    for d in inside + spill:
        blob = _get(f"{ARCHIVE}/daily/klines/{sym}/1m/{sym}-1m-{d}.zip")
        if blob is not None:
            blobs.append(blob)
    t, o, h, lo, c = _bars(blobs)
    fund = _funding(sym, month)
    out = []
    for t0, full in sorted(launches.items()):
        i0 = bisect_left(t, t0 + START_MS)
        if i0 >= len(t) or t[i0] != t0 + START_MS:
            out.append({"s": sym, "t": t0, "miss": 1})
            continue
        rec = {"s": sym, "t": t0, "p0": o[i0], "c": {}}
        for name in (CELLS if full else ("main",)):
            step, k, life, off = CELLS[name]
            v, marks = play(t, o, h, lo, c, i0, t0, step, k, life, off, name in FUND_CELLS)
            if name in FUND_CELLS:
                if fund is None:
                    v += [None, None]
                else:
                    paid = [(fund[tm], p, px) for tm, p, px in marks if tm in fund]
                    # лонг платит при положительной ставке
                    v += [round(-sum(r * p * px for r, p, px in paid), 7), len(paid)]
            rec["c"][name] = [round(x, 7) if isinstance(x, float) else x for x in v]
        out.append(rec)
    return sym, month, out


def launches_from(lists: list[dict], scope: str) -> dict[tuple[str, str], dict[int, bool]]:
    """(монета, месяц) → {граница: все клетки?}. `all`: топ-10 — все клетки, места 11–20 и
    случайные 10 — главная; `main`: только топ-10, только главная."""
    jobs: dict[tuple[str, str], dict[int, bool]] = defaultdict(dict)
    for row in lists:
        t0, m = row["t"], _month(row["t"])
        top = [x[0] for x in row["top"]]
        if scope == "main":
            for s in top[:10]:
                jobs[(s, m)][t0] = False
            continue
        for r, s in enumerate(top[:N_TOP]):
            jobs[(s, m)][t0] = jobs[(s, m)].get(t0, False) or r < 10
        for s in row["rnd"]:
            jobs[(s, m)].setdefault(t0, False)
    return jobs


def stage_simulate(out: Path, workers: int, scope: str) -> int:
    lists = [json.loads(x) for x in (out / "lists.jsonl").read_text(encoding="utf-8").splitlines()]
    jobs = launches_from(lists, scope)
    done_p, sim_p = out / "sim_done.txt", out / "sim.jsonl"
    done = set(done_p.read_text(encoding="utf-8").split()) if done_p.exists() else set()
    todo = sorted((k for k in jobs if f"{k[0]}:{k[1]}" not in done), key=lambda k: (k[1], k[0]))
    n_l = sum(len(v) for v in jobs.values())
    print(f"границ {len(lists)}, монето-запусков {n_l}, монето-месяцев {len(jobs)}, "
          f"осталось {len(todo)} (scope={scope})", flush=True)
    fail, t0 = 0, time.time()
    # процессы, а не потоки: разбор минуток держит GIL
    with (ProcessPoolExecutor(workers) as pool, sim_p.open("a", encoding="utf-8") as fs,
          done_p.open("a", encoding="utf-8") as fd):
        futs = {pool.submit(_sim_job, s, m, jobs[(s, m)]): (s, m) for s, m in todo}
        for k, f in enumerate(as_completed(futs), 1):
            s, m = futs[f]
            try:
                _, _, res = f.result()
            except Exception as e:  # noqa: BLE001 — сеть или память: месяц не теряется
                print(f"  ПРОПУСК {s} {m}: {type(e).__name__} {e}", flush=True)
                fail += 1
                continue
            for r in res:
                fs.write(json.dumps(r, separators=(",", ":")) + "\n")
            fs.flush()
            fd.write(f"{s}:{m}\n")
            fd.flush()
            if k % 100 == 0:
                el = (time.time() - t0) / 60
                print(f"  {k}/{len(todo)}  {el:.1f} мин, осталось ≈{el / k * (len(todo) - k):.0f}",
                      flush=True)
    print("готово" + (f"; не скачалось {fail} — повторить тем же запуском" if fail else ""),
          flush=True)
    return 1 if fail else 0


# ---------------------------------------------------------------- report

SELECTIONS = {
    "top10": lambda row: [x[0] for x in row["top"][:10]],
    "top5": lambda row: [x[0] for x in row["top"][:5]],
    "top20": lambda row: [x[0] for x in row["top"][:20]],
    "rnd10": lambda row: row["rnd"],
}


def launch_sums(lists: list[dict], sims: dict, sel: str, cell: str,
                costs: str = "main") -> dict[int, float]:
    """Граница → сумма итогов по монетам выбора (доли порции). Граница без разыгранных
    монет не входит."""
    out = {}
    for row in lists:
        vals = [net(r["c"][cell], costs) for s in SELECTIONS[sel](row)
                if (r := sims.get((s, row["t"]))) and cell in r.get("c", {})]
        if vals:
            out[row["t"]] = sum(vals)
    return out


def tstat(xs: list[float]) -> tuple[float, float, float]:
    """Среднее, ошибка среднего, t."""
    if len(xs) < 2:
        return (xs[0] if xs else float("nan")), float("nan"), float("nan")
    m, se = fmean(xs), stdev(xs) / math.sqrt(len(xs))
    return m, se, (m / se if se else float("nan"))


def by_day(sums: dict[int, float]) -> list[float]:
    d: dict[str, float] = defaultdict(float)
    for t0, v in sums.items():
        d[_day(t0)] += v
    return list(d.values())


def by_month(sums: dict[int, float]) -> dict[str, list[float]]:
    d: dict[str, list[float]] = defaultdict(list)
    for t0, v in sums.items():
        d[_month(t0)].append(v)
    return dict(sorted(d.items()))


HEAD = (f"{'клетка':<16}{'выбор':<7}{'запусков':>9}{'ср.%':>8}{'шум%':>7}{'t':>6}"
        f"{'t дни':>7}{'мес+':>7}{'тяж.%':>8}{'t тяж':>7}")


def _row(lists, sims, sel, cell) -> tuple[str, float, float]:
    s = launch_sums(lists, sims, sel, cell)
    if not s:
        return f"{cell:<16}{sel:<7}{0:>9}", float("nan"), float("nan")
    m, se, t = tstat(list(s.values()))
    _, _, td = tstat(by_day(s))
    mo = by_month(s)
    pos = sum(fmean(v) > 0 for v in mo.values())
    hm, _, ht = tstat(list(launch_sums(lists, sims, sel, cell, "heavy").values()))
    return (f"{cell:<16}{sel:<7}{len(s):>9}{m * 100:>+8.2f}{se * 100:>7.2f}{t:>+6.1f}{td:>+7.1f}"
            f"{f'{pos}/{len(mo)}':>7}{hm * 100:>+8.2f}{ht:>+7.1f}"), m, t


def stage_report(out: Path, oos: bool) -> int:
    lists = [json.loads(x) for x in (out / "lists.jsonl").read_text(encoding="utf-8").splitlines()]
    sims: dict[tuple[str, int], dict] = {}
    for line in (out / "sim.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        sims[(r["s"], r["t"])] = r
    recs = list(sims.values())
    miss = sum("miss" in r for r in recs)
    cut = sum(1 for r in recs if "c" in r and r["c"]["main"][7])
    span = f"{_day(lists[0]['t'])} … {_day(lists[-1]['t'])}"
    print(f"# сетка 4 ч на альт-перпах Binance — {'ВНЕ ВЫБОРКИ' if oos else 'выборка'} {span}")
    print(f"границ {len(lists)}; монето-запусков {len(recs)}, без минутки на старте {miss}, "
          f"с обрывом данных внутри окна (делистинг) {cut}")
    el = [row["n"] for row in lists]
    print(f"монет с оборотом ≥ 5 млн на границе: медиана {sorted(el)[len(el) // 2]}, "
          f"мин {min(el)}; итог запуска — сумма по монетам границы, в % порции\n")

    print("## клетки (издержки главные; «тяж.» — тяжёлые)")
    print(HEAD)
    line, m_main, t_main = _row(lists, sims, "top10", "main")
    print(line)
    res = {"main": (m_main, t_main)}
    if not oos:
        for cell in CELLS:
            if cell != "main":
                line, m, t = _row(lists, sims, "top10", cell)
                res[cell] = (m, t)
                print(line)
        for sel in ("top5", "top20", "rnd10"):
            print(_row(lists, sims, sel, "main")[0])

    keys = {(s, row["t"]) for row in lists for s in SELECTIONS["top10"](row)}
    main = [r for r in recs if "c" in r and (r["s"], r["t"]) in keys]
    mv = [r["c"]["main"] for r in main]
    print(f"\n## монето-запуск главной клетки ({len(mv)}): ср. "
          f"{fmean(net(v) for v in mv) * 100:+.3f}% порции, в плюсе "
          f"{sum(net(v) > 0 for v in mv) / len(mv):.0%}, без исполнений "
          f"{sum(v[3] == 0 for v in mv) / len(mv):.0%}, закрыты рынком с позицией "
          f"{sum(v[6] != 0 for v in mv) / len(mv):.0%}, ср. исполнений "
          f"{fmean(v[3] for v in mv):.1f}, макс |позиция| ≥ 4 у "
          f"{sum(v[4] >= 4 for v in mv) / len(mv):.1%}")

    s_main = launch_sums(lists, sims, "top10", "main")
    print("\n## главная клетка по месяцам: ср. итог запуска, % порции (запусков)")
    for mo, v in by_month(s_main).items():
        print(f"{mo}  {fmean(v) * 100:+8.2f}  ({len(v)})")

    print("\n## худшие 5 запусков (граница, сумма по монетам, худшие монеты)")
    worst = sorted(s_main.items(), key=lambda kv: kv[1])[:5]
    for t0, v in worst:
        row = next(x for x in lists if x["t"] == t0)
        coins = sorted((net(sims[(s, t0)]["c"]["main"]), s) for s in SELECTIONS["top10"](row)
                       if "c" in sims.get((s, t0), {}))[:3]
        print(f"{_dt(t0):%Y-%m-%d %H:%M}  {v * 100:+8.1f}  "
              + ", ".join(f"{s} {x * 100:+.1f}" for x, s in coins))
    d1010 = {t0: v for t0, v in s_main.items() if _day(t0) == "2025-10-10"}
    if d1010:
        print("10.10.2025: " + ", ".join(f"{_dt(t0):%H:%M} {v * 100:+.1f}"
                                         for t0, v in sorted(d1010.items()))
              + f"; за день {sum(d1010.values()) * 100:+.1f}")

    liq = sorted(((r["c"]["main"][5], r["s"], r["t"]) for r in main), reverse=True)
    n_liq = sum(x >= LIQ for x, _, _ in liq)
    print(f"\n## ликвидация при ×5 (уход на {LIQ:.0%} против средней): {n_liq} монето-запусков; "
          f"уход ≥10% у {sum(x >= 0.10 for x, _, _ in liq)}, ≥15% у "
          f"{sum(x >= 0.15 for x, _, _ in liq)}")
    for x, s, t0 in liq[:5]:
        print(f"  {s} {_dt(t0):%Y-%m-%d %H:%M}: уход {x:.1%}, итог "
              f"{net(sims[(s, t0)]['c']['main']) * 100:+.1f}% порции")

    print("\n## сверка фандинга: выплаты ВНУТРИ окна (в итог не входят по правилам)")
    for cell in FUND_CELLS:
        vs = [r["c"][cell] for r in main if cell in r["c"]]
        known = [v for v in vs if v[8] is not None]
        if not known:
            continue
        hit = [v for v in known if v[9]]
        print(f"{cell}: монето-запусков {len(vs)}, ставки известны у {len(known)}; с выплатой "
              f"внутри окна {len(hit)} ({len(hit) / len(known):.1%}); фандинг в среднем на "
              f"монето-запуск {fmean(v[8] for v in known) * 100:+.4f}% порции против итога "
              f"{fmean(net(v) for v in vs) * 100:+.3f}%")

    print("\n## ВЕРДИКТ по разделу 4 (заморожен 25.10.2026)")
    ok_main = m_main > 0 and t_main >= 2
    print(f"* средний итог запуска {m_main * 100:+.2f}% порции, t {t_main:+.1f}: "
          f"{'> 0 и t ≥ 2' if ok_main else 'НЕ (> 0 и t ≥ 2)'}")
    print(f"* ликвидаций при ×5: {n_liq} → {'failed независимо от среднего' if n_liq else 'нет'}")
    if oos:
        verdict = ok_main and not n_liq
        print(f"ИТОГ вне выборки: {'прошла' if verdict else 'НЕ прошла'}")
        return 0
    mo = by_month(s_main)
    pos = sum(fmean(v) > 0 for v in mo.values())
    ok_mo = pos >= len(mo) * 2 / 3
    print(f"* положительных месяцев {pos}/{len(mo)}: {'≥ 2/3' if ok_mo else '< 2/3'}")
    signs = {c: res[c][0] > 0 for c in PLATEAU if c in res}
    ok_pl = len(signs) == len(PLATEAU) and all(signs.values())
    print("* плато (шаг 2.5–3.5% × жизнь 2–8 ч, все > 0): "
          + ", ".join(f"{c} {res[c][0] * 100:+.2f}" for c in PLATEAU if c in res)
          + f" → {'да' if ok_pl else 'нет'}")
    rnd = launch_sums(lists, sims, "rnd10", "main")
    diff = [s_main[t0] - rnd[t0] for t0 in s_main if t0 in rnd]
    dm, dse, dt = tstat(diff)
    print(f"* выбор против случайных 10: разность {dm * 100:+.2f}% порции на запуск, шум "
          f"{dse * 100:.2f}, t {dt:+.1f} → "
          f"{'выбор что-то даёт' if dt > 2 else 'выбор ничего не даёт (правило — любая сетка)'}")
    verdict = ok_main and ok_mo and ok_pl and not n_liq
    print(f"ИТОГ выборки: {'эффект есть — дальше вне выборки' if verdict else 'failed'}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--from", dest="a", required=True)
    s.add_argument("--to", dest="b", required=True)
    s.add_argument("--cache", type=Path, help="кэш 4-часовых блоков (по умолчанию OUT/../h4)")
    s.add_argument("--workers", type=int, default=16)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--tradfi", type=Path, default=TRADFI)
    m = sub.add_parser("simulate")
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--workers", type=int, default=8)
    m.add_argument("--scope", choices=("all", "main"), default="all")
    r = sub.add_parser("report")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--oos", action="store_true", help="вне выборки: только главная клетка")
    a = ap.parse_args()
    if a.cmd == "select":
        cache = a.cache or a.out.parent / "cache"
        return stage_select(a.out, cache, a.a, a.b, a.workers, a.seed, a.tradfi)
    if a.cmd == "simulate":
        return stage_simulate(a.out, a.workers, a.scope)
    return stage_report(a.out, a.oos)


if __name__ == "__main__":
    raise SystemExit(main())
