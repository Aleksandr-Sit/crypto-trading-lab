#!/usr/bin/env python
"""Правило «купить обвал минуты, продать отскок»: проверка на сделках Binance Futures.

Источник правила — 279 сделок CryptosMX на копитрейдинге Binance
(`docs/research/cryptosmx-bybit-2026-10-03.md`): вход в минуту, когда альт-перп падает
на несколько процентов от закрытия предыдущей минуты, цель ≈+1.5%, через 7 минут выход
как есть. Минутных свечей для проверки мало — внутри минуты не видно, что было раньше,
дно или отскок, — поэтому сделка разыгрывается по ОТДЕЛЬНЫМ СДЕЛКАМ биржи (aggTrades).

Стадии (каждая дописывает свой файл и при повторе продолжает с места остановки):

    python scripts/crash_bounce_check.py events --out D --from 2025-01 --to 2026-09
    python scripts/crash_bounce_check.py simulate --out D
    python scripts/crash_bounce_check.py report --out D [--trader F.json]

* `events` — минутки ВСЕЙ вселенной USDT-перпов из data.binance.vision (с делистнутыми),
  событие: минимум минуты ниже закрытия предыдущей на `--drop` и больше. Месячные архивы
  качаются и выбрасываются, остаются события (`events.jsonl`).
* `simulate` — по дням с событиями качается дневной архив aggTrades и разыгрываются
  варианты входа (`VARIANTS`): лимитка на уровне обвала (исполнение — только если сделки
  прошли СТРОГО НИЖЕ уровня: очередь заявок не наша) или рыночная покупка через 0/1/5 с
  после пробоя. Выход — лимитка на цели (строго выше) или рынок через `HOLD_S`. Одна
  позиция на символ и вариант: событие во время позиции пропускается.
* `report` — средний итог сделки после комиссий, шум по ДНЯМ (обвалы рынка приходят
  пачкой — сотни монет в одну минуту, это одно свидетельство), по месяцам, по обороту.
  С `--trader` — сверка: нашёл ли детектор минуты входов CryptosMX.

Комиссии — VIP0 USDⓈ-M: мейкер 0.02%, тейкер 0.05%.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean, median

ARCHIVE = "https://data.binance.vision/data/futures/um"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
MAKER, TAKER = 0.0002, 0.0005
DROPS = (0.03, 0.05, 0.08, 0.12)
TP = 0.015
HOLD_S = 7 * 60
# (имя, порог обвала, способ входа, задержка с)
VARIANTS = [(f"limit{int(d * 100)}", d, "limit", 0) for d in DROPS] + [
    (f"mkt{int(d * 100)}_{lag}s", d, "market", lag) for d in DROPS for lag in (0, 1, 5)
]
UA = {"User-Agent": "Mozilla/5.0 crypto-trading-lab"}


def _get(url: str, tries: int = 4) -> bytes | None:
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


def _rows(blob: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and not rows[0][0].lstrip("-").isdigit():
        rows = rows[1:]  # у новых архивов есть заголовок
    return rows


def universe() -> list[str]:
    """Все USDT-перпы из каталога архива, включая делистнутые; без срочных (`_`)."""
    out, marker = [], ""
    while True:
        url = f"{LISTING}?delimiter=/&prefix=data/futures/um/monthly/klines/&marker={marker}"
        xml = _get(url).decode()
        names = re.findall(r"<Prefix>data/futures/um/monthly/klines/([^/<]+)/</Prefix>", xml)
        out += names
        if "<IsTruncated>true</IsTruncated>" not in xml:
            break
        marker = re.findall(r"<NextMarker>([^<]+)</NextMarker>", xml)[-1]
    return sorted({s for s in out if s.endswith("USDT") and "_" not in s})


def _months(a: str, b: str) -> list[str]:
    y, m = map(int, a.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _scan_month(sym: str, month: str, drop: float) -> tuple[str, str, list[dict] | None]:
    try:
        blob = _get(f"{ARCHIVE}/monthly/klines/{sym}/1m/{sym}-1m-{month}.zip")
    except RuntimeError as e:
        print(f"  ПРОПУСК {e}", flush=True)
        return sym, month, "fail"  # type: ignore[return-value]
    if blob is None:
        return sym, month, None
    rows = _rows(blob)
    ev = []
    qv = [float(r[7]) for r in rows]
    day_q = 0.0
    for i in range(1, len(rows)):
        day_q += qv[i - 1] - (qv[i - 1441] if i > 1440 else 0.0)  # оборот за ~сутки до минуты
        pc, lo = float(rows[i - 1][4]), float(rows[i][3])
        if pc > 0 and lo / pc - 1 <= -drop:
            ev.append({
                "sym": sym, "t": int(rows[i][0]), "prev_close": pc, "open": float(rows[i][1]),
                "low": lo, "close": float(rows[i][4]), "drop": lo / pc - 1,
                "qvol_min": qv[i], "qvol_24h": day_q if i >= 1440 else None,
            })
    return sym, month, ev


def stage_events(out: Path, a: str, b: str, drop: float, workers: int) -> int:
    out.mkdir(parents=True, exist_ok=True)
    done_p, ev_p = out / "events_done.txt", out / "events.jsonl"
    done = set(done_p.read_text().split()) if done_p.exists() else set()
    syms = universe()
    jobs = [(s, m) for m in _months(a, b) for s in syms if f"{s}:{m}" not in done]
    print(f"символов {len(syms)}, заданий {len(jobs)} (готово ранее {len(done)})", flush=True)
    n_ev = 0
    with ThreadPoolExecutor(workers) as pool, ev_p.open("a") as fe, done_p.open("a") as fd:
        futs = [pool.submit(_scan_month, s, m, drop) for s, m in jobs]
        for k, f in enumerate(as_completed(futs), 1):
            sym, month, ev = f.result()
            if ev == "fail":
                continue  # не помечать готовым: повтор возьмёт снова
            for e in ev or []:
                fe.write(json.dumps(e) + "\n")
            n_ev += len(ev or [])
            fe.flush()
            fd.write(f"{sym}:{month}\n")
            fd.flush()
            if k % 200 == 0:
                print(f"  {k}/{len(jobs)}  событий {n_ev}", flush=True)
    print(f"готово: событий {n_ev}", flush=True)
    return 0


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _trades(sym: str, day: str) -> tuple[list[int], list[float], list[float]]:
    blob = _get(f"{ARCHIVE}/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip")
    if blob is None:
        return [], [], []
    rows = _rows(blob)
    # agg_id, price, qty, first_id, last_id, time, is_buyer_maker
    return [int(r[5]) for r in rows], [float(r[1]) for r in rows], [float(r[2]) for r in rows]


def _first(ts: list[int], t0: int, lo: int = 0) -> int:
    """Индекс первой сделки с временем >= t0 (двоичный поиск)."""
    hi = len(ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if ts[mid] < t0:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _play(ts, px, qty, ev: dict, drop: float, mode: str, lag: int) -> dict | None:
    """Одна сделка по ленте. None — вход не состоялся."""
    level = ev["prev_close"] * (1 - drop)
    m0, m1 = ev["t"], ev["t"] + 60_000
    i = _first(ts, m0)
    trig = None
    while i < len(ts) and ts[i] < m1:
        if (px[i] < level) if mode == "limit" else (px[i] <= level):
            trig = i
            break
        i += 1
    if trig is None:
        return None
    if mode == "limit":
        e_i, entry, fee_in = trig, level, MAKER
        # что можно было купить у этого уровня: оборот строго ниже уровня за минуту обвала
        j, cap = trig, 0.0
        while j < len(ts) and ts[j] < m1:
            if px[j] < level:
                cap += px[j] * qty[j]
            j += 1
    else:
        e_i = _first(ts, ts[trig] + lag * 1000, trig + (1 if lag == 0 else 0))
        if e_i >= len(ts):
            return None
        entry, fee_in, cap = px[e_i], TAKER, None
    t_in = ts[e_i]
    tp = entry * (1 + TP)
    k = e_i + 1
    end = t_in + HOLD_S * 1000
    while k < len(ts) and ts[k] < end:
        if px[k] > tp:
            return {"entry": entry, "exit": tp, "t_in": t_in, "t_out": ts[k], "how": "tp",
                    "net": TP - fee_in - MAKER, "cap": cap}
        k += 1
    if k >= len(ts):
        return None  # лента дня кончилась раньше выхода — сделку не засчитывать
    x = px[k]
    return {"entry": entry, "exit": x, "t_in": t_in, "t_out": ts[k], "how": "time",
            "net": x / entry - 1 - fee_in - TAKER, "cap": cap}


def _sim_day(sym: str, day: str, evs: list[dict]) -> list[dict]:
    ts, px, qty = _trades(sym, day)
    if not ts:
        return [{"sym": sym, "day": day, "missing": True}]
    if max(e["t"] for e in evs) + 60_000 + HOLD_S * 1000 + 60_000 > ts[-1]:
        nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        t2, p2, q2 = _trades(sym, nxt)
        ts, px, qty = ts + t2, px + p2, qty + q2
    out = []
    for name, drop, mode, lag in VARIANTS:
        busy_until = 0
        for ev in sorted(evs, key=lambda e: e["t"]):
            if ev["drop"] > -drop or ev["t"] < busy_until:
                continue
            r = _play(ts, px, qty, ev, drop, mode, lag)
            if r is None:
                continue
            busy_until = r["t_out"]
            out.append({"sym": sym, "day": day, "var": name, "t": ev["t"], "drop_min": ev["drop"],
                        "qvol_24h": ev["qvol_24h"], **r})
    return out


def stage_simulate(out: Path, workers: int, min_drop: float, per_month: int, seed: int) -> int:
    evs = [json.loads(x) for x in (out / "events.jsonl").read_text().splitlines()]
    evs = [e for e in evs if e["drop"] <= -min_drop]
    by = defaultdict(list)
    for e in evs:
        by[(e["sym"], _day(e["t"]))].append(e)
    if per_month:
        # случайная выборка дней-символов, поровну с каждого месяца: лента сделок за все дни —
        # десятки гигабайт. Выбор не зависит от исхода, поэтому оценка среднего не смещена.
        months = defaultdict(list)
        for k in sorted(by):
            months[k[1][:7]].append(k)
        # своё зерно на месяц: добавление месяцев не меняет выборку уже посчитанных
        keep = {k for m, ks in months.items()
                for k in random.Random(f"{seed}:{m}").sample(ks, min(per_month, len(ks)))}
        by = {k: v for k, v in by.items() if k in keep}
    done_p, sim_p = out / "sim_done.txt", out / "sim.jsonl"
    done = set(done_p.read_text().split()) if done_p.exists() else set()
    jobs = [k for k in sorted(by) if f"{k[0]}:{k[1]}" not in done]
    print(f"событий {len(evs)}, дней-символов {len(by)}, осталось {len(jobs)}", flush=True)
    with ThreadPoolExecutor(workers) as pool, sim_p.open("a") as fs, done_p.open("a") as fd:
        futs = {pool.submit(_sim_day, s, d, by[(s, d)]): (s, d) for s, d in jobs}
        for k, f in enumerate(as_completed(futs), 1):
            s, d = futs[f]
            try:
                res = f.result()
            except RuntimeError as e:
                print(f"  ПРОПУСК {e}", flush=True)
                continue  # не помечать готовым: повтор возьмёт снова
            for r in res:
                fs.write(json.dumps(r) + "\n")
            fs.flush()
            fd.write(f"{s}:{d}\n")
            fd.flush()
            if k % 100 == 0:
                print(f"  {k}/{len(jobs)}", flush=True)
    print("готово", flush=True)
    return 0


def _stats(rows: list[dict]) -> tuple[int, float, float, float, float]:
    """n, среднее net, шум по дням (кластерная ошибка суммы по дням), доля плюса, доля цели."""
    n = len(rows)
    if n == 0:
        return 0, float("nan"), float("nan"), float("nan"), float("nan")
    mean = fmean(r["net"] for r in rows)
    days = defaultdict(lambda: [0.0, 0])
    for r in rows:
        days[r["day"]][0] += r["net"] - mean
        days[r["day"]][1] += 1
    g = len(days)
    if g < 2:
        return n, mean, float("nan"), sum(r["net"] > 0 for r in rows) / n, sum(r["how"] == "tp" for r in rows) / n
    se =math.sqrt(sum(v[0] ** 2 for v in days.values()) * g / max(g - 1, 1)) / n
    return n, mean, se, sum(r["net"] > 0 for r in rows) / n, sum(r["how"] == "tp" for r in rows) / n


def _line(name: str, rows: list[dict]) -> str:
    n, m, se, w, tp = _stats(rows)
    days = len({r["day"] for r in rows})
    return (f"{name:<14} {n:>6} {days:>5} {m * 100:>+7.3f} {se * 100:>6.3f} "
            f"{(m / se if se else 0):>+6.1f} {w:>5.0%} {tp:>5.0%}")


HEAD = f"{'':<14} {'n':>6} {'дней':>5} {'ср.%':>7} {'шум%':>6} {'t':>6} {'плюс':>5} {'цель':>5}"


def stage_report(out: Path, trader: Path | None) -> int:
    sims = [json.loads(x) for x in (out / "sim.jsonl").read_text().splitlines()]
    miss = [s for s in sims if s.get("missing")]
    sims = [s for s in sims if not s.get("missing")]
    print(f"сделок {len(sims)}; дней-символов без ленты aggTrades: {len(miss)}\n")
    print("## все варианты (итог сделки после комиссий; шум — по дням)")
    print(HEAD)
    by = defaultdict(list)
    for s in sims:
        by[s["var"]].append(s)
    for name, *_ in VARIANTS:
        print(_line(name, by[name]))

    cnt = defaultdict(int)
    for s in sims:
        cnt[s["day"]] += 1
    big = [d for d, c in sorted(cnt.items(), key=lambda kv: -kv[1])[:5]]
    print(f"\n## без пяти самых «пачечных» дней ({', '.join(big)})")
    print(HEAD)
    for name, *_ in VARIANTS:
        print(_line(name, [s for s in by[name] if s["day"] not in big]))

    for name in ("limit5", "mkt5_1s", "limit8", "mkt8_1s"):
        rows = by[name]
        print(f"\n## {name}: по месяцам")
        print(HEAD)
        bm = defaultdict(list)
        for s in rows:
            bm[s["day"][:7]].append(s)
        for k in sorted(bm):
            print(_line(k, bm[k]))
        print(f"## {name}: по обороту монеты за сутки до обвала (USDT)")
        print(HEAD)
        for lo, hi, lab in ((0, 5e6, "<5M"), (5e6, 50e6, "5-50M"), (50e6, 1e18, ">50M")):
            print(_line(lab, [s for s in rows if s["qvol_24h"] is not None and lo <= s["qvol_24h"] < hi]))
        caps = [s["cap"] for s in rows if s.get("cap") is not None]
        if caps:
            print(f"оборот строго ниже уровня в минуту обвала: медиана {median(caps):,.0f} USDT")

    if trader:
        pos = json.loads(trader.read_text(encoding="utf-8"))["positions"]
        evs = {(e["sym"], e["t"]) for e in map(json.loads, (out / "events.jsonl").read_text().splitlines())}
        scanned = {ln.split(":")[1] for ln in (out / "events_done.txt").read_text().split()}
        pos = [p for p in pos if _day(p["opened"])[:7] in scanned]
        hit = [p for p in pos if (p["symbol"], p["opened"] // 60000 * 60000) in evs]
        print(f"\n## сверка с CryptosMX: минута входа найдена детектором у {len(hit)} из {len(pos)} "
              "сделок в просканированных месяцах")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("events")
    e.add_argument("--out", type=Path, required=True)
    e.add_argument("--from", dest="a", required=True)
    e.add_argument("--to", dest="b", required=True)
    e.add_argument("--drop", type=float, default=0.03)
    e.add_argument("--workers", type=int, default=8)
    s = sub.add_parser("simulate")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--min-drop", type=float, default=0.03)
    s.add_argument("--per-month", type=int, default=0, help="выборка дней-символов на месяц (0 — все)")
    s.add_argument("--seed", type=int, default=7)
    r = sub.add_parser("report")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--trader", type=Path)
    a = ap.parse_args()
    if a.cmd == "events":
        return stage_events(a.out, a.a, a.b, a.drop, a.workers)
    if a.cmd == "simulate":
        return stage_simulate(a.out, a.workers, a.min_drop, a.per_month, a.seed)
    return stage_report(a.out, a.trader)


if __name__ == "__main__":
    raise SystemExit(main())
