"""Reconcile CT-scanner executions and test a declared necessary entry condition.

No profitability calculations or parameter search. --fetch archives public candles.
"""

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

PID = "5030491098292789505"
PREFIX = f"ctscanner_{PID}"
MINUTE = 60_000


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def iso(t):
    return datetime.fromtimestamp(t / 1000, UTC).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    root = args.root
    paths = [
        root / f"{PREFIX}{s}"
        for s in ("_scan_2026-10-25.json", "_2026-11-07.json", "_orders_2026-11-07.json")
    ]
    old, new, order_data = map(read, paths)
    old_rows = {r["positionId"]: r for r in old["rows"]}
    new_rows = {r["positionId"]: r for r in new["positions"]}
    changes = [k for k in old_rows.keys() & new_rows.keys() if old_rows[k] != new_rows[k]]
    rows = sorted((old_rows | new_rows).values(), key=lambda r: r["opened"])
    orders = order_data["orders"]
    candle_dir = root / "ctscanner_minutes_2026-11-07"
    candle_dir.mkdir(exist_ok=True)
    short = [r for r in rows if r["closed"] and r["closed"] - r["opened"] <= 5 * MINUTE]
    if args.fetch:
        import httpx

        with httpx.Client(timeout=30) as client:
            for r in short:
                path = candle_dir / f"{r['positionId']}.json"
                if path.exists() and read(path).get("error") is None:
                    continue
                minute = r["opened"] // MINUTE * MINUTE
                params = dict(
                    symbol=r["symbol"],
                    interval="1m",
                    startTime=minute - 2 * MINUTE,
                    endTime=minute + 5 * MINUTE - 1,
                    limit=10,
                )
                record = dict(params=params, fetched_at=datetime.now(UTC).isoformat())
                try:
                    response = client.get("https://fapi.binance.com/fapi/v1/klines", params=params)
                    response.raise_for_status()
                    batch = response.json()
                    if not isinstance(batch, list):
                        raise ValueError("Non-list candle response")
                    record.update(rows=batch, error=None)
                except (httpx.HTTPError, ValueError) as exc:
                    record.update(rows=[], error=str(exc))
                path.write_text(json.dumps(record), encoding="utf-8")
                time.sleep(0.2)
    reconciled, missing, mismatches, used = [], [], [], Counter()
    for r in rows:
        if not r["closed"]:
            continue
        for opening in (True, False):
            side = "BUY" if (r["side"] == "Long") == opening else "SELL"
            # A position interval includes all its partial fills; positionSide separates hedges.
            fills = [
                (i, o)
                for i, o in enumerate(orders)
                if o["symbol"] == r["symbol"]
                and o["side"] == side
                and o["positionSide"] == r["side"].upper()
                and r["opened"] - 1000 <= o["orderUpdateTime"] <= r["closed"] + 1000
            ]
            label = dict(position_id=r["positionId"], leg="open" if opening else "close")
            if not fills:
                missing.append(label)
                continue
            qty = sum(float(o["executedQty"]) for _, o in fills)
            price = sum(float(o["executedQty"]) * float(o["avgPrice"]) for _, o in fills) / qty
            expected_qty = float(r["maxOpenInterest"] if opening else r["closedVolume"])
            expected_price = float(r["avgCost"] if opening else r["avgClosePrice"])
            price_error = abs(price / expected_price - 1)
            qty_error = abs(qty - expected_qty)
            edge = (
                min(o["orderUpdateTime"] for _, o in fills)
                if opening
                else max(o["orderUpdateTime"] for _, o in fills)
            )
            time_error = abs(edge - r["opened" if opening else "closed"])
            item = dict(
                **label,
                fills=len(fills),
                qty_error=qty_error,
                price_relative_error=price_error,
                time_error_ms=time_error,
            )
            reconciled.append(item)
            used.update(i for i, _ in fills)
            if (
                qty_error > max(1e-8, expected_qty * 1e-7)
                or price_error > 1e-7
                or time_error > 2000
            ):
                mismatches.append(item)
    days = defaultdict(list)
    for r in rows:
        days[iso(r["opened"])[:10]].append(r)
    day_stats = {}
    for day, group in sorted(days.items()):
        durations = [(r["closed"] - r["opened"]) / 1000 for r in group if r["closed"]]
        day_stats[day] = dict(
            n=len(group),
            sides=dict(Counter(r["side"] for r in group)),
            symbols=dict(Counter(r["symbol"] for r in group)),
            duration_min=min(durations),
            duration_median=median(durations),
            duration_max=max(durations),
            timer60_2s=sum(abs(t - 60) <= 2 for t in durations),
            timer180_2s=sum(abs(t - 180) <= 2 for t in durations),
            quarter_hour_15s=sum(r["opened"] % (15 * MINUTE) <= 15_000 for r in group),
            notional_min=min(float(r["avgCost"]) * float(r["maxOpenInterest"]) for r in group),
            notional_max=max(float(r["avgCost"]) * float(r["maxOpenInterest"]) for r in group),
        )
    diagnostics, candle_sources, gaps = [], {}, []
    for r in short:
        path = candle_dir / f"{r['positionId']}.json"
        if not path.exists():
            gaps.append(r["positionId"])
            continue
        candle_sources[path.name] = digest(path)
        record = read(path)
        expected = r["opened"] // MINUTE * MINUTE - MINUTE
        bars = [b for b in record["rows"] if b[0] == expected]
        if record["error"] or len(bars) != 1 or bars[0][6] >= r["opened"]:
            gaps.append(r["positionId"])
            continue
        move = (float(r["avgCost"]) / float(bars[0][4]) - 1) * 100
        adverse = -move if r["side"] == "Long" else move
        diagnostics.append(
            dict(
                position_id=r["positionId"],
                day=iso(r["opened"])[:10],
                side=r["side"],
                signed_move_pct=move,
                event_move_pct=adverse,
                event_2pct=adverse >= 2,
                event_5pct=adverse >= 5,
            )
        )
    event_days = {}
    for day in sorted({d["day"] for d in diagnostics}):
        group = [d for d in diagnostics if d["day"] == day]
        event_days[day] = dict(
            n=len(group),
            event_2pct=sum(d["event_2pct"] for d in group),
            event_5pct=sum(d["event_5pct"] for d in group),
        )
    report = dict(
        portfolio_id=PID,
        script_sha256=digest(Path(__file__)),
        diagnostic_thresholds_pct=[2, 5],
        timer_tolerance_seconds=2,
        status="rule_unidentified",
        profitability_tested=False,
        sources={p.name: digest(p) for p in paths},
        old_count=len(old_rows),
        new_count=len(new_rows),
        reported_new_total=new["total"],
        union_count=len(rows),
        changed_shared_positions=changes,
        old_only=sorted(old_rows.keys() - new_rows.keys()),
        order_count=len(orders),
        reported_order_total=order_data["total"],
        order_types=dict(Counter(o["type"] for o in orders)),
        reconciled_legs=reconciled,
        missing_legs=missing,
        mismatches=mismatches,
        reused_orders=[i for i, n in used.items() if n > 1],
        unused_orders=[i for i in range(len(orders)) if i not in used],
        days=day_stats,
        short_count=len(short),
        candle_sources=candle_sources,
        candle_gaps=gaps,
        event_days=event_days,
        event_diagnostics=diagnostics,
    )
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "old_count",
                    "new_count",
                    "union_count",
                    "changed_shared_positions",
                    "order_count",
                    "mismatches",
                    "reused_orders",
                    "unused_orders",
                    "short_count",
                    "candle_gaps",
                    "event_days",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
