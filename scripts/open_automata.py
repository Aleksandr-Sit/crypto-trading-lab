#!/usr/bin/env python
"""Автоматы в открытых историях сделок копитрейдинга Binance (п.9 плана, 24.10.2026).

Ищет среди лид-портфелей «подпись автомата» — механизм, который повторяется в ИСПОЛНЕННЫХ
сделках: выход по таймеру, одна и та же цель, вход в одну минуту часа. Такой механизм
восстанавливается по сделкам × минутным свечам и проверяется на годах истории без участия
трейдера (так был найден автомат CryptosMX). Результат трейдера в отбор не входит: полей
доходности скрипт не пишет вовсе. Разбор — `docs/research/open-automata-2026-10-24.md`.

    python scripts/open_automata.py list   [--window 30D]    # весь список лидеров → bn_list.jsonl
    python scripts/open_automata.py tradfi                   # символы перпов акций/товаров/валют
    python scripts/open_automata.py probe  PID [PID ...]     # подпись одного портфеля
    python scripts/open_automata.py scan   [--limit N]       # весь пул с меткой API → bn_scan.jsonl
    python scripts/open_automata.py scan   --summary [FILE]  # итог по файлу, без сети
    python scripts/open_automata.py review [--md]            # классы и шорт-лист → bn_shortlist.csv

Данные лежат вне репозитория: `--data`, по умолчанию `Торговля/lab-data/open-automata`.
Только stdlib — запускается любым Python 3.12 без окружения проекта. Binance отвечает с
рабочей машины без входа; список — не больше 30 строк на страницу (≈20 мин на весь),
сделки — 50 на страницу.

Скан пишет строку сразу после каждого портфеля (`flush`) и при перезапуске пропускает уже
записанные `pid` — обрыв связи стоит только перезапуска той же командой. Недописанная
последняя строка (процесс убит посреди записи) отрезается при старте. Ошибки сети — в
`bn_scan_err.jsonl`, такие портфели повторяются при следующем запуске. Сырые закрытые позиции
активных портфелей — в `bn_scan_pos.jsonl`: окно истории у Binance скользящее, позже тех же
сделок уже не скачать.

Пороги подписи (раздел 4 записки) объявлены 24.10.2026 ДО пилота и не меняются; правила
классов и порядка шорт-листа — во время полного скана, до взгляда на его итог.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from statistics import median

API = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade"
FAPI_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0",
    "clienttype": "web",
}
DATA = Path(__file__).resolve().parents[2] / "lab-data" / "open-automata"
CRYPTOSMX = "5064581361618552064"  # положительный контроль: T = 0.65 на 420 с
MIRROR = "5256963119481330945"  # кандидат в «зеркало CryptosMX», открывает очередь разбора
SEED = 20261024
SCAN_PAGES = 4  # ≤ 200 последних закрытых позиций, как в пилоте

# Пороги подписи — раздел 4 записки, объявлены до пилота 24.10.2026. Не менять.
MIN_N = 30  # закрытых позиций в окне
MAX_PER_DAY = 200  # выше — маркет-мейкер или HFT: преимущество в скорости, не скопировать
RULES: dict[str, Callable[[dict], bool]] = {
    "T_timer_on_losses": lambda f: f["timer_loss_share"] >= 0.40 and f["timer_loss_mode_s"] >= 30
    and f["n"] * (1 - f["win_share"]) >= 10,
    "A_timer_on_all": lambda f: f["timer_all_share"] >= 0.40 and f["timer_all_mode_s"] >= 30,
    "P_fixed_target": lambda f: f["tp_share"] >= 0.40 and f["n"] * f["win_share"] >= 20,
    "M_fixed_minute": lambda f: f["top_minute_share"] >= 0.20,
}

# Классы и шорт-лист — объявлены во время скана, до взгляда на его итог.
TRADFI = 0.50  # доля сделок на TRADIFI_PERPETUAL — портфель на перпах акций/товаров
MART_WIN = 0.97  # P и прибыльных ≥ 97% — усреднение без стопа (мартингейл), для лаборатории закрыт
M_EVENT = 0.50  # M и больше половины входов в одну минуту — вход по часам свечи
COPY_OVERLAP = 0.5  # общих входов (символ, сторона, минута) от меньшего набора — копия бота
COPY_MIN = 10
ORDER = ("timer_T", "timer_A", "event_M")
ORDER_KEY = {"timer_T": "timer_loss_share", "timer_A": "timer_all_share",
             "event_M": "top_minute_share"}

RETRY_SLEEP_S = (5, 30, 90)  # на портфель; при 429/418 — вчетверо дольше
ABORT_AFTER = 25  # столько портфелей подряд с ошибкой — стоп, перезапуск продолжит
PROGRESS_EVERY = 100


# --- сеть --------------------------------------------------------------------------------


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=HEADERS,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8")).get("data") or {}


def history(pid: str, max_pages: int = 10) -> tuple[list[dict], int]:
    """Закрытые позиции портфеля, новые первыми, и их общее число по бирже."""
    rows: list[dict] = []
    total = 0
    for page in range(1, max_pages + 1):
        data = _post(f"{API}/lead-portfolio/position-history",
                     {"pageNumber": page, "pageSize": 50, "portfolioId": pid})
        lst = data.get("list") or []
        total = int(data.get("total") or 0)
        rows += lst
        if not lst or len(rows) >= total:
            break
        time.sleep(0.4)
    return rows, total


def fetch(pid: str) -> tuple[list[dict], int]:
    last: Exception | None = None
    for wait in (*RETRY_SLEEP_S, None):
        try:
            return history(pid, SCAN_PAGES)
        except Exception as err:
            last = err
            if wait is None:
                break
            slow = isinstance(err, urllib.error.HTTPError) and err.code in (418, 429)
            time.sleep(wait * (4 if slow else 1))
    assert last is not None
    raise last


# --- признаки ------------------------------------------------------------------------------


def _log_bin(s: float) -> int:
    return round(math.log(s) / math.log(1.05))  # корзины длительности шириной 5%


def _ret_bin(x: float) -> int:
    return round(x * 1000)  # корзины доходности по 0.1 п.п.


def mode_share(values: list[float], key: Callable[[float], int]) -> tuple[float, float]:
    """Доля значений в модальной корзине ±1 соседняя и медиана модальной корзины."""
    if not values:
        return 0.0, 0.0
    bins = Counter(key(v) for v in values)
    top, _ = bins.most_common(1)[0]
    share = sum(c for b, c in bins.items() if abs(b - top) <= 1) / len(values)
    modal = median([v for v in values if key(v) == top])
    return share, modal


def closed_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("closed") and r.get("avgCost")]


def features(rows: list[dict]) -> dict:
    """Признаки подписи по закрытым позициям. Доходность — по средним ценам, без комиссий."""
    closed = closed_rows(rows)
    if not closed:
        return {"n": 0}
    dur = []
    ret = []
    for r in closed:
        d = (int(r["closed"]) - int(r["opened"])) / 1000
        px0, px1 = float(r["avgCost"]), float(r["avgClosePrice"] or 0)
        if px0 <= 0 or px1 <= 0:
            continue
        x = px1 / px0 - 1
        if str(r.get("side")) == "Short":
            x = -x
        dur.append(max(d, 1.0))
        ret.append(x)
    losses = [d for d, x in zip(dur, ret, strict=True) if x < 0]
    wins = [x for x in ret if x > 0]
    t_loss, t_loss_mode = mode_share(losses, _log_bin)
    t_all, t_all_mode = mode_share(dur, _log_bin)
    tp, tp_mode = mode_share(wins, _ret_bin)
    opened = sorted(int(r["opened"]) for r in closed)
    span_d = max((opened[-1] - opened[0]) / 86_400_000, 1.0)
    minute = Counter((o // 60_000) % 60 for o in opened)
    sym = Counter(r.get("symbol") for r in closed)
    return {
        "n": len(dur),
        "span_d": round(span_d, 1),
        "per_day": round(len(dur) / span_d, 1),
        "symbols": len(sym),
        "top_symbol_share": round(sym.most_common(1)[0][1] / len(closed), 2),
        "long_share": round(sum(str(r.get("side")) == "Long" for r in closed) / len(closed), 2),
        "win_share": round(len(wins) / len(ret), 2) if ret else 0,
        "median_dur_s": round(median(dur)),
        "timer_loss_share": round(t_loss, 2), "timer_loss_mode_s": round(t_loss_mode),
        "timer_all_share": round(t_all, 2), "timer_all_mode_s": round(t_all_mode),
        "tp_share": round(tp, 2), "tp_mode_pct": round(tp_mode * 100, 2),
        "top_minute_share": round(minute.most_common(1)[0][1] / len(opened), 2),
        "median_lev": median(float(r.get("leverage") or 0) for r in closed),
    }


def classify(f: dict) -> tuple[str, list[str]]:
    """Допуск к подписи и сработавшие признаки."""
    if f.get("n", 0) < MIN_N:
        return "too_few", []
    if f["per_day"] > MAX_PER_DAY:
        return "mm_hft", []
    return "eligible", [k for k, rule in RULES.items() if rule(f)]


def klass(f: dict, hits: list[str]) -> str:
    """Класс портфеля с подписью; первое совпадение выигрывает."""
    if f.get("tradfi_share", 0) >= TRADFI:
        return "tradfi"
    if "P_fixed_target" in hits and f["win_share"] >= MART_WIN:
        return "martingale"
    if "T_timer_on_losses" in hits:
        return "timer_T"
    if "A_timer_on_all" in hits:
        return "timer_A"
    if "M_fixed_minute" in hits:
        return "event_M" if f["top_minute_share"] >= M_EVENT else "M_weak"
    return "P_other"


def scan_row(pid: str, meta: dict, pos: list[dict], total: int, tradfi: set[str]) -> dict:
    f = features(pos)
    status, hits = classify(f)
    closed = closed_rows(pos)
    syms = Counter(str(p.get("symbol")) for p in closed)
    minute = Counter((int(p["opened"]) // 60_000) % 60 for p in closed)
    f.update(
        pid=pid, total=total, fetched=len(pos), status=status, hits=hits,
        nick=meta.get("nickname"), copiers=meta.get("currentCopyCount"),
        tradfi_tag=meta.get("tradFiTag"),
        top_minute=minute.most_common(1)[0][0] if minute else None,
        tradfi_share=round(sum(c for s, c in syms.items() if s in tradfi) / len(closed), 2)
        if closed else 0.0,
        syms=dict(syms.most_common()),
    )
    return f


# --- файлы ---------------------------------------------------------------------------------


def heal(path: Path) -> bool:
    """Отрезать недописанную последнюю строку, чтобы дозапись началась с чистой строки."""
    if not path.exists():
        return False
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        path.write_bytes(data[: data.rfind(b"\n") + 1])
        return True
    return False


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def last_by_pid(path: Path) -> dict[str, dict]:
    return {r["pid"]: r for r in read_jsonl(path)}


def api_pool(data: Path) -> list[dict]:
    """Портфели с меткой API в случайном, но воспроизводимом порядке."""
    pool = sorted((r for r in read_jsonl(data / "bn_list.jsonl")
                   if r.get("apiKeyTag") == "API_KEY_TRADE"),
                  key=lambda r: str(r["leadPortfolioId"]))
    random.Random(SEED).shuffle(pool)  # оборванный скан — всё равно случайная выборка
    return pool


# --- подкоманды ----------------------------------------------------------------------------


def cmd_list(data: Path, window: str) -> int:
    rows: dict[str, dict] = {}
    page = 1
    while True:
        body = {"pageNumber": page, "pageSize": 100, "timeRange": window, "dataType": "ROI",
                "favoriteOnly": False, "hideFull": False, "nickname": "", "order": "DESC",
                "portfolioType": "ALL", "sortBy": "ROI"}
        try:
            d = _post(f"{API}/home-page/query-list", body)
        except Exception as err:
            print(f"page {page}: {type(err).__name__} {err}")
            break
        if page == 1:
            print(f"total by exchange: {d.get('total')}")
        lst = d.get("list") or []
        if not lst:
            break
        for row in lst:
            row.pop("chartItems", None)
            rows[str(row.get("leadPortfolioId"))] = row
        page += 1
        time.sleep(0.3)
    data.mkdir(parents=True, exist_ok=True)
    with (data / "bn_list.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows.values():
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tags = Counter(str(r.get("apiKeyTag")) for r in rows.values())
    print(f"pages read: {page - 1}, unique portfolios: {len(rows)}; apiKeyTag: {dict(tags)}")
    return 0


def cmd_tradfi(data: Path) -> int:
    with urllib.request.urlopen(urllib.request.Request(FAPI_INFO, headers=HEADERS),
                                timeout=60) as r:
        info = json.loads(r.read().decode("utf-8"))
    syms = sorted(s["symbol"] for s in info["symbols"]
                  if s.get("contractType") == "TRADIFI_PERPETUAL")
    data.mkdir(parents=True, exist_ok=True)
    snap = {"source": "fapi/v1/exchangeInfo contractType=TRADIFI_PERPETUAL",
            "fetched": time.strftime("%Y-%m-%d", time.gmtime()), "symbols": syms}
    (data / "bn_tradfi_symbols.json").write_text(json.dumps(snap, indent=0), encoding="utf-8")
    print(f"TRADIFI_PERPETUAL symbols: {len(syms)}")
    return 0


def cmd_probe(pids: list[str]) -> int:
    for pid in pids:
        try:
            rows, total = history(pid)
        except Exception as err:
            print(pid, "ERR", type(err).__name__, err)
            continue
        f = features(rows)
        f["total"] = total
        print(pid, json.dumps(f))
        time.sleep(0.4)
    return 0


def cmd_scan(data: Path, limit: int | None = None) -> int:
    out_p = data / "bn_scan.jsonl"
    pos_p = data / "bn_scan_pos.jsonl"
    err_p = data / "bn_scan_err.jsonl"
    for p in (out_p, pos_p, err_p):
        if heal(p):
            print(f"healed {p.name}: dropped a half-written last line", flush=True)
    pool = api_pool(data)
    done = set(last_by_pid(out_p))
    todo = [r for r in pool if str(r["leadPortfolioId"]) not in done][:limit]
    tradfi = set(json.loads((data / "bn_tradfi_symbols.json").read_text(encoding="utf-8"))
                 ["symbols"])
    print(f"pool {len(pool)}; already done {len(done)}; to do {len(todo)}", flush=True)
    stats: Counter[str] = Counter()
    fails_in_row = 0
    t0 = time.time()
    with out_p.open("a", encoding="utf-8") as out, pos_p.open("a", encoding="utf-8") as pos_out, \
            err_p.open("a", encoding="utf-8") as err_out:
        for i, meta in enumerate(todo, 1):
            pid = str(meta["leadPortfolioId"])
            try:
                pos, total = fetch(pid)
            except Exception as err:
                stats["err"] += 1
                fails_in_row += 1
                err_out.write(json.dumps({"pid": pid, "err": type(err).__name__,
                                          "msg": str(err)[:200], "at": int(time.time())}) + "\n")
                err_out.flush()
                if fails_in_row >= ABORT_AFTER:
                    print(f"ABORT: {fails_in_row} portfolios failed in a row; rerun to resume",
                          flush=True)
                    return 2
                continue
            fails_in_row = 0
            f = scan_row(pid, meta, pos, total, tradfi)
            out.write(json.dumps(f, ensure_ascii=False) + "\n")
            out.flush()
            if f["status"] != "too_few":
                pos_out.write(json.dumps({"pid": pid, "rows": closed_rows(pos)},
                                         ensure_ascii=False) + "\n")
                pos_out.flush()
            stats[f["status"]] += 1
            if f["hits"]:
                stats["flagged"] += 1
            if i % PROGRESS_EVERY == 0 or i == len(todo):
                el = time.time() - t0
                eta = el / i * (len(todo) - i)
                print(f"{len(done) + i}/{len(pool)} {el:.0f}s eta {eta / 60:.0f}min "
                      + " ".join(f"{k}={v}" for k, v in sorted(stats.items())), flush=True)
            time.sleep(0.4)
    print(f"scan finished in {time.time() - t0:.0f}s", flush=True)
    return cmd_summary(out_p)


def cmd_summary(path: Path) -> int:
    rows = last_by_pid(path)
    stats: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    for r in rows.values():
        if "err" in r:
            stats["err"] += 1
            continue
        status, hits = classify(r)  # пересчёт: в файле пилота нет полей status/hits
        stats[status] += 1
        if hits:
            stats["flagged"] += 1
        counts.update(hits)
    print(f"file {path.name}: portfolios {len(rows)}")
    print("stats:", dict(sorted(stats.items())))
    print("by rule:", {k: counts[k] for k in RULES})
    return 0


def entries(rows: list[dict]) -> set[tuple]:
    return {(r.get("symbol"), r.get("side"), int(r["opened"]) // 60_000) for r in rows}


def fold_copies(ordered: list[dict], ent: dict[str, set[tuple]]) -> list[dict]:
    """Свернуть копии одного бота в первую строку очереди, где он встретился."""
    kept: list[dict] = []
    for r in ordered:
        e = ent.get(r["pid"], set())
        r["copies"] = []
        for q in kept:
            eq = ent.get(q["pid"], set())
            small = min(len(e), len(eq))
            if small >= COPY_MIN and len(e & eq) / small >= COPY_OVERLAP:
                q["copies"].append(r["pid"])
                break
        else:
            kept.append(r)
    return kept


def load_entries(path: Path, pids: set[str]) -> dict[str, set[tuple]]:
    out: dict[str, set[tuple]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or not any(p in line[:40] for p in pids):
                continue
            d = json.loads(line)
            if d["pid"] in pids:
                out[d["pid"]] = entries(d["rows"])
    return out


EntLoader = Callable[[set[str]], dict[str, set[tuple]]]


def shortlist(rows: dict[str, dict],
              ent_loader: EntLoader) -> tuple[list[dict], dict[str, Counter]]:
    """Классы и шорт-лист: таймеры и событийные M, без мартингейла и перпов акций."""
    stats: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    cand: dict[str, list[dict]] = {k: [] for k in ORDER}
    for r in rows.values():
        status, hits = classify(r)
        stats[status] += 1
        if status != "eligible" or not hits:
            continue
        stats["flagged"] += 1
        rules.update(hits)
        k = klass(r, hits)
        r["klass"] = k
        classes[k] += 1
        if k in cand and r["pid"] != CRYPTOSMX:
            cand[k].append(r)
    ordered: list[dict] = []
    for k in ORDER:
        ordered += sorted(cand[k], key=lambda r, k=k: (-r[ORDER_KEY[k]], -r["n"], r["pid"]))
    kept = fold_copies(ordered, ent_loader({r["pid"] for r in ordered}))
    mirror = rows.get(MIRROR)
    if mirror and classify(mirror)[0] != "too_few":
        kept = [r for r in kept if r["pid"] != MIRROR]
        mirror["copies"] = mirror.get("copies", [])
        mirror.setdefault("klass", "no_hit")
        kept.insert(0, mirror)
    return kept, {"status": stats, "rules": rules, "classes": classes}


def fmt_dur(s: float) -> str:
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.0f} min"
    if s < 172_800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} d"


def md_table(kept: list[dict]) -> list[str]:
    lines = ["| # | pid | ник | класс | монет | лонг | сделок/сут | таймер убытков | таймер всех "
             "| цель | минута часа | плечо | копии |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(kept, 1):
        nick = str(r.get("nick") or "").replace("|", "/")
        lines.append(
            f"| {i} | `{r['pid']}` | {nick} | {r['klass']} | {r['symbols']} "
            f"| {r['long_share']:.2f} | {r['per_day']} "
            f"| {fmt_dur(r['timer_loss_mode_s'])} · {r['timer_loss_share']:.2f} "
            f"| {fmt_dur(r['timer_all_mode_s'])} · {r['timer_all_share']:.2f} "
            f"| {r['tp_mode_pct']:+.2f}% · {r['tp_share']:.2f} "
            f"| :{r['top_minute']:02d} · {r['top_minute_share']:.2f} "
            f"| ×{r['median_lev']:g} | {len(r['copies']) or ''} |")
    return lines


def cmd_review(data: Path, md: bool) -> int:
    rows = last_by_pid(data / "bn_scan.jsonl")
    errs = set(last_by_pid(data / "bn_scan_err.jsonl")) - set(rows)
    pool = len(api_pool(data))
    kept, agg = shortlist(rows, lambda pids: load_entries(data / "bn_scan_pos.jsonl", pids))
    print(f"pool {pool}; scanned {len(rows)}; errors not scanned {len(errs)}")
    print("status:", dict(sorted(agg["status"].items())), f"(MIN_N {MIN_N})")
    print("by rule:", {k: agg["rules"][k] for k in RULES})
    print("by class:", dict(agg["classes"].most_common()))
    for name, pid in (("CryptosMX control", CRYPTOSMX), ("mirror candidate", MIRROR)):
        r = rows.get(pid)
        if r:
            print(f"{name}:", classify(r), r.get("klass"), "n", r["n"], "long", r["long_share"],
                  "T", r["timer_loss_share"], r["timer_loss_mode_s"],
                  "tp", r["tp_mode_pct"], r["tp_share"])
    folded = sum(len(r["copies"]) for r in kept)
    print(f"shortlist: {len(kept)} (+{folded} copies folded); by class:",
          dict(Counter(r["klass"] for r in kept)))
    cols = ["rank", "pid", "klass", "nick", "n", "span_d", "symbols", "long_share", "per_day",
            "timer_loss_mode_s", "timer_loss_share", "timer_all_mode_s", "timer_all_share",
            "tp_mode_pct", "tp_share", "top_minute", "top_minute_share", "median_lev",
            "win_share", "tradfi_share", "copies", "top_syms"]
    with (data / "bn_shortlist.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for i, r in enumerate(kept, 1):
            row = {**r, "rank": i, "copies": " ".join(r["copies"]),
                   "top_syms": " ".join(list(r["syms"])[:5])}
            w.writerow([row.get(c) for c in cols])
    print("written bn_shortlist.csv")
    if md:
        print()
        print("\n".join(md_table(kept)))
    return 0


def main(argv: list[str] | None = None) -> int:
    reconf = getattr(sys.stdout, "reconfigure", None)
    if reconf:
        reconf(encoding="utf-8", errors="replace")  # ники — не cp1251
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=DATA, help="папка данных вне репозитория")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="весь список лидеров → bn_list.jsonl")
    p.add_argument("--window", default="30D")
    sub.add_parser("tradfi", help="символы TRADIFI_PERPETUAL → bn_tradfi_symbols.json")
    p = sub.add_parser("probe", help="подпись одного или нескольких портфелей")
    p.add_argument("pids", nargs="+")
    p = sub.add_parser("scan", help="весь пул с меткой API → bn_scan.jsonl")
    p.add_argument("--limit", type=int, help="только первые N несделанных (пробный прогон)")
    p.add_argument("--summary", nargs="?", const="bn_scan.jsonl", metavar="FILE",
                   help="итог по файлу в папке данных, без сети")
    p = sub.add_parser("review", help="классы и шорт-лист → bn_shortlist.csv")
    p.add_argument("--md", action="store_true", help="ещё и таблица для записки")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        return cmd_list(a.data, a.window)
    if a.cmd == "tradfi":
        return cmd_tradfi(a.data)
    if a.cmd == "probe":
        return cmd_probe(a.pids)
    if a.cmd == "scan":
        if a.summary:
            return cmd_summary(a.data / a.summary)
        return cmd_scan(a.data, a.limit)
    return cmd_review(a.data, a.md)


if __name__ == "__main__":
    sys.exit(main())
