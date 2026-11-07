"""Audit #74 using archived positions and executed orders, without P&L selection."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from open_automata import features

PID = "5238239613252372224"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso(t):
    return datetime.fromtimestamp(t / 1000, UTC).isoformat()


def describe(rows):
    durations = [(r["closed"] - r["opened"]) / 1000 for r in rows]
    return dict(
        n=len(rows),
        first_open=iso(min(r["opened"] for r in rows)),
        last_close=iso(max(r["closed"] for r in rows)),
        symbols=len({r["symbol"] for r in rows}),
        leverage=dict(Counter(r["leverage"] for r in rows)),
        duration_min=min(durations),
        duration_median=median(durations),
        duration_max=max(durations),
        within_300s_10s=sum(abs(t - 300) <= 10 for t in durations),
        within_14400s_30s=sum(abs(t - 14400) <= 30 for t in durations),
        at_most_310s=sum(t <= 310 for t in durations),
        # Descriptive tolerances only; these do not define an executable rule.
        early_before_290s=sum(t < 290 for t in durations),
        early_adverse=sum(
            (r["closed"] - r["opened"]) < 290000
            and (float(r["avgClosePrice"]) - float(r["avgCost"]))
            * (1 if r["side"] == "Long" else -1)
            < 0
            for r in rows
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"yang_{PID}{s}.json"
        for s in ("_scan_2026-10-25", "_2026-11-07", "_orders_2026-11-07")
    ]
    old, new, od = [json.loads(p.read_text(encoding="utf-8-sig")) for p in paths]
    raw_path = args.root / f"yang_{PID}_orders_rawpass_2026-11-07.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    paths.append(raw_path)
    om = {r["positionId"]: r for r in old["rows"]}
    nm = {r["positionId"]: r for r in new["positions"]}
    rows = sorted((om | nm).values(), key=lambda r: r["opened"])

    def order_key(o):
        return tuple(
            o[k]
            for k in (
                "symbol",
                "side",
                "type",
                "positionSide",
                "orderTime",
                "orderUpdateTime",
                "executedQty",
                "avgPrice",
            )
        )

    first_orders = {order_key(o): o for o in od["orders"]}
    second_orders = {order_key(o): o for o in raw["orders"]}
    orders = sorted((first_orders | second_orders).values(), key=lambda o: o["orderUpdateTime"])
    assigned = defaultdict(list)
    unmatched, ambiguous = [], []
    for i, o in enumerate(orders):
        matches = [
            r
            for r in rows
            if r["symbol"] == o["symbol"]
            and r["side"].upper() == o["positionSide"]
            # API position timestamps can trail execution (observed AGT: 5 ms).
            and r["opened"] - 10 <= o["orderUpdateTime"] <= r["closed"] + 10
        ]
        if len(matches) == 1:
            assigned[matches[0]["positionId"]].append(i)
        else:
            (ambiguous if matches else unmatched).append(i)
    legs, missing = [], []
    for r in rows:
        entry = "BUY" if r["side"] == "Long" else "SELL"
        for side, field in (
            (entry, "avgCost"),
            ("SELL" if entry == "BUY" else "BUY", "avgClosePrice"),
        ):
            indices = [i for i in assigned[r["positionId"]] if orders[i]["side"] == side]
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
                    side=side,
                    entry=side == entry,
                    order_indices=indices,
                    qty=str(qty),
                    qty_error=str(qty - Decimal(str(r["closedVolume"]))),
                    price_error=str(abs(avg - Decimal(str(r[field])))),
                    price_precision_compatible=abs(avg - Decimal(str(r[field]))) <= precision,
                    types=dict(Counter(o["type"] for o in selected)),
                    placement_to_update_ms=[
                        o["orderUpdateTime"] - o["orderTime"] for o in selected
                    ],
                )
            )
    events = sorted([(r["opened"], 1) for r in rows] + [(r["closed"], -1) for r in rows])
    peak = running = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    report = dict(
        portfolio_id=PID,
        status="rule_unidentified",
        profitability_tested=False,
        script_sha256=sha(Path(__file__)),
        feature_script_sha256=sha(Path(__file__).with_name("open_automata.py")),
        sources={p.name: sha(p) for p in paths},
        raw_order_pages={
            p.name: sha(p)
            for p in sorted((args.root / "yang_order_pages_2026-11-07").glob("*.json"))
        },
        profile_description=new["detail"].get("description"),
        old_count=len(om),
        current_count=len(nm),
        reported_current_total=new["total"],
        current_complete=new["complete"],
        union_count=len(rows),
        old_missing_current=sorted(om.keys() - nm.keys()),
        changed_shared=[k for k in om.keys() & nm.keys() if om[k] != nm[k]],
        status_counts=dict(Counter(r["status"] for r in rows)),
        by_side={s: describe([r for r in rows if r["side"] == s]) for s in ("Long", "Short")},
        peak_simultaneous=peak,
        scan_timer={k: v for k, v in features(old["rows"]).items() if k.startswith("timer")},
        current_timer={k: v for k, v in features(rows).items() if k.startswith("timer")},
        order_count=len(orders),
        assignment_tolerance_ms=10,
        reported_order_total=od["total"],
        order_passes=dict(
            first_unique=len(first_orders),
            second_unique=len(second_orders),
            second_reported_total=raw["total"],
            second_observed_totals=raw["observed_totals"],
            second_raw_count=raw["raw_count"],
            second_duplicates=raw["duplicates"],
            only_first=len(first_orders.keys() - second_orders.keys()),
            only_second=len(second_orders.keys() - first_orders.keys()),
        ),
        order_types=dict(Counter(o["type"] + ":" + o["side"] for o in orders)),
        first_order=iso(min(o["orderTime"] for o in orders)),
        last_order=iso(max(o["orderTime"] for o in orders)),
        ambiguous_orders=ambiguous,
        unmatched_order_indices=unmatched,
        unmatched_sides=dict(
            Counter(orders[i]["positionSide"] + ":" + orders[i]["side"] for i in unmatched)
        ),
        missing_legs=missing,
        legs=legs,
    )
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("legs", "missing_legs")},
            ensure_ascii=True,
        )
    )
    print(
        "legs",
        len(legs),
        "missing",
        len(missing),
        "volume errors",
        sum(Decimal(x["qty_error"]) != 0 for x in legs),
        "price flags",
        sum(not x["price_precision_compatible"] for x in legs),
    )


if __name__ == "__main__":
    main()
