"""Offline #3 audit: execution reconciliation and descriptive regime timing."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from open_automata import features

PID = "5226612818704717824"
FIVE_MIN = 300_000
FIVE_HOURS = 18_000_000


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso(t):
    return datetime.fromtimestamp(t / 1000, UTC).isoformat()


def timing(rows):
    durations = [(r["closed"] - r["opened"]) / 1000 for r in rows]
    anchors = [r["closed"] - (r["opened"] // FIVE_MIN * FIVE_MIN + FIVE_HOURS) for r in rows]
    running = peak = 0
    for _, delta in sorted([(r["opened"], 1) for r in rows] + [(r["closed"], -1) for r in rows]):
        running += delta
        peak = max(peak, running)
    return dict(
        n=len(rows),
        first_open=iso(min(r["opened"] for r in rows)),
        last_close=iso(max(r["closed"] for r in rows)),
        sides=dict(Counter(r["side"] for r in rows)),
        leverage=dict(Counter(r["leverage"] for r in rows)),
        margin=dict(Counter(r["isolated"] for r in rows)),
        symbols=len({r["symbol"] for r in rows}),
        peak_overlap=peak,
        duration_min_s=min(durations),
        duration_median_s=median(durations),
        duration_max_s=max(durations),
        opening_5min_offset_min_ms=min(r["opened"] % FIVE_MIN for r in rows),
        opening_5min_offset_max_ms=max(r["opened"] % FIVE_MIN for r in rows),
        closing_anchor_offset_min_ms=min(anchors),
        closing_anchor_offset_max_ms=max(anchors),
        # These are descriptive post-inspection bounds, not preregistered tests.
        close_after_5h_anchor_within20s=sum(0 <= x <= 20_000 for x in anchors),
        features={k: v for k, v in features(rows).items() if k.startswith("timer")},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"fanjiang_{PID}{suffix}.json"
        for suffix in (
            "_scan_2026-10-25",
            "_2026-11-07",
            "_orders_rawpass_2026-11-07",
        )
    ]
    old, new, od = map(read, paths)
    om = {r["positionId"]: r for r in old["rows"]}
    nm = {r["positionId"]: r for r in new["positions"]}
    rows = sorted((om | nm).values(), key=lambda r: r["opened"])
    assert all(r["closed"] and r["status"] == "All Closed" for r in rows)
    orders = od["orders"]
    assigned = defaultdict(list)
    unmatched, ambiguous = [], []
    for i, o in enumerate(orders):
        matches = [
            r
            for r in rows
            if r["symbol"] == o["symbol"]
            and r["side"].upper() == o["positionSide"]
            and r["opened"] - 10 <= o["orderUpdateTime"] <= r["closed"] + 10
        ]
        if len(matches) == 1:
            assigned[matches[0]["positionId"]].append(i)
        else:
            (ambiguous if matches else unmatched).append(i)
    positions, legs, missing = [], [], []
    for r in rows:
        entry = "BUY" if r["side"] == "Long" else "SELL"
        counts = {}
        fills = sorted(
            [orders[i] for i in assigned[r["positionId"]]],
            key=lambda o: o["orderUpdateTime"],
        )
        inventory = peak = minimum = Decimal(0)
        for fill in fills:
            inventory += Decimal(str(fill["executedQty"])) * (1 if fill["side"] == entry else -1)
            peak = max(peak, inventory)
            minimum = min(minimum, inventory)
        for side, field in (
            (entry, "avgCost"),
            ("SELL" if entry == "BUY" else "BUY", "avgClosePrice"),
        ):
            indices = [i for i in assigned[r["positionId"]] if orders[i]["side"] == side]
            counts["entry" if side == entry else "exit"] = len(indices)
            if not indices:
                missing.append(dict(position_id=r["positionId"], side=side))
                continue
            selected = [orders[i] for i in indices]
            qty = sum(Decimal(str(o["executedQty"])) for o in selected)
            avg = (
                sum(Decimal(str(o["executedQty"])) * Decimal(str(o["avgPrice"])) for o in selected)
                / qty
            )
            precision = sum(
                Decimal(str(o["executedQty"]))
                * Decimal(10) ** Decimal(str(o["avgPrice"])).as_tuple().exponent
                for o in selected
            ) / qty + Decimal("1e-8")
            legs.append(
                dict(
                    position_id=r["positionId"],
                    entry=side == entry,
                    side=side,
                    order_indices=indices,
                    qty=str(qty),
                    qty_error=str(qty - Decimal(str(r["closedVolume"]))),
                    price_error=str(abs(avg - Decimal(str(r[field])))),
                    price_precision_compatible=abs(avg - Decimal(str(r[field]))) <= precision,
                    types=dict(Counter(o["type"] for o in selected)),
                )
            )
        positions.append(
            dict(
                position_id=r["positionId"],
                symbol=r["symbol"],
                side=r["side"],
                opened=iso(r["opened"]),
                closed=iso(r["closed"]),
                opening_offset_ms=r["opened"] % FIVE_MIN,
                closing_anchor_offset_ms=r["closed"]
                - (r["opened"] // FIVE_MIN * FIVE_MIN + FIVE_HOURS),
                max_open_interest=str(r["maxOpenInterest"]),
                closed_volume=str(r["closedVolume"]),
                order_counts=counts,
                inventory_final=str(inventory),
                inventory_min=str(minimum),
                inventory_peak=str(peak),
                peak_error=str(peak - Decimal(str(r["maxOpenInterest"]))),
                execution_timestamp_ties=len(fills) - len({o["orderUpdateTime"] for o in fills}),
            )
        )
    early = [r for r in rows if r["leverage"] == "1"]
    sol = [r for r in rows if r["symbol"] == "SOLUSDT" and r["leverage"] == "10"]
    later = [r for r in rows if r not in early and r not in sol]
    limits = [
        dict(
            order_index=i,
            symbol=o["symbol"],
            side=o["side"],
            position_side=o["positionSide"],
            placed=iso(o["orderTime"]),
            updated=iso(o["orderUpdateTime"]),
            placement_to_update_ms=o["orderUpdateTime"] - o["orderTime"],
        )
        for i, o in enumerate(orders)
        if o["type"] == "LIMIT"
    ]
    raw_paths = [
        p
        for folder in ("fanjiang_pages_2026-11-07", "fanjiang_order_pages_2026-11-07")
        for p in sorted((args.root / folder).glob("*.json"))
    ]
    report = dict(
        portfolio_id=PID,
        status="rule_unidentified",
        profitability_tested=False,
        profile_description=new["detail"].get("description"),
        script_sha256=sha(Path(__file__)),
        feature_script_sha256=sha(Path(__file__).with_name("open_automata.py")),
        sources={p.name: sha(p) for p in paths},
        raw_sources={str(p.relative_to(args.root)): sha(p) for p in raw_paths},
        old_count=len(om),
        new_count=len(nm),
        current_total=new["total"],
        current_complete=new["complete"],
        union_count=len(rows),
        old_missing_current=sorted(om.keys() - nm.keys()),
        changed_shared=[k for k in om.keys() & nm.keys() if om[k] != nm[k]],
        scan_timing=timing(old["rows"]),
        all_timing=timing(rows),
        regimes={
            name: timing(group)
            for name, group in (
                ("early_long_1x", early),
                ("sol_10x", sol),
                ("later_mixed", later),
            )
        },
        order_count=len(orders),
        reported_order_total=od["total"],
        order_observed_totals=od["observed_totals"],
        order_raw_count=od["raw_count"],
        order_duplicates=od["duplicates"],
        assignment_tolerance_ms=10,
        first_order=iso(min(o["orderTime"] for o in orders)),
        last_order=iso(max(o["orderTime"] for o in orders)),
        order_types=dict(
            Counter(o["type"] + ":" + o["positionSide"] + ":" + o["side"] for o in orders)
        ),
        unmatched_order_indices=unmatched,
        ambiguous_order_indices=ambiguous,
        missing_legs=missing,
        legs=legs,
        positions=positions,
        limit_orders=limits,
    )
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k
                not in (
                    "legs",
                    "positions",
                    "limit_orders",
                    "raw_sources",
                    "sources",
                )
            },
            ensure_ascii=True,
        )
    )
    print(
        "legs",
        len(legs),
        "qty errors",
        sum(Decimal(x["qty_error"]) != 0 for x in legs),
        "price flags",
        sum(not x["price_precision_compatible"] for x in legs),
    )


if __name__ == "__main__":
    main()
