"""Offline execution reconstruction of portfolio #26; no profitability test."""

import argparse
import bisect
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

PID = "5220829154176782593"
HOUR = 3_600_000


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso(t):
    return datetime.fromtimestamp(t / 1000, UTC).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--universe", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"shenming_{PID}{suffix}"
        for suffix in ("_scan_2026-10-25.json", "_2026-11-07.json", "_orders_2026-11-07.json")
    ]
    old, new, order_snap = map(read, paths)
    old_map = {r["positionId"]: r for r in old["rows"]}
    new_map = {r["positionId"]: r for r in new["positions"]}
    rows = sorted((old_map | new_map).values(), key=lambda r: r["opened"])
    orders = order_snap["orders"]
    legs = defaultdict(list)
    unmatched, ambiguous = [], []
    for i, o in enumerate(orders):
        candidates = [
            r
            for r in rows
            if r["symbol"] == o["symbol"]
            and r["side"].upper() == o["positionSide"]
            and r["closed"] is not None
            and r["opened"] - 2 <= o["orderUpdateTime"] <= r["closed"] + 2
        ]
        if len(candidates) != 1:
            (unmatched if not candidates else ambiguous).append(i)
            continue
        r = candidates[0]
        opening = (o["side"] == "BUY") == (r["side"] == "Long")
        legs[(r["positionId"], "open" if opening else "close")].append((i, o))
    matches, missing, errors, inventory = [], [], [], []
    for r in rows:
        for leg, field, price_field in (
            ("open", "opened", "avgCost"),
            ("close", "closed", "avgClosePrice"),
        ):
            fills = legs.get((r["positionId"], leg), [])
            label = dict(position_id=r["positionId"], leg=leg)
            if not fills:
                missing.append(label)
                continue
            qty = sum(float(o["executedQty"]) for _, o in fills)
            price = sum(float(o["executedQty"]) * float(o["avgPrice"]) for _, o in fills) / qty
            # Total entry volume equals total close volume for a fully closed position;
            # maxOpenInterest is peak inventory, not necessarily cumulative turnover.
            qty_error = abs(qty - float(r["closedVolume"]))
            price_error = abs(price / float(r[price_field]) - 1)
            t = (min if leg == "open" else max)(o["orderUpdateTime"] for _, o in fills)
            item = dict(
                **label,
                fills=len(fills),
                qty=qty,
                qty_error=qty_error,
                price_relative_error=price_error,
                price_absolute_error=abs(price - float(r[price_field])),
                time_error_ms=abs(t - r[field]),
                placed_first_ms=min(o["orderTime"] for _, o in fills),
                completed_first_ms=min(o["orderUpdateTime"] for _, o in fills),
                completed_last_ms=max(o["orderUpdateTime"] for _, o in fills),
                types=dict(Counter(o["type"] for _, o in fills)),
            )
            matches.append(item)
            if qty_error > max(1e-8, float(r["closedVolume"]) * 1e-7) or price_error > 1e-7:
                errors.append(item)
        all_fills = sorted(
            [
                (o["orderUpdateTime"], i, float(o["executedQty"]))
                for i, o in legs.get((r["positionId"], "open"), [])
            ]
            + [
                (o["orderUpdateTime"], i, -float(o["executedQty"]))
                for i, o in legs.get((r["positionId"], "close"), [])
            ]
        )
        balance = peak = 0.0
        for _, _, delta in all_fills:
            balance += delta
            peak = max(peak, balance)
        if all_fills:
            inventory.append(
                dict(
                    position_id=r["positionId"],
                    residual=balance,
                    peak=peak,
                    peak_error=abs(peak - float(r["maxOpenInterest"])),
                )
            )
    events = []
    for r in rows:
        events.append((r["opened"], 1))
        if r["closed"]:
            events.append((r["closed"], -1))
    active = peak_active = 0
    for _, change in sorted(events):
        active += change
        peak_active = max(peak_active, active)
    months = {}
    for month in sorted({iso(r["opened"])[:7] for r in rows}):
        group = [r for r in rows if iso(r["opened"])[:7] == month]
        closed = [r for r in group if r["closed"]]
        durations = [(r["closed"] - r["opened"]) / HOUR for r in closed]
        months[month] = dict(
            n=len(group),
            sides=dict(Counter(r["side"] for r in group)),
            leverage=dict(Counter(r["leverage"] for r in group)),
            symbols=len({r["symbol"] for r in group}),
            opened_first3min=sum(r["opened"] % HOUR < 180_000 for r in group),
            closed_first3min=sum(r["closed"] % HOUR < 180_000 for r in closed),
            one_hour_30s=sum(abs(t - 1) <= 30 / 3600 for t in durations),
            integer_hour_30s=sum(
                round(t) >= 1 and abs(t - round(t)) <= 30 / 3600 for t in durations
            ),
            duration_min_hours=min(durations),
            duration_median_hours=median(durations),
            duration_max_hours=max(durations),
            median_peak_notional=median(
                float(r["maxOpenInterest"]) * float(r["avgCost"]) for r in group
            ),
        )
    selected = [
        r
        for r in rows
        if iso(r["opened"])[:7] == "2026-10"
        and r["side"] == "Long"
        and r["opened"] % HOUR < 180_000
    ]
    rank_rows, rank_missing, source_hashes = [], [], {}
    if args.universe:
        metadata = read(args.universe / "symbols.json")
        bars = {}
        for path in sorted(args.universe.glob("*.json")):
            source_hashes[path.name] = sha(path)
            if path.stem not in metadata:
                continue
            record = read(path)
            if not record["complete"]:
                continue
            bars[path.stem] = {b[0]: float(b[4]) for b in record["rows"]}
        frames = {}
        for t in sorted({r["opened"] // HOUR * HOUR for r in selected}):
            values = {}
            for symbol, series in bars.items():
                now, before = series.get(t - HOUR), series.get(t - 25 * HOUR)
                # Require a continuous 25-point close window for the declared 24h change.
                if now and before and all(t - k * HOUR in series for k in range(1, 26)):
                    if metadata[symbol]["onboard"] < t:
                        values[symbol] = now / before - 1
            frames[t] = values
        for r in selected:
            frame = frames[r["opened"] // HOUR * HOUR]
            if r["symbol"] not in frame:
                rank_missing.append(r["positionId"])
                continue
            value = frame[r["symbol"]]
            ordered = sorted(frame.values())
            better = len(ordered) - bisect.bisect_right(ordered, value)
            rank_rows.append(
                dict(
                    position_id=r["positionId"],
                    symbol=r["symbol"],
                    opened=iso(r["opened"]),
                    universe_n=len(frame),
                    return24h=value,
                    strictly_better=better,
                    top10=better < 10,
                )
            )
    report = dict(
        portfolio_id=PID,
        status="rule_unidentified",
        profitability_tested=False,
        script_sha256=sha(Path(__file__)),
        sources={p.name: sha(p) for p in paths},
        old_count=len(old_map),
        new_count=len(new_map),
        reported_new_total=new["total"],
        union_count=len(rows),
        old_missing_new=sorted(old_map.keys() - new_map.keys()),
        changed_shared=[k for k in old_map.keys() & new_map.keys() if old_map[k] != new_map[k]],
        first_open=iso(rows[0]["opened"]),
        last_open=iso(rows[-1]["opened"]),
        order_count=len(orders),
        reported_order_total=order_snap["total"],
        order_types=dict(Counter(o["type"] for o in orders)),
        peak_simultaneous=peak_active,
        legs=matches,
        missing_legs=missing,
        reconciliation_errors=errors,
        unmatched_orders=unmatched,
        ambiguous_orders=ambiguous,
        inventory=inventory,
        months=months,
        rank_selected_count=len(selected),
        rank_missing=rank_missing,
        rank_covered=len(rank_rows),
        rank_top10=sum(r["top10"] for r in rank_rows),
        rank_entries=rank_rows,
        universe_sources=source_hashes,
        universe_manifest=read(args.universe / "manifest.json") if args.universe else None,
        rank_orders_before_boundary=[
            r["positionId"]
            for r in selected
            if min(o["orderTime"] for _, o in legs[(r["positionId"], "open")])
            < r["opened"] // HOUR * HOUR
        ],
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
                    "order_count",
                    "order_types",
                    "peak_simultaneous",
                    "unmatched_orders",
                    "ambiguous_orders",
                    "months",
                    "rank_selected_count",
                    "rank_covered",
                    "rank_top10",
                    "rank_missing",
                )
            }
        )
    )
    print("matched legs", len(matches), "missing legs", len(missing), "errors", len(errors))


if __name__ == "__main__":
    main()
