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
  прошли СТРОГО НИЖЕ уровня: очередь заявок не наша) или рыночная покупка через `LAGS`
  (0–5 с) после пробоя. Выход — лимитка на цели (строго выше) или рынок через `HOLD_S`. Одна
  позиция на символ и вариант: событие во время позиции пропускается.
* `report` — средний итог сделки после комиссий, шум по ДНЯМ (обвалы рынка приходят
  пачкой — сотни монет в одну минуту, это одно свидетельство), по месяцам, по обороту.
  С `--trader` — сверка: нашёл ли детектор минуты входов CryptosMX.
* `fidelity --trader F` — сверка механики ленты с фактом автора: его вход против ask, его
  быстрый выход против правила «строго выше», его выход по таймеру против bid через 7 мин.

Для портфеля (`crash_bounce_portfolio.py`) — полный прогон: `simulate --min-drop 0.05 --side
--vars limit5,mkt5_0.25s --all-events --broad-first --stress 2025-10-10T20:56`
(каждое событие независимо, широкие даты первыми, стресс «API лёг» на 60 мин).

Замер вперёд (`crash_bounce_forward.py`, `docs/research/crash-bounce-paper-2026-10-09.md`):
`events --daily --from ГГГГ-ММ-ДД --to ГГГГ-ММ-ДД` — дневные архивы минуток; `simulate
--days A:B --strict-since D` — только дни окна, невыложенная лента — повтор, а не пропуск.

Комиссии — VIP0 USDⓈ-M: мейкер 0.02%, тейкер 0.05%.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from array import array
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import fmean, median

