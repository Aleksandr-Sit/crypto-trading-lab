"""Reconcile #63 closed positions, additional entries and unmatched inventory."""

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from open_automata import features

PID = "5262575735183470080"


def read(p):
    return json.loads(p.read_text(encoding="utf-8-sig"))


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def iso(t):
    return datetime.fromtimestamp(t / 1000, UTC).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"angelcat_{PID}{suffix}"
        for suffix in ("_scan_2026-10-25.json", "_2026-11-07.json", "_orders_2026-11-07.json")
    ]
    old, new, od = map(read, paths)
    old_map = {r["positionId"]: r for r in old["rows"]}
    new_map = {r["positionId"]: r for r in new["positions"]}
    rows = sorted((old_map | new_map).values(), key=lambda r: r["opened"])
    orders = od["orders"]
    assigned, unmatched, ambiguous = defaultdict(list), [], []
    for i, o in enumerate(orders):
        matches = [
            r
            for r in rows
            if r["symbol"] == o["symbol"]
            and r["side"].upper() == o["positionSide"]
            and r["opened"] - 2 <= o["orderUpdateTime"] <= r["closed"] + 2
        ]
        if len(matches) != 1:
            (unmatched if not matches else ambiguous).append(i)
            continue
        assigned[matches[0]["positionId"]].append((i, o))
    reconciled, pairs, missing = [], [], []
    for r in rows:
        legs = {}
        fills = assigned[r["positionId"]]
        for side, field in (("BUY", "avgCost"), ("SELL", "avgClosePrice")):
            selected = [o for _, o in fills if o["side"] == side]
            if not selected:
                missing.append(dict(position_id=r["positionId"], side=side))
                continue
            qty = sum(Decimal(str(o["executedQty"])) for o in selected)
            average = (
                sum(Decimal(str(o["avgPrice"])) * Decimal(str(o["executedQty"])) for o in selected)
                / qty
            )
            # Compatibility with numeric display precision; API rounding method is unknown.
            precision = sum(
                Decimal(str(o["executedQty"]))
                * Decimal(10) ** Decimal(str(o["avgPrice"])).as_tuple().exponent
                for o in selected
            ) / qty + Decimal("1e-8")
            err = abs(average - Decimal(str(r[field])))
            legs[side] = dict(
                n=len(selected),
                qty=float(qty),
                qty_error=float(qty - Decimal(str(r["closedVolume"]))),
                price_absolute_error=float(err),
                display_precision_compatible=err <= precision,
                types=dict(Counter(o["type"] for o in selected)),
            )
        buys = sorted(
            [o for _, o in fills if o["side"] == "BUY"], key=lambda o: o["orderUpdateTime"]
        )
        for a, b in zip(buys, buys[1:], strict=False):
            pairs.append(
                dict(
                    position_id=r["positionId"],
                    gap_ms=b["orderUpdateTime"] - a["orderUpdateTime"],
                    lower_price=float(b["avgPrice"]) < float(a["avgPrice"]),
                    price_ratio=float(b["avgPrice"]) / float(a["avgPrice"]),
                    qty_ratio=float(b["executedQty"]) / float(a["executedQty"]),
                    notional_ratio=float(b["executedQty"])
                    * float(b["avgPrice"])
                    / (float(a["executedQty"]) * float(a["avgPrice"])),
                )
            )
        reconciled.append(dict(position_id=r["positionId"], symbol=r["symbol"], legs=legs))
    residuals = defaultdict(Decimal)
    for i in unmatched:
        o = orders[i]
        residuals[o["symbol"]] += Decimal(str(o["executedQty"])) * (1 if o["side"] == "BUY" else -1)
    losses = []
    for r in old["rows"]:
        if float(r["avgClosePrice"]) < float(r["avgCost"]):
            seconds = (r["closed"] - r["opened"]) / 1000
            losses.append(
                dict(
                    position_id=r["positionId"],
                    symbol=r["symbol"],
                    opened=iso(r["opened"]),
                    closed=iso(r["closed"]),
                    holding_seconds=seconds,
                    bin=round(math.log(max(seconds, 1)) / math.log(1.05)),
                )
            )
    mode_bin = Counter(x["bin"] for x in losses).most_common(1)[0][0]
    for x in losses:
        x["in_timer_group"] = abs(x["bin"] - mode_bin) <= 1
    report = dict(
        portfolio_id=PID,
        status="rule_unidentified",
        profitability_tested=False,
        script_sha256=sha(Path(__file__)),
        feature_script_sha256=sha(Path(__file__).with_name("open_automata.py")),
        sources={p.name: sha(p) for p in paths},
        old_count=len(old_map),
        new_count=len(new_map),
        reported_new_total=new["total"],
        union_count=len(rows),
        old_missing_current=sorted(old_map.keys() - new_map.keys()),
        changed_shared=[k for k in old_map.keys() & new_map.keys() if old_map[k] != new_map[k]],
        order_count=len(orders),
        reported_order_total=od["total"],
        order_types=dict(Counter((o["type"] + ":" + o["side"]) for o in orders)),
        first_order=iso(min(o["orderTime"] for o in orders)),
        last_order=iso(max(o["orderTime"] for o in orders)),
        ambiguous_orders=ambiguous,
        unmatched_order_indices=unmatched,
        unmatched_sides=dict(Counter(orders[i]["side"] for i in unmatched)),
        unmatched_symbols=len(residuals),
        unmatched_net_qty={s: str(q) for s, q in residuals.items()},
        missing_legs=missing,
        reconciliation=reconciled,
        additional_entry_pairs=pairs,
        scan_timer={k: v for k, v in features(old["rows"]).items() if k.startswith("timer")},
        current_timer={k: v for k, v in features(rows).items() if k.startswith("timer")},
        scan_negative_price_positions=losses,
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
                    "changed_shared",
                    "order_count",
                    "order_types",
                    "unmatched_sides",
                    "unmatched_symbols",
                    "missing_legs",
                    "ambiguous_orders",
                    "scan_timer",
                    "current_timer",
                )
            }
        )
    )
    print("additional pairs", len(pairs), "lower price", sum(p["lower_price"] for p in pairs))


if __name__ == "__main__":
    main()
