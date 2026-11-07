"""Audit #4 batch timing and reconcile available executions; no price/P&L search."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from open_automata import features

PID = "5127540263600794368"
DAY = 86_400_000


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso(t):
    return datetime.fromtimestamp(t / 1000, UTC).isoformat()


def timer(rows):
    return {k: v for k, v in features(rows).items() if k.startswith("timer")}


def pair(r):
    return r["opened"] // DAY, r["closed"] // DAY, r["side"]


def describe(rows):
    return dict(
        n=len(rows),
        symbols=len({r["symbol"] for r in rows}),
        first_open=iso(min(r["opened"] for r in rows)),
        last_open=iso(max(r["opened"] for r in rows)),
        first_close=iso(min(r["closed"] for r in rows)),
        last_close=iso(max(r["closed"] for r in rows)),
        open_span_ms=max(r["opened"] for r in rows) - min(r["opened"] for r in rows),
        close_span_ms=max(r["closed"] for r in rows) - min(r["closed"] for r in rows),
        sides=dict(Counter(r["side"] for r in rows)),
        leverage=dict(Counter(r["leverage"] for r in rows)),
        timer=timer(rows),
    )


def order_key(o):
    return tuple(
        o[k]
        for k in (
            "symbol",
            "positionSide",
            "side",
            "type",
            "orderTime",
            "orderUpdateTime",
            "executedQty",
            "avgPrice",
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    root = args.root
    old_path = root / f"hester_{PID}_scan_2026-10-25.json"
    old = read(old_path)
    om = {r["positionId"]: r for r in old["rows"]}
    nm, totals, changed, passes, source_paths = {}, [], [], {}, [old_path]
    for folder in sorted(root.glob("hester_pages*_2026-11-07")):
        local, raw_count = {}, 0
        for path in sorted(folder.glob("page_*.json")):
            source_paths.append(path)
            data = read(path)["response"]["data"]
            totals.append(data["total"])
            raw_count += len(data["list"])
            for r in data["list"]:
                key = r["positionId"]
                if key in nm and nm[key] != r:
                    changed.append(dict(position_id=key, source=str(path)))
                nm[key] = local[key] = r
        passes[folder.name] = dict(raw_count=raw_count, unique_count=len(local))
    rows = sorted((om | nm).values(), key=lambda r: r["opened"])
    assert all(r["closed"] and r["status"] == "All Closed" for r in rows)
    groups = defaultdict(list)
    for r in rows:
        groups[pair(r)].append(r)
    scan_pair = Counter(pair(r) for r in old["rows"]).most_common(1)[0][0]
    scan_episode = [r for r in old["rows"] if pair(r) == scan_pair]
    scan_rest = [r for r in old["rows"] if pair(r) != scan_pair]
    unique_episode = [r for r in om.values() if pair(r) == scan_pair]
    orders, order_passes = {}, {}
    for path in sorted(root.glob(f"hester_{PID}_orders_*.json")):
        source_paths.append(path)
        d = read(path)
        order_passes[path.name] = {
            k: d[k]
            for k in (
                "total",
                "observed_totals",
                "raw_count",
                "unique_count",
                "duplicates",
            )
        }
        orders.update({order_key(o): o for o in d["orders"]})
    orders = sorted(orders.values(), key=lambda o: o["orderUpdateTime"])
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
    legs, missing, inventory = [], [], []
    for r in rows:
        if r["positionId"] not in assigned:
            continue
        indices = assigned[r["positionId"]]
        entry = "BUY" if r["side"] == "Long" else "SELL"
        balance = peak = minimum = Decimal(0)
        for i in indices:
            o = orders[i]
            balance += Decimal(str(o["executedQty"])) * (1 if o["side"] == entry else -1)
            peak, minimum = max(peak, balance), min(minimum, balance)
        inventory.append(
            dict(
                position_id=r["positionId"],
                both_sides_present={orders[i]["side"] for i in indices} == {"BUY", "SELL"},
                final=str(balance),
                minimum=str(minimum),
                peak=str(peak),
                peak_error=str(peak - Decimal(str(r["maxOpenInterest"]))),
                timestamp_ties=len(indices) - len({orders[i]["orderUpdateTime"] for i in indices}),
            )
        )
        for side, field in (
            (entry, "avgCost"),
            ("SELL" if entry == "BUY" else "BUY", "avgClosePrice"),
        ):
            selected = [i for i in indices if orders[i]["side"] == side]
            if not selected:
                missing.append(dict(position_id=r["positionId"], side=side))
                continue
            qty = sum(Decimal(str(orders[i]["executedQty"])) for i in selected)
            average = (
                sum(
                    Decimal(str(orders[i]["executedQty"])) * Decimal(str(orders[i]["avgPrice"]))
                    for i in selected
                )
                / qty
            )
            tolerance = sum(
                Decimal(str(orders[i]["executedQty"]))
                * Decimal(10) ** Decimal(str(orders[i]["avgPrice"])).as_tuple().exponent
                for i in selected
            ) / qty + Decimal("1e-8")
            legs.append(
                dict(
                    position_id=r["positionId"],
                    side=side,
                    entry=side == entry,
                    order_indices=selected,
                    qty=str(qty),
                    qty_error=str(qty - Decimal(str(r["closedVolume"]))),
                    price_error=str(abs(average - Decimal(str(r[field])))),
                    price_precision_compatible=abs(average - Decimal(str(r[field]))) <= tolerance,
                )
            )
    running = peak = 0
    for _, delta in sorted([(r["opened"], 1) for r in rows] + [(r["closed"], -1) for r in rows]):
        running += delta
        peak = max(peak, running)
    detail_path = root / "hester_pages_2026-11-07/detail.json"
    source_paths.append(detail_path)
    for folder in sorted(root.glob("hester_order_*2026-11-07")):
        source_paths.extend(sorted(folder.glob("*.json")))
    report = dict(
        portfolio_id=PID,
        status="rule_unidentified",
        profitability_tested=False,
        script_sha256=sha(Path(__file__)),
        feature_script_sha256=sha(Path(__file__).with_name("open_automata.py")),
        sources={str(p.relative_to(root)): sha(p) for p in source_paths},
        profile_description=read(detail_path)["data"].get("description"),
        position_passes=passes,
        observed_position_totals=sorted(set(totals)),
        current_count=len(nm),
        current_complete=len(set(totals)) == 1 and len(nm) == totals[0],
        old_count=len(om),
        old_raw_count=len(old["rows"]),
        old_duplicate_count=len(old["rows"]) - len(om),
        union_count=len(rows),
        changed_between_pages=changed,
        old_missing_current=sorted(om.keys() - nm.keys()),
        changed_shared=[k for k in om.keys() & nm.keys() if om[k] != nm[k]],
        all_timing=describe(rows),
        peak_closed_position_overlap=peak,
        scan_timing=describe(old["rows"]),
        scan_unique_timing=describe(list(om.values())),
        scan_episode=describe(scan_episode),
        scan_episode_unique=describe(unique_episode),
        scan_without_episode=describe(scan_rest),
        day_pair_groups=[describe(g) for g in sorted(groups.values(), key=len, reverse=True)],
        order_passes=order_passes,
        order_count=len(orders),
        first_order=iso(min(o["orderTime"] for o in orders)),
        last_order=iso(max(o["orderTime"] for o in orders)),
        order_types=dict(
            Counter(o["type"] + ":" + o["positionSide"] + ":" + o["side"] for o in orders)
        ),
        assignment_tolerance_ms=10,
        covered_positions=len(assigned),
        uncovered_positions=len(rows) - len(assigned),
        unmatched_order_indices=unmatched,
        ambiguous_order_indices=ambiguous,
        missing_covered_legs=missing,
        orders=orders,
        legs=legs,
        inventory=inventory,
    )
    merged = dict(
        portfolio_id=PID,
        total=totals[0],
        complete=report["current_complete"],
        positions=sorted(nm.values(), key=lambda r: r["opened"]),
    )
    merged_path = root / f"hester_{PID}_merged_2026-11-07.json"
    merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    report["merged_positions_sha256"] = sha(merged_path)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k
                not in (
                    "sources",
                    "orders",
                    "legs",
                    "inventory",
                    "day_pair_groups",
                )
            },
            ensure_ascii=True,
        )
    )
    print(
        "legs",
        len(legs),
        "qty mismatches",
        sum(Decimal(x["qty_error"]) != 0 for x in legs),
        "price flags",
        sum(not x["price_precision_compatible"] for x in legs),
    )


if __name__ == "__main__":
    main()
