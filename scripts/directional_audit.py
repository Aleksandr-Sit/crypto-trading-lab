"""Audit #39's common closing timestamps and conditional timer signature offline."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from open_automata import features

PID = "5095694990932973056"


def read(p):
    return json.loads(p.read_text(encoding="utf-8"))


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def iso(t):
    return datetime.fromtimestamp(t / 1000, UTC).isoformat() if t is not None else None


def signature(rows):
    f = features(rows)
    return {
        k: f[k]
        for k in (
            "n",
            "timer_loss_share",
            "timer_loss_mode_s",
            "timer_all_share",
            "timer_all_mode_s",
        )
        if k in f
    }


def groups(rows):
    grouped = defaultdict(list)
    for r in rows:
        if r["closed"]:
            grouped[r["closed"] // 1000].append(r)
    return sorted(grouped.values(), key=lambda g: (-len(g), min(r["closed"] for r in g)))


def group_summary(g):
    return dict(
        n=len(g),
        unique_symbols=len({r["symbol"] for r in g}),
        sides=dict(Counter(r["side"] for r in g)),
        first_open=iso(min(r["opened"] for r in g)),
        last_open=iso(max(r["opened"] for r in g)),
        first_close=iso(min(r["closed"] for r in g)),
        last_close=iso(max(r["closed"] for r in g)),
        closing_span_ms=max(r["closed"] for r in g) - min(r["closed"] for r in g),
        holding_min_seconds=min((r["closed"] - r["opened"]) / 1000 for r in g),
        holding_max_seconds=max((r["closed"] - r["opened"]) / 1000 for r in g),
        position_ids=sorted(r["positionId"] for r in g),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    scan_path = args.root / f"directional_{PID}_scan_2026-10-25.json"
    orders_path = args.root / f"directional_{PID}_orders_2026-11-07.json"
    sources = {p.name: sha(p) for p in (scan_path, orders_path)}
    snapshots, counts, totals = {}, {}, []
    variants = defaultdict(set)
    for dirname in (
        "directional_pages_2026-11-07",
        "directional_pages200_2026-11-07",
        "directional_pages199_2026-11-07",
    ):
        raw_count, unique = 0, set()
        for p in sorted((args.root / dirname).glob("*.json")):
            sources[f"{dirname}/{p.name}"] = sha(p)
        for p in sorted((args.root / dirname).glob("page_*.json")):
            sources[f"{dirname}/{p.name}"] = sha(p)
            record = read(p)["response"]["data"]
            totals.append(record["total"])
            raw_count += len(record["list"])
            for r in record["list"]:
                pid = r["positionId"]
                unique.add(pid)
                variants[pid].add(json.dumps(r, sort_keys=True))
                snapshots[pid] = r
        counts[dirname] = dict(raw_count=raw_count, unique_count=len(unique))
    old = read(scan_path)["rows"]
    old_map = {r["positionId"]: r for r in old}
    combined = old_map | snapshots
    rows = list(combined.values())
    merged_path = args.root / f"directional_{PID}_merged_2026-11-07.json"
    merged = dict(
        portfolio_id=PID,
        reported_totals=sorted(set(totals)),
        sources=sources,
        complete=bool(totals) and len(set(totals)) == 1 and len(snapshots) == totals[0],
        positions=sorted(snapshots.values(), key=lambda r: r["opened"]),
    )
    merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    scan_groups, all_groups = groups(old), groups(rows)
    largest_scan = {r["positionId"] for r in scan_groups[0]}
    largest_all = {r["positionId"] for r in all_groups[0]}
    orders = read(orders_path)["orders"]
    assigned, unmatched, ambiguous = defaultdict(list), [], []
    for i, o in enumerate(orders):
        matches = [
            r
            for r in rows
            if r["symbol"] == o["symbol"]
            and r["side"].upper() == o["positionSide"]
            and r["opened"] - 2 <= o["orderUpdateTime"]
            and (r["closed"] is None or o["orderUpdateTime"] <= r["closed"] + 2)
        ]
        if len(matches) != 1:
            (unmatched if not matches else ambiguous).append(i)
            continue
        r = matches[0]
        assigned[r["positionId"]].append(o)
    reconciliation = []
    for pid, fills in assigned.items():
        r = combined[pid]
        legs = {}
        for opening in (True, False):
            selected = [
                o for o in fills if ((o["side"] == "BUY") == (r["side"] == "Long")) == opening
            ]
            qty = sum((Decimal(str(o["executedQty"])) for o in selected), Decimal(0))
            avg = (
                (
                    sum(
                        Decimal(str(o["executedQty"])) * Decimal(str(o["avgPrice"]))
                        for o in selected
                    )
                    / qty
                )
                if qty
                else None
            )
            expected = Decimal(str(r["avgCost"] if opening else r["avgClosePrice"]))
            legs["open" if opening else "close"] = dict(
                n=len(selected),
                qty=float(qty),
                price_absolute_error=float(abs(avg - expected)) if avg is not None else None,
            )
        reconciliation.append(
            dict(
                position_id=pid,
                symbol=r["symbol"],
                closed=r["closed"],
                status=r["status"],
                legs=legs,
                peak_qty=r["maxOpenInterest"],
                reported_closed_qty=r["closedVolume"],
            )
        )
    report = dict(
        portfolio_id=PID,
        status="rule_unidentified",
        profitability_tested=False,
        sources=sources,
        script_sha256=sha(Path(__file__)),
        merged_snapshot_sha256=sha(merged_path),
        fetch_script_sha256=sha(Path(__file__).with_name("fetch_directional_history.py")),
        feature_script_sha256=sha(Path(__file__).with_name("open_automata.py")),
        page_counts=counts,
        reported_totals=sorted(set(totals)),
        current_unique=len(snapshots),
        current_complete=bool(totals) and len(set(totals)) == 1 and len(snapshots) == totals[0],
        conflicting_page_rows=[pid for pid, v in variants.items() if len(v) > 1],
        conflicting_page_fields={
            pid: [
                key
                for key in json.loads(next(iter(v)))
                if len({str(json.loads(raw)[key]) for raw in v}) > 1
            ]
            for pid, v in variants.items()
            if len(v) > 1
        },
        scan_rows=len(old),
        scan_closed=sum(bool(r["closed"]) for r in old),
        scan_unique=len(old_map),
        scan_missing_current=sorted(old_map.keys() - snapshots.keys()),
        changed_shared_scan=[
            pid for pid in old_map.keys() & snapshots.keys() if old_map[pid] != snapshots[pid]
        ],
        union_unique=len(rows),
        union_closed=sum(bool(r["closed"]) for r in rows),
        scan_signature=signature(old),
        scan_without_largest_group=signature(
            [r for r in old if r["positionId"] not in largest_scan]
        ),
        union_signature=signature(rows),
        union_without_largest_group=signature(
            [r for r in rows if r["positionId"] not in largest_all]
        ),
        scan_largest_groups=[group_summary(g) for g in scan_groups[:5]],
        union_largest_groups=[group_summary(g) for g in all_groups[:5]],
        order_count=len(orders),
        reported_order_total=read(orders_path)["total"],
        order_types=dict(Counter(o["type"] for o in orders)),
        first_order=iso(min(o["orderTime"] for o in orders)),
        last_order=iso(max(o["orderTime"] for o in orders)),
        covered_positions=len(assigned),
        unmatched_orders=unmatched,
        ambiguous_orders=ambiguous,
        largest_scan_group_order_coverage=len(largest_scan & assigned.keys()),
        reconciliation=reconciliation,
    )
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "page_counts",
                    "reported_totals",
                    "current_unique",
                    "current_complete",
                    "conflicting_page_rows",
                    "union_unique",
                    "scan_closed",
                    "scan_signature",
                    "scan_without_largest_group",
                    "union_signature",
                    "union_without_largest_group",
                    "covered_positions",
                    "unmatched_orders",
                    "ambiguous_orders",
                )
            }
        )
    )
    for group in report["union_largest_groups"]:
        print({k: v for k, v in group.items() if k != "position_ids"})


if __name__ == "__main__":
    main()