ARCHIVE = "https://data.binance.vision/data/futures/um"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
MAKER, TAKER = 0.0002, 0.0005
DROPS = (0.03, 0.05, 0.08, 0.12)
TP = 0.015
HOLD_S = 7 * 60
# Задержка рыночного входа после пробоя: 0 — следующая сделка ленты (быстрее любого бота),
# 0.1–0.5 с — бот на websocket с сервера рядом с биржей, 1 и 5 с — медленнее.
LAGS = (0, 0.1, 0.25, 0.5, 1, 5)
# (имя, порог обвала, способ входа, задержка с)
VARIANTS = [(f"limit{int(d * 100)}", d, "limit", 0) for d in DROPS] + [
    (f"mkt{int(d * 100)}_{lag}s", d, "market", lag) for d in DROPS for lag in LAGS
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


def _get_file(url: str, tries: int = 4) -> Path | None:
    """То же, что `_get`, но архив пишется во временный файл: архив ленты дня каскада у
    ETH/BTC — сотни мегабайт, и держать его в памяти каждого процесса нельзя."""
    url = urllib.parse.quote(url, safe=":/?=&%")
    for k in range(tries):
        fh = tempfile.NamedTemporaryFile(prefix="cb-", suffix=".zip", delete=False)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                shutil.copyfileobj(r, fh, 1 << 20)
            fh.close()
            return Path(fh.name)
        except urllib.error.HTTPError as e:
            fh.close()
            Path(fh.name).unlink(missing_ok=True)
            if e.code == 404:
                return None
            time.sleep(2 ** k)
        except Exception:  # noqa: BLE001 — сеть: повтор
            fh.close()
            Path(fh.name).unlink(missing_ok=True)
            time.sleep(2 ** k)
    raise RuntimeError(f"не скачалось: {url}")


def _rows(blob: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and not rows[0][0].lstrip("-").isdigit():
        rows = rows[1:]  # у новых архивов есть заголовок
    return rows


def universe(kind: str = "monthly") -> list[str]:
    """Все USDT-перпы из каталога архива, включая делистнутые; без срочных (`_`).
    `kind="daily"` — каталог дневных архивов: в месячном нет монет, листингованных в этом месяце."""
    out, marker = [], ""
    while True:
        url = f"{LISTING}?delimiter=/&prefix=data/futures/um/{kind}/klines/&marker={marker}"
        xml = _get(url).decode()
        names = re.findall(rf"<Prefix>data/futures/um/{kind}/klines/([^/<]+)/</Prefix>", xml)
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


def _days(a: str, b: str) -> list[str]:
    d, end, out = date.fromisoformat(a), date.fromisoformat(b), []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _scan_month(sym: str, month: str, drop: float) -> tuple[str, str, list[dict] | None]:
    try:
        blob = _get(f"{ARCHIVE}/monthly/klines/{sym}/1m/{sym}-1m-{month}.zip")
    except RuntimeError as e:
        print(f"  ПРОПУСК {e}", flush=True)
        return sym, month, "fail"  # type: ignore[return-value]
    if blob is None:
        return sym, month, None
    return sym, month, _events(sym, _rows(blob), drop)


def _scan_day(sym: str, day: str, drop: float) -> tuple[str, str, list[dict] | None]:
    """Дневной архив минуток (замер вперёд: месячного за текущий месяц ещё нет). Первая минута
    суток сравнивается с закрытием последней минуты ПРОШЛЫХ суток — без этого минута 00:00 UTC
    выпадала бы каждый день, а не раз в месяц, как у месячного архива."""
    prev_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    try:
        blob = _get(f"{ARCHIVE}/daily/klines/{sym}/1m/{sym}-1m-{day}.zip")
        prev = _get(f"{ARCHIVE}/daily/klines/{sym}/1m/{sym}-1m-{prev_day}.zip") if blob else None
    except RuntimeError as e:
        print(f"  ПРОПУСК {e}", flush=True)
        return sym, day, "fail"  # type: ignore[return-value]
    if blob is None:
        return sym, day, None
    rows = _rows(blob)
    if prev is not None:
        rows = _rows(prev)[-1:] + rows  # день листинга: прошлых суток нет, как у месячного
    return sym, day, _events(sym, rows, drop)


def _events(sym: str, rows: list[list[str]], drop: float) -> list[dict]:
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
    return ev


def stage_events(out: Path, a: str, b: str, drop: float, workers: int,
                 daily: bool = False) -> int:
    """`a`, `b` — месяцы ГГГГ-ММ, с `daily` — дни ГГГГ-ММ-ДД (дневные архивы минуток)."""
    out.mkdir(parents=True, exist_ok=True)
    done_p, ev_p = out / "events_done.txt", out / "events.jsonl"
    done = set(done_p.read_text().split()) if done_p.exists() else set()
    syms = universe("daily" if daily else "monthly")
    periods, scan = (_days(a, b), _scan_day) if daily else (_months(a, b), _scan_month)
    jobs = [(s, m) for m in periods for s in syms if f"{s}:{m}" not in done]
    print(f"символов {len(syms)}, заданий {len(jobs)} (готово ранее {len(done)})", flush=True)
    n_ev = n_fail = 0
    with ThreadPoolExecutor(workers) as pool, ev_p.open("a") as fe, done_p.open("a") as fd:
        futs = [pool.submit(scan, s, m, drop) for s, m in jobs]
        for k, f in enumerate(as_completed(futs), 1):
            sym, month, ev = f.result()
            if ev == "fail":
                n_fail += 1
                continue  # не помечать готовым: повтор возьмёт снова
            for e in ev or []:
                fe.write(json.dumps(e) + "\n")
            n_ev += len(ev or [])
            fe.flush()
            fd.write(f"{sym}:{month}\n")
            fd.flush()
            if k % 200 == 0:
                print(f"  {k}/{len(jobs)}  событий {n_ev}", flush=True)
    print(f"готово: событий {n_ev}" + (f", не скачалось {n_fail} — повторить тем же запуском"
                                        if n_fail else ""), flush=True)
    # код 1 при недокачанном: замер вперёд не должен разбирать сутки с дырой в событиях
    return 1 if n_fail else 0


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _trades(sym: str, day: str) -> tuple[array, array, array, array]:
    """Время (мс), цена, объём сделок дня и сторона агрессора (1 — продавец, сделка прошла
    по bid; 0 — покупатель, по ask). Читается потоком в массивы чисел: таблица строк дня
    обвала — сотни мегабайт на процесс, а процессов десяток."""
    ts, px, qty, sell = array("q"), array("d"), array("d"), array("b")
    path = _get_file(f"{ARCHIVE}/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip")
    if path is None:
        return ts, px, qty, sell
    try:
        with zipfile.ZipFile(path) as z, z.open(z.namelist()[0]) as fh:
            for line in fh:
                # agg_id, price, qty, first_id, last_id, time, is_buyer_maker
                f = line.split(b",")
                if not f[0].isdigit():
                    continue  # заголовок у новых архивов
                ts.append(int(f[5]))
                px.append(float(f[1]))
                qty.append(float(f[2]))
                sell.append(f[6].strip().lower() == b"true")
    finally:
        path.unlink(missing_ok=True)
    return ts, px, qty, sell


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


def _stuck_exit(ts, px, sell, e_i: int, entry: float, until: int) -> tuple | None:
    """Выход, когда бот не может продать по таймеру до `until` (мс; API биржи лёг): стоящая
    на бирже цель работает, дальше — первая продажа по bid после `until`.
    → (t_out, цена выхода, как, минимум цены за удержание) или None, если лента кончилась."""
    tp, low, k = entry * (1 + TP), min(entry, px[e_i]), e_i + 1
    while k < len(ts) and ts[k] < until:
        if px[k] > tp:
            return ts[k], tp, "tp", low
        low = min(low, px[k])
        k += 1
    while k < len(ts) and not sell[k]:
        k += 1
    if k >= len(ts):
        return None
    return ts[k], px[k], "time", min(low, px[k])


def _play(ts, px, qty, ev: dict, drop: float, mode: str, lag: float,
          sell=None, stress: tuple[int, int] | None = None) -> dict | None:
    """Одна сделка по ленте. None — вход не состоялся.

    Без `sell` рыночные цены — первая сделка ленты после момента, чья бы она ни была: покупка
    получает и сделки по bid, то есть полспреда в подарок. С `sell` рыночная покупка — первая
    сделка покупателя-агрессора (по ask), продажа по таймеру — первая сделка продавца (по bid).
    `low` — минимум цены за удержание (худший момент внутри сделки). `stress` = (T0, T1), мс:
    окно, когда API биржи недоступен; сделке, задевшей окно, пишется и выход «застрявшего»
    бота (поля `api_*`, только со стороной сделки).
    """
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
        e_i = _first(ts, ts[trig] + round(lag * 1000), trig + (1 if lag == 0 else 0))
        while sell is not None and e_i < len(ts) and sell[e_i]:
            e_i += 1
        if e_i >= len(ts):
            return None
        entry, fee_in, cap = px[e_i], TAKER, None
    t_in = ts[e_i]
    tp = entry * (1 + TP)
    k = e_i + 1
    end = t_in + HOLD_S * 1000
    low = min(entry, px[e_i])
    r = None
    while k < len(ts) and ts[k] < end:
        if px[k] > tp:
            r = {"entry": entry, "exit": tp, "t_in": t_in, "t_out": ts[k], "how": "tp",
                 "net": TP - fee_in - MAKER, "cap": cap, "low": low}
            break
        low = min(low, px[k])
        k += 1
    if r is None:
        while sell is not None and k < len(ts) and not sell[k]:
            k += 1
        if k >= len(ts):
            return None  # лента дня кончилась раньше выхода — сделку не засчитывать
        x = px[k]
        r = {"entry": entry, "exit": x, "t_in": t_in, "t_out": ts[k], "how": "time",
             "net": x / entry - 1 - fee_in - TAKER, "cap": cap, "low": min(low, x)}
    if stress and sell is not None and t_in < stress[1] and r["t_out"] > stress[0]:
        s = _stuck_exit(ts, px, sell, e_i, entry, max(stress[1], end))
        if s is not None:
            r.update(api_out=s[0], api_exit=s[1], api_how=s[2], api_low=s[3],
                     api_net=s[1] / entry - 1 - fee_in - (MAKER if s[2] == "tp" else TAKER))
    return r


def _stress_limits(sym: str, day: str, ts, px, sell, stress: tuple[int, int],
                   drop: float = 0.05) -> list[dict]:
    """Стресс «API лёг» для лимиток: заявка, стоявшая в T0 (уровень −drop от последней
    сделки до T0 ≈ закрытие прошлой минуты), не снимается и исполняется при первой сделке
    строго ниже уровня до T1; выход — `_stuck_exit` (цель стоит, таймер ждёт T1)."""
    t0, t1 = stress
    i = _first(ts, t0)
    if i == 0 or i >= len(ts):
        return []
    level = px[i - 1] * (1 - drop)
    while i < len(ts) and ts[i] < t1:
        if px[i] < level:
            s = _stuck_exit(ts, px, sell, i, level, max(t1, ts[i] + HOLD_S * 1000))
            if s is None:
                return []
            return [{"sym": sym, "day": day, "var": f"limit{round(drop * 100)}_api{SIDE}",
                     "t": t0, "entry": level, "exit": s[1], "t_in": ts[i], "t_out": s[0],
                     "how": s[2], "low": s[3],
                     "net": s[1] / level - 1 - MAKER - (MAKER if s[2] == "tp" else TAKER)}]
        i += 1
    return []


SIDE = "|side"  # суффикс варианта, разыгранного со стороной сделки (`simulate --side`)


def _sim_day(sym: str, day: str, evs: list[dict], side: bool = False,
             names: tuple[str, ...] | None = None, all_events: bool = False,
             stress: tuple[int, int] | None = None, strict_since: str | None = None) -> list[dict]:
    """`names` — только эти варианты; `all_events` — каждое событие разыгрывается независимо
    (иначе событие во время позиции на ту же монету пропускается; для портфеля это плохо:
    он, пропустив сделку за неимением слота, должен видеть следующий обвал монеты).

    `strict_since` — замер вперёд: для дней не раньше этой даты НЕВЫЛОЖЕННАЯ лента (своего дня
    или следующего, нужного сделке на стыке суток) — ошибка, а не «ленты нет»: день не
    помечается готовым и берётся следующим запуском. Иначе свежий день, разобранный до того,
    как архив выложен целиком, навсегда терял бы сделки — молча."""
    strict = strict_since is not None and day >= strict_since
    ts, px, qty, sell = _trades(sym, day)
    if not ts:
        if strict:
            raise RuntimeError(f"лента {sym} {day} ещё не выложена")
        return [{"sym": sym, "day": day, "missing": True}]
    need = max(e["t"] for e in evs) + 60_000 + HOLD_S * 1000 + 60_000
    if stress and _day(stress[0]) == day:
        need = max(need, stress[1] + HOLD_S * 1000 + 60_000)
    if need > ts[-1]:
        nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        t2, p2, q2, s2 = _trades(sym, nxt)
        # Ждать ленту следующих суток — только если сделка ЗАХОДИТ за полночь. Последняя сделка
        # дня раньше нужного времени бывает и без полуночи — монету сняли с торгов (PUMPBTCUSDT
        # 05.10.2026: обвал 09:15, поставка 09:20, лент дальше не будет никогда); строгий режим
        # ждал бы её вечно, и замер вперёд не выдал бы ни одних суток.
        day_end = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
                      * 1000) + 86_400_000
        if not t2 and strict and need > day_end:
            raise RuntimeError(f"лента {sym} {nxt} (стык суток) ещё не выложена")
        ts, px, qty, sell = ts + t2, px + p2, qty + q2, sell + s2
    out = []
    variants = [v for v in VARIANTS if names is None or v[0] in names]
    for suffix, sd in (("", None), (SIDE, sell)) if side else (("", None),):
        for name, drop, mode, lag in variants:
            busy_until = 0
            for ev in sorted(evs, key=lambda e: e["t"]):
                if ev["drop"] > -drop or ev["t"] < busy_until:
                    continue
                r = _play(ts, px, qty, ev, drop, mode, lag, sd, stress)
                if r is None:
                    continue
                if not all_events:
                    busy_until = r["t_out"]
                out.append({"sym": sym, "day": day, "var": name + suffix, "t": ev["t"],
                            "drop_min": ev["drop"], "qvol_24h": ev["qvol_24h"], **r})
    if side and stress and _day(stress[0]) == day:
        for _name, drop, mode, _lag in variants:
            if mode == "limit":
                out += _stress_limits(sym, day, ts, px, sell, stress, drop)
    return out


def _broad_minutes(evs: list[dict], k: int = 30) -> dict[str, int]:
    """Дата → число минут, в которые обвалилось ≥k монет (по всем событиям файла)."""
    per_min = defaultdict(set)
    for e in evs:
        per_min[e["t"]].add(e["sym"])
    out = defaultdict(int)
    for t, syms in per_min.items():
        if len(syms) >= k:
            out[_day(t)] += 1
    return out


def stage_simulate(out: Path, workers: int, min_drop: float, per_month: int, seed: int,
                   side: bool = False, names: tuple[str, ...] | None = None,
                   all_events: bool = False, broad_first: bool = False,
                   stress: tuple[int, int] | None = None, passes: int = 1,
                   days: tuple[str, str] | None = None, strict_since: str | None = None) -> int:
    evs = [json.loads(x) for x in (out / "events.jsonl").read_text().splitlines()]
    broad = _broad_minutes(evs) if broad_first else {}
    evs = [e for e in evs if e["drop"] <= -min_drop]
    if days:
        # замер вперёд: события раньше окна лежат в файле ради счёта обвалов за 30 суток
        # (выбор 100 монет), разыгрывать их не нужно
        evs = [e for e in evs if days[0] <= _day(e["t"]) <= days[1]]
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
    # Несколько проходов: ночью связь с архивом пропадала часами, и проход выдавал тысячи
    # «не скачалось». Следующий проход берёт только непомеченные дни; ноль готовых за проход —
    # сеть лежит, дальше не крутить вхолостую.
    for p in range(1, passes + 1):
        done = set(done_p.read_text().split()) if done_p.exists() else set()
        jobs = [k for k in sorted(by) if f"{k[0]}:{k[1]}" not in done]
        if broad_first:
            # сначала даты с самым широким обвалом (10.10.2025 — 78 минут): пул берёт задания
            # в порядке подачи, и ответ по каскаду готов задолго до конца прогона
            jobs.sort(key=lambda k: (-broad.get(k[1], 0), k[1], k[0]))
        print(f"проход {p}/{passes}: событий {len(evs)}, дней-символов {len(by)}, "
              f"осталось {len(jobs)}", flush=True)
        if not jobs:
            break
        failed = _run_jobs(jobs, by, workers, sim_p, done_p, side, names, all_events, stress,
                           strict_since)
        if not failed or failed == len(jobs) or p == passes:
            if failed:
                print(f"не скачалось {failed} дней-символов — повторить тем же запуском",
                      flush=True)
            break
        print(f"не скачалось {failed}; следующий проход через 5 мин", flush=True)
        time.sleep(300)
    print("готово", flush=True)
    return 0


def _run_jobs(jobs, by, workers, sim_p, done_p, side, names, all_events, stress,
              strict_since=None) -> int:
    """Один проход по дням-символам; → сколько не удалось (не помечены готовыми)."""
    failed = 0
    # процессы, а не потоки: разбор ленты сделок держит GIL, и десять потоков шли на одном ядре
    with ProcessPoolExecutor(workers) as pool, sim_p.open("a") as fs, done_p.open("a") as fd:
        futs = {pool.submit(_sim_day, s, d, by[(s, d)], side, names, all_events, stress,
                            strict_since): (s, d)
                for s, d in jobs}
        for k, f in enumerate(as_completed(futs), 1):
            s, d = futs[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001 — сеть или память: день не теряется
                print(f"  ПРОПУСК {s} {d}: {type(e).__name__} {e}", flush=True)
                failed += 1
                continue  # не помечать готовым: повтор возьмёт снова
            for r in res:
                fs.write(json.dumps(r) + "\n")
            fs.flush()
            fd.write(f"{s}:{d}\n")
            fd.flush()
            if k % 100 == 0:
                print(f"  {k}/{len(jobs)}", flush=True)
    return failed


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
    win = sum(r["net"] > 0 for r in rows) / n
    tp = sum(r["how"] == "tp" for r in rows) / n
    if g < 2:
        return n, mean, float("nan"), win, tp
    se = math.sqrt(sum(v[0] ** 2 for v in days.values()) * g / max(g - 1, 1)) / n
    return n, mean, se, win, tp


def _line(name: str, rows: list[dict]) -> str:
    n, m, se, w, tp = _stats(rows)
    days = len({r["day"] for r in rows})
    return (f"{name:<14} {n:>6} {days:>5} {m * 100:>+7.3f} {se * 100:>6.3f} "
            f"{(m / se if se else 0):>+6.1f} {w:>5.0%} {tp:>5.0%}")


HEAD = f"{'':<14} {'n':>6} {'дней':>5} {'ср.%':>7} {'шум%':>6} {'t':>6} {'плюс':>5} {'цель':>5}"
# столбцы таблицы «месяц × вариант»: лимитки и кривая задержки при обвале ≥5%
MONTH_VARS = ("limit3", "limit5", "limit8") + tuple(f"mkt5_{lag}s" for lag in LAGS)


def _month_weights(out: Path) -> dict[str, float]:
    """Вес дня-символа месяца: сколько их среди событий / сколько разыграно.

    Выборка берёт поровну с каждого месяца, а обвалов в месяцах разное число (10.2025 —
    вшестеро больше, чем 01.2025). Бот торговал бы каждое событие, поэтому итог «как у бота» —
    среднее с этими весами; невзвешенное отвечает на вопрос «типичный месяц».
    """
    pop = defaultdict(set)
    for line in (out / "events.jsonl").read_text().splitlines():
        e = json.loads(line)
        d = _day(e["t"])
        pop[d[:7]].add((e["sym"], d))
    smp = defaultdict(int)
    for key in (out / "sim_done.txt").read_text().split():
        smp[key.rsplit(":", 1)[1][:7]] += 1
    return {m: len(pop[m]) / k for m, k in smp.items() if pop.get(m)}


def _wline(name: str, rows: list[dict], w: dict[str, float]) -> str:
    """Взвешенное среднее и кластерный шум по дням с теми же весами."""
    rows = [r for r in rows if r["day"][:7] in w]
    if not rows:
        return f"{name:<14} {0:>6}"
    tw = sum(w[r["day"][:7]] for r in rows)
    mean = sum(w[r["day"][:7]] * r["net"] for r in rows) / tw
    days = defaultdict(float)
    for r in rows:
        days[r["day"]] += w[r["day"][:7]] * (r["net"] - mean)
    g = len(days)
    se = math.sqrt(sum(v * v for v in days.values()) * g / max(g - 1, 1)) / tw
    return (f"{name:<14} {len(rows):>6} {g:>5} {mean * 100:>+7.3f} {se * 100:>6.3f} "
            f"{(mean / se if se else 0):>+6.1f}")


def _matrix(by: dict[str, list[dict]], names: tuple[str, ...]) -> None:
    """Среднее за месяц по каждому варианту и итог: в скольких месяцах плюс и t > 2."""
    months = sorted({s["day"][:7] for n in names for s in by[n]})
    print(f"{'':<8}" + "".join(f"{n:>14}" for n in names))
    pos, sig = defaultdict(int), defaultdict(int)
    for m in months:
        cells = []
        for n in names:
            k, mean, se, *_ = _stats([s for s in by[n] if s["day"].startswith(m)])
            if not k:
                cells.append(f"{'—':>14}")
                continue
            pos[n] += mean > 0
            sig[n] += se > 0 and mean / se > 2
            cells.append(f"{mean * 100:>+8.2f}({k:>4})")
        print(f"{m:<8}" + "".join(cells))
    for lab, cnt in (("плюс", pos), ("t>2", sig)):
        print(f"{lab:<8}" + "".join(f"{f'{cnt[n]}/{len(months)}':>14}" for n in names))


def _breadth_block(out: Path, by: dict[str, list[dict]]) -> None:
    """Ширина обвала: сколько монет обвалилось в ПРЕДЫДУЩУЮ минуту — бот знает это при входе.

    Вторая таблица — проверка фильтра по ширине на каждом «широком» дне: если он выигрывает
    только на одном каскаде (10.10.2025), а в остальные широкие дни срезает прибыльные входы,
    то он подогнан под одно событие.
    """
    per_min = defaultdict(set)
    for line in (out / "events.jsonl").read_text().splitlines():
        e = json.loads(line)
        per_min[e["t"]].add(e["sym"])

    def prev(r: dict) -> int:
        return len(per_min.get(r["t"] - 60_000, ()))

    ks = (3, 10, 30, 0)
    print("\n## ширина обвала: вход пропускается, если в предыдущую минуту обвалилось ≥K монет "
          "(ср.%, в скобках t)")
    print(f"{'':<14}" + "".join(f"{f'K={k}' if k else 'без фильтра':>14}" for k in ks))
    for name in MONTH_VARS:
        cells = []
        for k in ks:
            _, m, se, *_ = _stats([r for r in by[name] if not k or prev(r) < k])
            cells.append(f"{m * 100:+.2f}({(m / se if se else 0):+.1f})")
        print(f"{name:<14}" + "".join(f"{c:>14}" for c in cells))
    broad = defaultdict(int)
    for t, syms in per_min.items():
        if len(syms) >= 30:
            broad[_day(t)] += 1
    print("## limit5 в дни, где есть минуты с обвалом ≥30 монет: сумма итогов сделок, % "
          "(сделок) — все входы | с K=10")
    for d in sorted(broad):
        a = [r for r in by["limit5"] if r["day"] == d]
        if a:
            b = [r for r in a if prev(r) < 10]
            print(f"{d}  широких минут {broad[d]:>3}: {sum(r['net'] for r in a) * 100:+8.1f} "
                  f"({len(a):>3}) | {sum(r['net'] for r in b) * 100:+8.1f} ({len(b):>3})")


def _side_block(by: dict[str, list[dict]]) -> None:
    """Попарно по одним и тем же входам: итог со стороной сделки минус итог без неё."""
    print("\n## сторона сделки: рыночная покупка по ask, продажа по таймеру по bid "
          "(попарно, одни и те же входы; шум разницы — по дням)")
    print(f"{'':<14} {'пар':>6} {'было%':>7} {'стало%':>7} {'разн.':>7} {'шум':>6}"
          f"   без 10.2025: {'было%':>7} {'стало%':>7} {'t':>5}")
    for name, *_ in VARIANTS:
        plain = {(s["sym"], s["t"]): s for s in by[name]}
        pairs = [(plain[k], s) for s in by[name + SIDE] if (k := (s["sym"], s["t"])) in plain]
        if not pairs:
            continue
        diff = [{"day": a["day"], "net": b["net"] - a["net"], "how": b["how"]} for a, b in pairs]
        _, d, se, *_ = _stats(diff)
        calm = [(a, b) for a, b in pairs if not a["day"].startswith("2025-10")]
        _, m_new, se_new, *_ = _stats([b for _, b in calm])
        print(f"{name:<14} {len(pairs):>6} {fmean(a['net'] for a, _ in pairs) * 100:>+7.3f} "
              f"{fmean(b['net'] for _, b in pairs) * 100:>+7.3f} {d * 100:>+7.3f} {se * 100:>6.3f}"
              f"   {fmean(a['net'] for a, _ in calm) * 100:>+20.3f} {m_new * 100:>+7.3f} "
              f"{(m_new / se_new if se_new else 0):>+5.1f}")


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

    w = _month_weights(out)
    print("\n## то же, взвешено числом дней-символов с обвалом в месяце (итог «как у бота»)")
    print(f"веса месяцев: {', '.join(f'{m[2:]} {v:.1f}' for m, v in sorted(w.items()))}")
    print(HEAD[:HEAD.index("плюс")].rstrip())
    for name, *_ in VARIANTS:
        print(_wline(name, by[name], w))

    cnt = defaultdict(int)
    for s in sims:
        cnt[s["day"]] += 1
    big = [d for d, c in sorted(cnt.items(), key=lambda kv: -kv[1])[:5]]
    print(f"\n## без пяти самых «пачечных» дней ({', '.join(big)})")
    print(HEAD)
    for name, *_ in VARIANTS:
        print(_line(name, [s for s in by[name] if s["day"] not in big]))

    print("\n## без октября 2025 (обвал рынка 10.10.2025)")
    print(HEAD)
    for name, *_ in VARIANTS:
        print(_line(name, [s for s in by[name] if not s["day"].startswith("2025-10")]))

    print("\n## по месяцам: среднее итога сделки, % (в скобках — сделок)")
    _matrix(by, MONTH_VARS)
    _breadth_block(out, by)

    if any(s["var"].endswith(SIDE) for s in sims):
        _side_block(by)

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
            print(_line(lab, [s for s in rows
                              if s["qvol_24h"] is not None and lo <= s["qvol_24h"] < hi]))
        caps = [s["cap"] for s in rows if s.get("cap") is not None]
        if caps:
            print(f"оборот строго ниже уровня в минуту обвала: медиана {median(caps):,.0f} USDT")

    if trader:
        pos = json.loads(trader.read_text(encoding="utf-8"))["positions"]
        lines = (out / "events.jsonl").read_text().splitlines()
        evs = {(e["sym"], e["t"]) for e in map(json.loads, lines)}
        scanned = {ln.split(":")[1] for ln in (out / "events_done.txt").read_text().split()}
        pos = [p for p in pos if _day(p["opened"])[:7] in scanned]
        hit = [p for p in pos if (p["symbol"], p["opened"] // 60000 * 60000) in evs]
        print(f"\n## сверка с CryptosMX: минута входа найдена детектором у {len(hit)} "
              f"из {len(pos)} сделок в просканированных месяцах")
    return 0


def _fidelity_day(sym: str, day: str, pos: list[dict]) -> list[dict]:
    """Сделки автора за день-символ против ленты: его вход — с первой покупкой по ask с его
    миллисекунды; его выход до 6.9 мин — исполнилась бы продажа по нашему правилу «сделка
    строго выше цены» к его выходу; выход 6.9–7.5 мин — с первой продажей по bid через 7 мин."""
    ts, px, _, sell = _trades(sym, day)
    if not ts:
        return [{"sym": sym, "day": day, "missing": True}]
    if max(p["closed"] for p in pos) + 60_000 > ts[-1]:
        nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        t2, p2, _, s2 = _trades(sym, nxt)
        ts, px, sell = ts + t2, px + p2, sell + s2
    out = []
    for p in pos:
        dur = (p["closed"] - p["opened"]) / 60_000
        r = {"sym": sym, "opened": p["opened"], "dur": dur,
             "gain": p["avgClosePrice"] / p["avgCost"] - 1}
        i = _first(ts, p["opened"])
        while i < len(ts) and sell[i]:
            i += 1
        if i < len(ts):
            r["entry_diff"] = p["avgCost"] / px[i] - 1
        if dur < 6.9:
            j = _first(ts, p["opened"])
            while j < len(ts) and px[j] <= p["avgClosePrice"]:
                j += 1
            r["kind"] = "fast"
            r["hit_lag_s"] = (ts[j] - p["closed"]) / 1000 if j < len(ts) else None
        else:
            j = _first(ts, p["opened"] + HOLD_S * 1000)
            while j < len(ts) and not sell[j]:
                j += 1
            r["kind"] = "timer"
            r["exit_diff"] = p["avgClosePrice"] / px[j] - 1 if j < len(ts) else None
        out.append(r)
    return out


def stage_fidelity(out: Path, trader: Path, workers: int) -> int:
    """Сверка механики ленты с фактом автора (сделки до 7.5 мин; длинные — ручные).
    Пороги объявлены заранее (`crash-bounce-capital-2026-10-04.md`): доля быстрых выходов,
    воспроизведённых правилом «строго выше» с допуском 2 с, ≥90% — цифрам депозита верим как
    абсолютным, 70–90% — только сравнение клеток, <70% — механику переделывать."""
    out.mkdir(parents=True, exist_ok=True)
    pos = json.loads(trader.read_text(encoding="utf-8"))["positions"]
    pos = [p for p in pos if (p["closed"] - p["opened"]) / 60_000 <= 7.5]
    by = defaultdict(list)
    for p in pos:
        by[(p["symbol"], _day(p["opened"]))].append(p)
    print(f"сделок до 7.5 мин: {len(pos)}, дней-символов {len(by)}", flush=True)
    rows = []
    with ProcessPoolExecutor(workers) as pool:
        futs = [pool.submit(_fidelity_day, s, d, v) for (s, d), v in sorted(by.items())]
        for f in as_completed(futs):
            rows += f.result()
    (out / "fidelity.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    miss = [r for r in rows if r.get("missing")]
    rows = [r for r in rows if not r.get("missing")]
    ed = [r["entry_diff"] for r in rows if "entry_diff" in r]
    print(f"дней-символов без ленты: {len(miss)}")
    print(f"вход: его цена / первая покупка по ask с его миллисекунды − 1: медиана "
          f"{median(ed) * 100:+.3f}%, медиана |разницы| {median(map(abs, ed)) * 100:.3f}% "
          f"({len(ed)} сделок)")
    fast = [r for r in rows if r["kind"] == "fast"]
    lag = [r["hit_lag_s"] for r in fast]
    ok = sum(x is not None and x <= 2 for x in lag)
    early = sum(x is not None and x < -2 for x in lag)
    print(f"выход до 6.9 мин ({len(fast)}): продажа по его цене исполнилась бы по правилу "
          f"«строго выше» к его выходу (допуск 2 с) — {ok} ({ok / len(fast):.0%}); из них "
          f"лента прошла выше его цены раньше выхода больше чем на 2 с — {early}")
    hit = sorted(x for x in lag if x is not None)
    if hit:
        q = [hit[int(len(hit) * f)] for f in (0.1, 0.5, 0.9)]
        print(f"  сдвиг «лента выше его цены» − «его выход», с: 10% {q[0]:+.1f}, медиана "
              f"{q[1]:+.1f}, 90% {q[2]:+.1f}")
    share = ok / len(fast)
    verdict = ("≥90%: цифрам депозита верим как абсолютным" if share >= 0.9 else
               "70–90%: только сравнение клеток между собой" if share >= 0.7 else
               "<70%: механику переделывать до портфеля")
    print(f"  ВЕРДИКТ сверки: {verdict}")
    tim = [r["exit_diff"] for r in rows if r["kind"] == "timer" and r.get("exit_diff") is not None]
    if tim:
        print(f"выход по таймеру ({len(tim)}): его цена / наша первая продажа по bid через 7 мин "
              f"− 1: медиана {median(tim) * 100:+.3f}%, медиана |разницы| "
              f"{median(map(abs, tim)) * 100:.3f}%")
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
    e.add_argument("--drop", type=float, default=0.03)
    e.add_argument("--workers", type=int, default=8)
    e.add_argument("--daily", action="store_true",
                   help="дневные архивы минуток: --from/--to — дни ГГГГ-ММ-ДД (замер вперёд)")
    s = sub.add_parser("simulate")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--min-drop", type=float, default=0.03)
    s.add_argument("--per-month", type=int, default=0,
                   help="выборка дней-символов на месяц (0 — все)")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--side", action="store_true",
                   help=f"разыграть ещё и со стороной сделки (варианты с суффиксом {SIDE})")
    s.add_argument("--vars", help="только эти варианты, через запятую (limit5,mkt5_0.25s)")
    s.add_argument("--all-events", action="store_true",
                   help="каждое событие независимо, без пропуска во время позиции (для портфеля)")
    s.add_argument("--broad-first", action="store_true",
                   help="сначала даты с минутами обвала ≥30 монет, самые широкие первыми")
    s.add_argument("--stress", help="начало простоя API, UTC: 2025-10-10T20:56")
    s.add_argument("--stress-min", type=int, default=60, help="длительность простоя, мин")
    s.add_argument("--passes", type=int, default=1,
                   help="проходов по нескачанным дням (пауза 5 мин; стоп, если проход пустой)")
    s.add_argument("--days", help="разыгрывать только эти дни: ГГГГ-ММ-ДД:ГГГГ-ММ-ДД")
    s.add_argument("--strict-since", help="с этого дня невыложенная лента — повтор, а не пропуск")
    r = sub.add_parser("report")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--trader", type=Path)
    fi = sub.add_parser("fidelity", help="сверка механики ленты со сделками автора")
    fi.add_argument("--out", type=Path, required=True)
    fi.add_argument("--trader", type=Path, required=True)
    fi.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    if a.cmd == "fidelity":
        return stage_fidelity(a.out, a.trader, a.workers)
    if a.cmd == "events":
        return stage_events(a.out, a.a, a.b, a.drop, a.workers, a.daily)
    if a.cmd == "simulate":
        names = tuple(a.vars.split(",")) if a.vars else None
        if names and (bad := set(names) - {v[0] for v in VARIANTS}):
            ap.error(f"нет таких вариантов: {', '.join(sorted(bad))}")
        stress = None
        if a.stress:
            t0 = int(datetime.strptime(a.stress, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
                     .timestamp() * 1000)
            stress = (t0, t0 + a.stress_min * 60_000)
        days = tuple(a.days.split(":")) if a.days else None
        return stage_simulate(a.out, a.workers, a.min_drop, a.per_month, a.seed, a.side,
                              names, a.all_events, a.broad_first, stress, a.passes, days,
                              a.strict_since)
    return stage_report(a.out, a.trader)


if __name__ == "__main__":
    raise SystemExit(main())
