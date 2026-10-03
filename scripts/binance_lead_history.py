#!/usr/bin/env python
"""История сделок ОДНОГО лид-портфеля копитрейдинга Binance и её разбор.

`binance_copytrade.py` смотрит на лидерборд целиком. Этот скрипт — на одного трейдера:
Binance, в отличие от Bybit, отдаёт закрытые позиции лидера без входа в аккаунт
(открытые скрыты). По ним видно то, чего не видно в процентах профиля: сколько
длится сделка, какой у неё размер, какая худшая, на чём сделана прибыль.

    python scripts/binance_lead_history.py fetch   --portfolio 5064581361618552064 --out F.json
    python scripts/binance_lead_history.py summary F.json

Поля позиции: `opened`/`closed` — мс UTC; `avgCost`/`avgClosePrice` — цены;
`maxOpenInterest` — наибольший размер в монетах; `closingPnl` — прибыль в USDT
(после комиссий по расчёту Binance); `roi` — доля от маржи.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

BASE = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0",
    "clienttype": "web",
}
PAGE = 50


def fetch(portfolio: str, out: Path, pause: float) -> int:
    import httpx

    with httpx.Client(headers=HEADERS, timeout=20) as c:
        detail = c.get(f"{BASE}/detail", params={"portfolioId": portfolio}).json()["data"]
        rows: list[dict] = []
        page = 1
        while True:
            body = {"pageNumber": page, "pageSize": PAGE, "portfolioId": portfolio}
            data = c.post(f"{BASE}/position-history", json=body).json()["data"]
            rows += data["list"]
            if len(rows) >= data["total"] or not data["list"]:
                break
            page += 1
            time.sleep(pause)
    uniq = {r["positionId"]: r for r in rows}
    snap = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "portfolio_id": portfolio,
        "detail": detail,
        "total": data["total"],
        "positions": sorted(uniq.values(), key=lambda r: r["opened"]),
    }
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved {len(uniq)} of {data['total']} positions -> {out}")
    return 0


def _dur(sec: float) -> str:
    for lim, label in ((1, "<1 s"), (60, "1 s-1 min"), (3600, "1 min-1 h"),
                       (86400, "1 h-1 d"), (7 * 86400, "1-7 d")):
        if sec < lim:
            return label
    return ">7 d"


DUR_ORDER = ["<1 s", "1 s-1 min", "1 min-1 h", "1 h-1 d", "1-7 d", ">7 d"]


def summary(path: Path) -> int:
    snap = json.loads(path.read_text(encoding="utf-8"))
    d, pos = snap["detail"], snap["positions"]
    for p in pos:
        p["pnl"] = float(p["closingPnl"])
        p["sec"] = (p["closed"] - p["opened"]) / 1000
        p["notional"] = float(p["avgCost"]) * float(p["maxOpenInterest"])
        p["ret"] = p["pnl"] / p["notional"] if p["notional"] else 0.0
        p["day"] = datetime.fromtimestamp(p["opened"] / 1000, timezone.utc)

    n = len(pos)
    wins = [p for p in pos if p["pnl"] > 0]
    total = sum(p["pnl"] for p in pos)
    gross_w = sum(p["pnl"] for p in wins)
    gross_l = -sum(p["pnl"] for p in pos if p["pnl"] <= 0)
    print(f"# {d['nickname']}  portfolio {snap['portfolio_id']}  (snapshot {snap['fetched_at']})")
    print(f"start {datetime.fromtimestamp(d['startTime'] / 1000, timezone.utc):%Y-%m-%d}, "
          f"margin {float(d['marginBalance']):.0f} USDT, AUM {float(d['aumAmount']):.0f}, "
          f"copiers {d['currentCopyCount']}/{d['maxCopyCount']} (all-time {d['totalCopyCount']})")
    print(f"copierPnl {float(d['copierPnl']):+.2f} USDT, leader profit share earned "
          f"(rebateFee) {float(d['rebateFee']):.2f}, sharpe(Binance) {d['sharpRatio']}")
    print(f"\npositions {n}, closed {sum(p['status'] == 'All Closed' for p in pos)}, "
          f"leverage {dict(Counter(p['leverage'] for p in pos))}, "
          f"sides {dict(Counter(p['side'] for p in pos))}")
    print(f"leader PnL {total:+.2f} USDT; win rate {len(wins) / n:.0%}; "
          f"profit factor {gross_w / gross_l if gross_l else float('inf'):.2f}")
    print(f"median win {median(p['pnl'] for p in wins):+.2f}, "
          f"median loss {median([p['pnl'] for p in pos if p['pnl'] <= 0] or [0]):+.2f}")
    rets = sorted(p["ret"] for p in pos)
    print(f"return per trade (pnl/notional): median {median(rets):+.2%}, "
          f"worst {rets[0]:+.2%}, best {rets[-1]:+.2%}")
    print(f"notional per trade: median {median(p['notional'] for p in pos):.0f}, "
          f"max {max(p['notional'] for p in pos):.0f} USDT")

    print("\n## by duration")
    print(f"{'bucket':<10} {'n':>4} {'win':>5} {'pnl':>9} {'med ret':>8}")
    by = defaultdict(list)
    for p in pos:
        by[_dur(p["sec"])].append(p)
    for k in DUR_ORDER:
        g = by.get(k)
        if g:
            print(f"{k:<10} {len(g):>4} {sum(x['pnl'] > 0 for x in g) / len(g):>5.0%} "
                  f"{sum(x['pnl'] for x in g):>+9.2f} {median(x['ret'] for x in g):>+8.2%}")

    print("\n## by month (open time)")
    bm = defaultdict(list)
    for p in pos:
        bm[f"{p['day']:%Y-%m}"].append(p)
    for k in sorted(bm):
        g = bm[k]
        print(f"{k}  n {len(g):>3}  win {sum(x['pnl'] > 0 for x in g) / len(g):>4.0%}  "
              f"pnl {sum(x['pnl'] for x in g):>+8.2f}")

    print("\n## top symbols by |pnl|")
    bs = defaultdict(list)
    for p in pos:
        bs[p["symbol"]].append(p)
    top = sorted(bs.items(), key=lambda kv: -abs(sum(x["pnl"] for x in kv[1])))[:15]
    for s, g in top:
        print(f"{s:<16} n {len(g):>3}  pnl {sum(x['pnl'] for x in g):>+8.2f}  "
              f"med dur {median(x['sec'] for x in g) / 3600:>7.2f} h")
    print(f"symbols total {len(bs)}")

    print("\n## worst 8 trades")
    for p in sorted(pos, key=lambda x: x["pnl"])[:8]:
        print(f"{p['day']:%Y-%m-%d %H:%M} {p['symbol']:<14} {p['side']:<5} "
              f"pnl {p['pnl']:+8.2f}  ret {p['ret']:+7.2%}  dur {p['sec'] / 3600:8.2f} h  "
              f"notional {p['notional']:.0f}")
    print("\n## best 8 trades")
    for p in sorted(pos, key=lambda x: -x["pnl"])[:8]:
        print(f"{p['day']:%Y-%m-%d %H:%M} {p['symbol']:<14} {p['side']:<5} "
              f"pnl {p['pnl']:+8.2f}  ret {p['ret']:+7.2%}  dur {p['sec']:9.1f} s  "
              f"notional {p['notional']:.0f}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--portfolio", required=True)
    f.add_argument("--out", type=Path, required=True)
    f.add_argument("--pause", type=float, default=0.5)
    s = sub.add_parser("summary")
    s.add_argument("path", type=Path)
    a = ap.parse_args()
    if a.cmd == "fetch":
        return fetch(a.portfolio, a.out, a.pause)
    return summary(a.path)


if __name__ == "__main__":
    raise SystemExit(main())
