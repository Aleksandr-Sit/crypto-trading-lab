#!/usr/bin/env python
"""Настоящий круг издержек на перпах Bybit в моменты правила «гашение первого часа».

Зачем (26.09.2026). Замер движком `cex-perp-paper-day-open-fade` дал `failed`: эффект
цены +0.22…+0.26% на сделку, а круг модели лаборатории для Bybit ≈0.26% (тейкер 10 б.п.
и полспреда 2.5 б.п. на сторону, `config/costs.yaml`). Модель общая на всю лабораторию,
и понижать её под результат нельзя — поэтому круг меряется по данным, ровно в те минуты,
когда правило торгует: вход в 01:00 UTC, выход в 00:00 UTC. В эти минуты толпа дневных
правил торгует вместе с нами, и спред может быть шире обычного — для этого контрольные
моменты 00:30 и 02:30 тех же суток.

Две стадии.

`tape` — спред по ИСТОРИИ, из публичного архива сделок Bybit
(`public.bybit.com/trading/<SYMBOL>/<SYMBOL><дата>.csv.gz`, сторона агрессора в каждой
строке). Две соседние сделки разной стороны — покупка прошла по ask, продажа по bid —
дают разницу цен, равную спреду в эту секунду; медиана по минуте окна устойчива к
движению цены между ними. Сделки с флагом `RPI` (заявки улучшения цены для розницы)
выбрасываются: заявка через API их не видит. Архив бывает отсортирован от конца суток
к началу (так до 2022 года) — тогда файл читается целиком, иначе чтение обрывается
после последнего окна.

`book` — стакан СЕЙЧАС (или серия снимков вокруг `--at HH:MM` UTC): спред, глубина
в пределах 5 и 10 б.п. от середины и цена исполнения рыночной заявки на заданные
суммы — то, чего из ленты сделок не узнать.

    python scripts/bybit_spread.py --stage tape --out /app/data/costs/bybit_tape.jsonl
    python scripts/bybit_spread.py --stage tape --report /app/data/costs/bybit_tape.jsonl
    python scripts/bybit_spread.py --stage book
    python scripts/bybit_spread.py --stage book --at 01:00 --span 90 --every-s 5
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median, quantiles

ARCHIVE = "https://public.bybit.com/trading/{s}/{s}{d}.csv.gz"
BOOK = "https://api.bybit.com/v5/market/orderbook?category=linear&symbol={s}&limit=200"
# Те же монеты и те же начала рядов, что в замере движком (15m, venue=bybit на сервере).
START = {
    "BTCUSDT": date(2020, 11, 1),
    "ETHUSDT": date(2021, 3, 15),
    "SOLUSDT": date(2022, 9, 22),
    "XRPUSDT": date(2022, 9, 22),
    "ADAUSDT": date(2022, 9, 22),
    "AVAXUSDT": date(2022, 9, 22),
    "DOGEUSDT": date(2022, 9, 22),
    "LINKUSDT": date(2022, 9, 22),
}
# Окна: момент суток UTC (минуты от полуночи) → метка. Правило — 00:00 и 01:00.
ANCHORS = {0: "00:00 выход", 60: "01:00 вход", 30: "00:30 контр.", 150: "02:30 контр."}
WINDOW_S = 60
UA = {"User-Agent": "crypto-trading-lab/bybit_spread"}


# -- лента сделок ---------------------------------------------------------------------


def _open(url: str, timeout: int = 60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def window_rows(symbol: str, day: date) -> dict[int, list[tuple[float, str, float]]] | None:
    """Сделки (время, сторона, цена) в окнах якорей; None — файла нет."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()
    bounds = {a: (start + a * 60, start + a * 60 + WINDOW_S) for a in ANCHORS}
    last_end = max(b for _, b in bounds.values())
    out: dict[int, list[tuple[float, str, float]]] = {a: [] for a in ANCHORS}
    try:
        resp = _open(ARCHIVE.format(s=symbol, d=day.isoformat()))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    order = 0  # 1 — от начала суток, −1 — от конца
    prev_ts: float | None = None
    with resp, gzip.GzipFile(fileobj=resp) as gz:
        for row in csv.DictReader(io.TextIOWrapper(gz, encoding="utf-8")):
            ts = float(row["timestamp"])
            if ts > 1e11:  # миллисекунды
                ts /= 1000
            if order == 0 and prev_ts is not None and ts != prev_ts:
                order = 1 if ts > prev_ts else -1
            prev_ts = ts
            if order == 1 and ts >= last_end:
                break  # дальше окон нет — не качаем остаток суток
            if row.get("RPI", "0") not in ("0", ""):
                continue
            for a, (lo, hi) in bounds.items():
                if lo <= ts < hi:
                    out[a].append((ts, row["side"], float(row["price"])))
                    break
    if order == -1:
        for a in out:
            out[a].reverse()  # порядок исполнения — от ранних к поздним
    return out


