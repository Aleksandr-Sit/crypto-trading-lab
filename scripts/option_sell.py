#!/usr/bin/env python
"""Продажа волатильности как СДЕЛКА: недельные опционы Deribit, издержки, хвост, размер.

Зачем. `vol_premium.py` показал премию за риск волатильности — 6.6–7.1 пункта при шуме
~3 (`docs/research/new-markets-2026-09-21.md`). Но пункт волатильности — не доходность:
продавцу достаётся премия минус спред, минус комиссия, минус выплата по экспирации, а
убыток не ограничен. Этот скрипт отвечает на вопрос, что остаётся от премии в сделке и
какой размер позиции держит просадку ветки 5%.

Устройство (все варианты зафиксированы ДО первого прогона, 29.09.2026):

* каждую пятницу в 08:00 UTC истекают недельные опционы; вход — окно `--window-min`
  минут с 08:10 той же пятницы, продаётся серия, истекающая через 7 суток;
* V1 `straddle` — продать колл и пут на страйке около денег, держать до экспирации;
* V2 `strangle` — продать пут и колл на ±1σ недели (σ — из цены страддла около денег);
* V3 `ironfly` — V1 плюс купленные «крылья» на ±2σ: убыток сверху ограничен;
* цена — медиана `mark_price` биржи по сделкам окна; если у страйка сделки только по одной
  ноге, вторая достраивается паритетом (форвард = индекс; ошибка — базис за неделю);
* исполнение — по рынку: продажа по `mark·(1−h)`, покупка по `mark·(1+h)`, где `h` —
  эффективный полуспред, измеренный по сделкам агрессоров того же года и той же зоны
  удалённости страйка (|цена − mark| / mark);
* комиссия — нынешний тариф Deribit (проверен вызовом 29.09.2026, `get_instruments`):
  0.03% базового актива за ногу, но не больше 12.5% цены опциона; поставка — 0.015% за
  ногу в деньгах с тем же потолком;
* расчёт — в долларах, как на линейных USDT-опционах Bybit: выплата колла `max(D−K,0)`,
  пута `max(K−D,0)`, `D` — цена поставки Deribit в день экспирации.

Что печатается: доход на номинал за неделю со средней и шумом, знак по годам, доля
убыточных недель, худшие недели с датами, и два размера позиции — номинал = капитал
(плечо ×1) и номинал, при котором просадка всей кривой ровно 5% (лимит ветки).
Недели без котировок считаются и печатаются: пропуски не должны прятать смещение.

    python scripts/option_sell.py fetch   --currency BTC --cache data/options
    python scripts/option_sell.py measure --currency BTC --cache data/options

Только стандартная библиотека: запускается и локально, и в контейнере.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from math import log, sqrt
from pathlib import Path
from statistics import fmean, median, stdev

HISTORY = "https://history.deribit.com/api/v2/public"
LIVE = "https://www.deribit.com/api/v2/public"
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
FEE_LEG = 0.0003  # доля базового актива за ногу (get_instruments, 29.09.2026)
FEE_DELIVERY = 0.00015
FEE_CAP = 0.125  # не больше этой доли цены опциона
WEEKS_PER_YEAR = 365 / 7


def get(url: str, tries: int = 5) -> dict:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:  # сеть, 429, 5xx — ждём и повторяем
            if attempt == tries - 1:
                raise
            print(f"  повтор {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("недостижимо")


def expiry_code(d: date) -> str:
    """Дата в имени инструмента Deribit: 4JUN21, 25DEC26 (день без ведущего нуля)."""
    return f"{d.day}{MONTHS[d.month - 1]}{d.year % 100:02d}"


def fridays(start: date, end: date) -> list[date]:
    d = start + timedelta(days=(4 - start.weekday()) % 7)
    out = []
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------- fetch


def fetch_window(currency: str, t0: int, t1: int) -> list[dict]:
    """Все сделки опционами валюты в окне [t0, t1), с листанием по `has_more`."""
    seen: dict[str, dict] = {}
    start = t0
    while True:
        url = (
            f"{HISTORY}/get_last_trades_by_currency_and_time?currency={currency}"
            f"&kind=option&start_timestamp={start}&end_timestamp={t1}&count=1000&sorting=asc"
        )
        res = get(url)["result"]
        trades = res["trades"]
        for t in trades:
            seen[t["trade_id"]] = t
        if not res.get("has_more") or not trades:
            break
        last = trades[-1]["timestamp"]
        start = last if last > start else start + 1
    return sorted(seen.values(), key=lambda t: t["timestamp"])


def cmd_fetch(args: argparse.Namespace) -> int:
    cache = Path(args.cache) / args.currency.lower()
    cache.mkdir(parents=True, exist_ok=True)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=8)
    weeks = fridays(date.fromisoformat(args.start), end)
    fetched = 0
    for f in weeks:
        path = cache / f"{f.isoformat()}.json.gz"
        if path.exists():
            continue
        t0 = datetime(f.year, f.month, f.day, 8, 10, tzinfo=UTC)
        trades = fetch_window(args.currency, ms(t0), ms(t0 + timedelta(minutes=args.window_min)))
        keep = [
            {
                k: t[k]
                for k in (
                    "timestamp",
                    "instrument_name",
                    "price",
                    "mark_price",
                    "index_price",
                    "direction",
                    "amount",
                )
            }
            for t in trades
        ]
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(keep, fh)
        fetched += 1
        if fetched % 20 == 0:
            print(f"  {f}: {len(keep)} сделок, скачано недель {fetched}")
    deliv = Path(args.cache) / f"delivery_{args.currency.lower()}.json"
    rows: list[dict] = []
    offset = 0
    while True:
        res = get(
            f"{LIVE}/get_delivery_prices?index_name={args.currency.lower()}_usd"
            f"&count=1000&offset={offset}"
        )["result"]
        rows += res["data"]
        offset += len(res["data"])
        if not res["data"] or offset >= res["records_total"]:
            break
    deliv.write_text(json.dumps({r["date"]: r["delivery_price"] for r in rows}), encoding="utf-8")
    print(f"готово: недель {len(weeks)}, новых {fetched}, цен поставки {len(rows)}")
    return 0


# ---------------------------------------------------------------- measure


def parse(name: str) -> tuple[str, float, str] | None:
    parts = name.split("-")
    if len(parts) != 4:
        return None
    return parts[1], float(parts[2]), parts[3]


def zone(k: float, s: float, sig_w: float) -> str:
    """Зона удалённости страйка в сигмах недели: для полуспреда по зонам."""
    z = abs(log(k / s)) / sig_w if sig_w > 0 else 0.0
    return "atm" if z < 0.5 else ("1s" if z < 1.5 else "2s")


def load_week(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def marks_for(trades: list[dict], code: str) -> tuple[dict[tuple[float, str], float], float]:
    """Медиана mark (в валюте) по страйку и типу для серии `code`; индекс — медиана окна."""
    buckets: dict[tuple[float, str], list[float]] = defaultdict(list)
    for t in trades:
        p = parse(t["instrument_name"])
        if p and p[0] == code:
            buckets[(p[1], p[2])].append(t["mark_price"])
    s0 = median(t["index_price"] for t in trades)
    return {k: median(v) for k, v in buckets.items()}, s0


def price(marks: dict[tuple[float, str], float], k: float, kind: str, s0: float) -> float | None:
    """Цена опциона в долларах; недостающая нога — паритетом (форвард = индекс)."""
    if (k, kind) in marks:
        return marks[(k, kind)] * s0
    other = "P" if kind == "C" else "C"
    if (k, other) in marks:
        o = marks[(k, other)] * s0
        v = o + (s0 - k) if kind == "C" else o - (s0 - k)
        return v if v > 0 else None
    return None


def nearest(strikes: list[float], target: float, s0: float, tol: float) -> float | None:
    if not strikes:
        return None
    k = min(strikes, key=lambda x: abs(x - target))
    return k if abs(k - target) / s0 <= tol else None


def fee(opt_usd: float, s: float, rate: float) -> float:
    return min(rate * s, FEE_CAP * opt_usd)


def atm(trades: list[dict], code: str, tol: float):
    """Котировки серии, индекс, страйк около денег, цены его ног и σ недели из страддла.

    σ: цена страддла около денег ≈ 0.8·S·σ·√T (2·√(2/π) ≈ 1.6, половина — на ногу).
    Одна функция и для зон полуспреда, и для сделки — иначе зоны разъедутся."""
    marks, s0 = marks_for(trades, code)
    strikes = sorted({k for k, _ in marks})
    k0 = nearest(strikes, s0, s0, tol)
    if k0 is None:
        return None
    c0, p0 = price(marks, k0, "C", s0), price(marks, k0, "P", s0)
    if c0 is None or p0 is None:
        return None
    sig_w = (c0 + p0) / (0.8 * s0)
    return marks, s0, strikes, k0, sig_w


def half_spreads(
    weeks: dict[date, list[dict]], codes: dict[date, str], tol: float
) -> dict[tuple[int, str], float]:
    """Эффективный полуспред агрессора: |цена − mark| / mark, медиана по году и зоне."""
    acc: dict[tuple[int, str], list[float]] = defaultdict(list)
    for f, trades in weeks.items():
        if not trades:
            continue
        a = atm(trades, codes[f], tol)
        if a is None:
            continue
        s0, sig_w = a[1], a[4]
        for t in trades:
            p = parse(t["instrument_name"])
            if not p or p[0] != codes[f] or t["mark_price"] <= 0:
                continue
            acc[(f.year, zone(p[1], s0, sig_w))].append(
                abs(t["price"] - t["mark_price"]) / t["mark_price"]
            )
    return {k: median(v) for k, v in acc.items() if len(v) >= 20}


def trade_week(
    f: date,
    trades: list[dict],
    code: str,
    deliv: dict[str, float],
    hs: dict[tuple[int, str], float],
    variant: str,
    tol: float,
) -> dict | None:
    expiry = f + timedelta(days=7)
    d = deliv.get(expiry.isoformat())
    if d is None or not trades:
        return None
    a = atm(trades, code, tol)
    if a is None:
        return None
    marks, s0, strikes, k0, sig_w = a
    sig = sig_w / sqrt(7 / 365)

    def h(k: float) -> float:
        z = zone(k, s0, sig_w)
        return hs.get((f.year, z), hs.get((f.year, "atm"), 0.05))

    legs: list[tuple[float, str, int]] = []  # страйк, тип, знак (−1 продано, +1 куплено)
    if variant in ("straddle", "ironfly"):
        legs += [(k0, "C", -1), (k0, "P", -1)]
    if variant == "strangle":
        kp = nearest(strikes, s0 * (1 - sig_w), s0, 0.5 * sig_w)
        kc = nearest(strikes, s0 * (1 + sig_w), s0, 0.5 * sig_w)
        if kp is None or kc is None:
            return None
        legs += [(kp, "P", -1), (kc, "C", -1)]
    if variant == "ironfly":
        kp = nearest(strikes, s0 * (1 - 2 * sig_w), s0, 0.7 * sig_w)
        kc = nearest(strikes, s0 * (1 + 2 * sig_w), s0, 0.7 * sig_w)
        if kp is None or kc is None:
            return None
        legs += [(kp, "P", +1), (kc, "C", +1)]

    cash = 0.0
    costs = 0.0
    premium_mid = 0.0
    payoff = 0.0
    for k, kind, sign in legs:
        v = price(marks, k, kind, s0)
        if v is None:
            return None
        fill = v * (1 + sign * h(k))  # продажа ниже mark, покупка выше
        cash += -sign * fill
        costs += abs(v - fill) + fee(v, s0, FEE_LEG)
        premium_mid += -sign * v
        intrinsic = max(d - k, 0.0) if kind == "C" else max(k - d, 0.0)
        payoff += sign * intrinsic
        if intrinsic > 0:
            cash -= fee(intrinsic, d, FEE_DELIVERY)
            costs += fee(intrinsic, d, FEE_DELIVERY)
        cash_fee = fee(v, s0, FEE_LEG)
        cash -= cash_fee
    pnl = cash + payoff
    return {
        "date": f,
        "s0": s0,
        "d": d,
        "k0": k0,
        "sig": sig,
        "pnl": pnl,
        "ret": pnl / s0,
        "premium": premium_mid,
        "costs": costs,
        "move": d / s0 - 1,
    }


def curve(rets: list[float], lev: float) -> tuple[float, float]:
    """Годовой доход (сложный) и максимальная просадка при номинале = lev × капитал."""
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= max(1 + lev * r, 0.0)
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak if peak > 0 else 1.0)
    years = len(rets) / WEEKS_PER_YEAR
    ann = eq ** (1 / years) - 1 if eq > 0 and years > 0 else -1.0
    return ann, dd


def lev_for_dd(rets: list[float], limit: float) -> float:
    lo, hi = 0.0, 5.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if curve(rets, mid)[1] > limit:
            hi = mid
        else:
            lo = mid
    return lo


def cmd_measure(args: argparse.Namespace) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cache = Path(args.cache) / args.currency.lower()
    deliv = json.loads(
        (Path(args.cache) / f"delivery_{args.currency.lower()}.json").read_text("utf-8")
    )
    weeks = {date.fromisoformat(p.name[:10]): load_week(p) for p in sorted(cache.glob("*.json.gz"))}
    if args.until:
        weeks = {f: t for f, t in weeks.items() if f <= date.fromisoformat(args.until)}
    codes = {f: expiry_code(f + timedelta(days=7)) for f in weeks}
    hs = half_spreads(weeks, codes, args.tol)
    print(f"{args.currency}: недель в кэше {len(weeks)}, {min(weeks)} … {max(weeks)}")
    print("полуспред агрессора, медиана доли mark (год: atm / 1σ / 2σ):")
    for y in sorted({y for y, _ in hs}):
        cells = " / ".join(
            f"{hs[(y, z)] * 100:5.1f}%" if (y, z) in hs else "  —  " for z in ("atm", "1s", "2s")
        )
        print(f"  {y}: {cells}")

    for variant in ("straddle", "strangle", "ironfly"):
        rows = [
            r
            for f in sorted(weeks)
            if (r := trade_week(f, weeks[f], codes[f], deliv, hs, variant, args.tol))
        ]
        print(
            f"\n=== {variant}: недель {len(rows)} из {len(weeks)} "
            f"(пропущено {len(weeks) - len(rows)})"
        )
        if len(rows) < 20:
            continue
        rets = [r["ret"] for r in rows]
        m, se = fmean(rets), stdev(rets) / sqrt(len(rets))
        prem = fmean(r["premium"] / r["s0"] for r in rows)
        cost = fmean(r["costs"] / r["s0"] for r in rows)
        print(
            f"  на номинал за неделю: {m * 100:+.3f}% ±{se * 100:.3f} (шум); премия по mark "
            f"{prem * 100:.3f}%, издержки {cost * 100:.3f}% "
            f"({cost / prem * 100 if prem else 0:.0f}% премии)"
        )
        print(
            f"  убыточных недель {sum(r < 0 for r in rets) / len(rets) * 100:.0f}%; "
            f"худшая {min(rets) * 100:+.2f}%, лучшая {max(rets) * 100:+.2f}%"
        )
        by_year: dict[int, list[float]] = defaultdict(list)
        for r in rows:
            by_year[r["date"].year].append(r["ret"])
        cells = "  ".join(
            f"{y}:{fmean(v) * 100:+.2f}({len(v)})" for y, v in sorted(by_year.items())
        )
        plus = sum(fmean(v) > 0 for v in by_year.values())
        print(
            f"  по годам (средняя за неделю, недель): {cells}  — в плюсе {plus} из {len(by_year)}"
        )
        for lev in (1.0,):
            ann, dd = curve(rets, lev)
            print(f"  номинал = капитал: {ann * 100:+.1f}% годовых, просадка {dd * 100:.1f}%")
        lev5 = lev_for_dd(rets, 0.05)
        ann5, _ = curve(rets, lev5)
        print(
            f"  номинал для просадки 5%: {lev5:.3f} капитала → {ann5 * 100:+.2f}% годовых "
            f"(безрисковая 4%; остальной капитал в замере не работает)"
        )
        worst = sorted(rows, key=lambda r: r["ret"])[:6]
        print(
            "  худшие недели: "
            + "; ".join(
                f"{r['date']} {r['ret'] * 100:+.1f}% (ход {r['move'] * 100:+.1f}%)" for r in worst
            )
        )
        oct10 = [r for r in rows if r["date"] == date(2025, 10, 10)]
        if oct10:
            r = oct10[0]
            print(f"  неделя 10.10.2025: {r['ret'] * 100:+.2f}% (ход {r['move'] * 100:+.1f}%)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "measure"):
        p = sub.add_parser(name)
        p.add_argument("--currency", default="BTC")
        p.add_argument("--cache", default="data/options")
    f = sub.choices["fetch"]
    f.add_argument("--start", default="2019-01-04")
    f.add_argument("--end", default=None)
    f.add_argument("--window-min", type=int, default=120)
    m = sub.choices["measure"]
    m.add_argument(
        "--tol", type=float, default=0.02, help="страйк около денег не дальше доли индекса"
    )
    m.add_argument("--until", default=None)
    args = ap.parse_args()
    return cmd_fetch(args) if args.cmd == "fetch" else cmd_measure(args)


if __name__ == "__main__":
    raise SystemExit(main())