def bounce_bps(rows: list[tuple[float, str, float]]) -> tuple[float | None, int, float | None]:
    """Медиана |Δцены| между соседними сделками разной стороны, б.п.; число пар; тик, б.п."""
    diffs: list[float] = []
    tick: float | None = None
    for (_, s0, p0), (_, s1, p1) in zip(rows, rows[1:], strict=False):
        d = abs(p1 - p0)
        mid = (p0 + p1) / 2
        if d > 0:
            t = d / mid * 1e4
            tick = t if tick is None else min(tick, t)
        if s0 != s1 and mid > 0:
            diffs.append(d / mid * 1e4)
    return (median(diffs) if diffs else None), len(diffs), tick


def stage_tape(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            done.add((rec["symbol"], rec["day"]))
    until = date.fromisoformat(args.until) if args.until else date.today() - timedelta(days=1)
    days: list[date] = []
    d = date.fromisoformat(args.since)
    while d <= until:
        days.append(d)
        d += timedelta(days=args.every)
    todo = [
        (s, d)
        for d in days
        for s in START
        if d >= START[s] and (s, d.isoformat()) not in done
    ][: args.limit or None]
    print(f"дней в выборке {len(days)}, файлов к разбору {len(todo)}, уже есть {len(done)}")
    t0 = time.time()
    with out.open("a", encoding="utf-8") as fh:
        for i, (symbol, day) in enumerate(todo, 1):
            try:
                rows = window_rows(symbol, day)
            except Exception as err:  # noqa: BLE001 — сеть: отказ одного файла не роняет прогон
                print(f"  {symbol} {day}: отказ {type(err).__name__}: {err}")
                continue
            if rows is None:
                continue
            rec: dict[str, object] = {"symbol": symbol, "day": day.isoformat()}
            for a, part in rows.items():
                b, n, tick = bounce_bps(part)
                rec[str(a)] = {"bps": b, "pairs": n, "trades": len(part), "tick": tick}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            if i % 50 == 0:
                print(f"  {i}/{len(todo)}  ({time.time() - t0:.0f} с)")
    print(f"готово: {len(todo)} файлов за {time.time() - t0:.0f} с")
    return report(out, args.taker_bps)


def _vals(recs: list[dict], anchor: int, field: str = "bps", **match: str) -> list[float]:
    """Значения поля окна `anchor` у записей, где совпали `symbol=` и/или `year=`."""
    out: list[float] = []
    for r in recs:
        if "symbol" in match and r["symbol"] != match["symbol"]:
            continue
        if "year" in match and not r["day"].startswith(match["year"]):
            continue
        v = r[str(anchor)][field]
        if v is not None:
            out.append(v)
    return out


def report(path: Path, taker_bps: float) -> int:
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    print("\nСпред по ленте, б.п. (полный, bid–ask): медиана по дням / 90-й процентиль.")
    print(f"Окно {WINDOW_S} с после момента; в скобках — сколько дней с оценкой.\n")
    keys = sorted(ANCHORS)
    print(
        f"{'монета':9} "
        + " ".join(f"{ANCHORS[a]:>20}" for a in keys)
        + f" {'тик':>6} {'круг, %':>8}"
    )
    rounds: list[float] = []
    for symbol in START:
        cells = []
        for a in keys:
            vals = _vals(recs, a, symbol=symbol)
            if len(vals) >= 2:
                p90 = quantiles(vals, n=10)[-1]
                cells.append(f"{median(vals):6.2f} / {p90:6.2f} ({len(vals):3})")
            else:
                cells.append(f"{'—':>20}")
        ticks = [t for a in keys for t in _vals(recs, a, "tick", symbol=symbol)]
        tick = f"{median(ticks):6.2f}" if ticks else "—"
        entry, exit_ = _vals(recs, 60, symbol=symbol), _vals(recs, 0, symbol=symbol)
        if entry and exit_:
            # Тейкер платит комиссию на каждой стороне и полспреда на входе и на выходе.
            rt = (2 * taker_bps + median(entry) / 2 + median(exit_) / 2) / 100
            rounds.append(rt)
            rt_s = f"{rt:8.3f}"
        else:
            rt_s = f"{'—':>8}"
        print(f"{symbol:9} " + " ".join(cells) + f" {tick:>6} {rt_s}")
    if rounds:
        print(
            f"\nКруг по тейкеру ({taker_bps} б.п. на сторону + полспреда в 01:00 и в 00:00): "
            f"медиана по монетам {median(rounds):.3f}%, худшая {max(rounds):.3f}%."
        )

    print("\nПо годам, все монеты (медиана по дням), б.п.:")
    years = sorted({r["day"][:4] for r in recs})
    print(f"{'год':5} " + " ".join(f"{ANCHORS[a]:>14}" for a in keys))
    for y in years:
        cells = []
        for a in keys:
            vals = _vals(recs, a, year=y)
            cells.append(f"{median(vals):14.2f}" if vals else f"{'—':>14}")
        print(f"{y:5} " + " ".join(cells))
    return 0


# -- стакан ---------------------------------------------------------------------------


def book_snapshot(symbol: str, sizes: list[float]) -> dict[str, float]:
    with _open(BOOK.format(s=symbol), timeout=15) as resp:
        data = json.load(resp)["result"]
    asks = [(float(p), float(q)) for p, q in data["a"]]
    bids = [(float(p), float(q)) for p, q in data["b"]]
    mid = (asks[0][0] + bids[0][0]) / 2
    snap = {"spread_bps": (asks[0][0] - bids[0][0]) / mid * 1e4}
    for side, levels in (("ask", asks), ("bid", bids)):
        for band in (5, 10):
            lim = mid * (1 + band / 1e4) if side == "ask" else mid * (1 - band / 1e4)
            inside = [(p, q) for p, q in levels if (p <= lim if side == "ask" else p >= lim)]
            snap[f"{side}_depth_{band}bps_usd"] = sum(p * q for p, q in inside)
        for usd in sizes:
            left, cost, got = usd, 0.0, 0.0
            for p, q in levels:
                take = min(left, p * q)
                cost += take
                got += take / p
                left -= take
                if left <= 0:
                    break
            if left > 0:
                snap[f"{side}_{int(usd)}_bps"] = float("nan")
                continue
            vwap = cost / got
            snap[f"{side}_{int(usd)}_bps"] = abs(vwap - mid) / mid * 1e4
    return snap


def stage_book(args: argparse.Namespace) -> int:
    sizes = [float(x) for x in args.sizes.split(",")]
    moments: list[datetime | None] = [None]
    if args.at:
        hh, mm = (int(x) for x in args.at.split(":"))
        now = datetime.now(UTC)
        anchor = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if anchor + timedelta(seconds=args.span) < now:
            anchor += timedelta(days=1)
        first = anchor - timedelta(seconds=args.span)
        wait = (first - now).total_seconds()
        if wait > 0:
            print(f"ждём до {first:%Y-%m-%d %H:%M:%S} UTC ({wait / 60:.0f} мин)", flush=True)
            time.sleep(wait)
        n = int(2 * args.span / args.every) + 1
        moments = [first + timedelta(seconds=args.every * i) for i in range(n)]
    rows: list[dict] = []
    for m in moments:
        if m is not None:
            pause = (m - datetime.now(UTC)).total_seconds()
            if pause > 0:
                time.sleep(pause)
        at = datetime.now(UTC)
        for symbol in START:
            try:
                snap = book_snapshot(symbol, sizes)
            except Exception as err:  # noqa: BLE001
                print(f"  {symbol}: отказ {type(err).__name__}: {err}", flush=True)
                continue
            rows.append({"symbol": symbol, "at": at.isoformat(), **snap})
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    label = f"вокруг {args.at} UTC ±{args.span} с, снимков {len(moments)}" if args.at else "сейчас"
    print(f"\nСтакан Bybit, {label}. Медиана по снимкам (худший — в скобках).")
    cols = ["spread_bps"] + [f"{s}_{int(u)}_bps" for u in sizes for s in ("ask", "bid")]
    cols += ["ask_depth_10bps_usd", "bid_depth_10bps_usd"]
    print(f"{'монета':9} " + " ".join(f"{c:>22}" for c in cols))
    for symbol in START:
        mine = [r for r in rows if r["symbol"] == symbol]
        if not mine:
            continue
        cells = []
        for c in cols:
            vals = [r[c] for r in mine if r[c] == r[c]]
            if not vals:
                cells.append(f"{'—':>22}")
            elif c.endswith("_usd"):
                cells.append(f"{median(vals):>12,.0f} ({min(vals):>7,.0f})")
            else:
                cells.append(f"{median(vals):>12.3f} ({max(vals):>7.3f})")
        print(f"{symbol:9} " + " ".join(cells))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=["tape", "book"], required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--report", default="", help="только отчёт по готовому файлу ленты")
    ap.add_argument("--since", default="2021-01-04")
    ap.add_argument("--until", default="")
    ap.add_argument("--every", type=int, default=14, help="шаг выборки дней, суток")
    ap.add_argument("--limit", type=int, default=0, help="не больше N файлов (проба)")
    ap.add_argument("--at", default="", help="HH:MM UTC — серия снимков стакана вокруг момента")
    ap.add_argument("--span", type=int, default=90, help="± секунд вокруг --at")
    ap.add_argument("--every-s", dest="every_s", type=int, default=5)
    ap.add_argument("--sizes", default="312,3000", help="суммы рыночной заявки, USD")
    ap.add_argument(
        "--taker-bps",
        dest="taker_bps",
        type=float,
        default=5.5,
        help="комиссия тейкера на сторону, б.п. (Bybit без VIP для перпов USDT — 5.5)",
    )
    args = ap.parse_args()
    if args.stage == "tape":
        if args.report:
            return report(Path(args.report), args.taker_bps)
        if not args.out:
            ap.error("--stage tape требует --out")
        return stage_tape(args)
    args.every = args.every_s
    return stage_book(args)


if __name__ == "__main__":
    raise SystemExit(main())
