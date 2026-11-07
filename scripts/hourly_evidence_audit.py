"""Inventory saved execution evidence and shared hourly portfolio choices offline."""

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from statistics import median

from hourly_automata_audit import HOUR, PIDS, digest, read

MINUTE = 60_000


def audit(root):
    hashes, minutes, conflicts = {}, {}, set()
    inventory = {}
    for folder in ("anno_klines", "dhz_klines", "twin_klines"):
        files, count = sorted((root / folder).glob("*.json")), 0
        for path in files:
            hashes[f"{folder}/{path.name}"] = digest(path)
            data = read(path)
            rows = data["1m"] if isinstance(data, dict) else data
            target = minutes.setdefault(path.stem, {})
            for row in rows:
                t, close = int(row[0]), float(row[4])
                if t % MINUTE:
                    raise ValueError(f"Unaligned minute in {path}")
                key = (path.stem, t)
                if t in target and target[t] != close:
                    conflicts.add(key)
                target[t] = close
                count += 1
        inventory[folder] = dict(files=len(files), rows=count)
    # Never silently choose between disagreeing archives.
    for symbol, t in conflicts:
        del minutes[symbol][t]
    metadata = {}
    for name in ("twin_exchangeinfo_2026-10-27.json", "hourly_universe_symbols_2026-10-26.json"):
        path = root / name
        hashes[name] = digest(path)
        data = read(path)
        metadata[name] = dict(
            symbols=len(data), fields=sorted({k for v in data.values() for k in v})
        )
    positions, order_stats, choices = {}, {}, {}
    for pid in PIDS:
        pos_path = root / f"hourly_{pid}_2026-11-07.json"
        ord_path = root / f"hourly_{pid}_orders_2026-11-07.json"
        for path in (pos_path, ord_path):
            hashes[path.name] = digest(path)
        positions[pid] = read(pos_path)["positions"]
        orders = read(ord_path)["orders"]
        buys = [o for o in orders if o["side"] == "BUY"]
        coverage, examples = Counter(), []
        for order in buys:
            t = order["orderTime"] // MINUTE * MINUTE - MINUTE
            rows = minutes.get(order["symbol"], {})
            coverage["buy_orders"] += 1
            if t in rows:
                coverage["last_closed_minute"] += 1
            for h in (1, 4, 12, 24, 48):
                if t in rows and t - h * HOUR in rows:
                    coverage[f"selected_return_{h}h_available"] += 1
            if len(examples) < 3 and t in rows:
                examples.append(
                    dict(
                        symbol=order["symbol"],
                        order_time=order["orderTime"],
                        closed_minute=t,
                        close=rows[t],
                    )
                )
        notionals = sorted(float(o["executedQty"]) * float(o["avgPrice"]) for o in buys)
        order_stats[pid] = dict(
            coverage={
                k: coverage[k]
                for k in (
                    "buy_orders",
                    "last_closed_minute",
                    *(f"selected_return_{h}h_available" for h in (1, 4, 12, 24, 48)),
                )
            },
            buy_notional_min=notionals[0],
            buy_notional_median=median(notionals),
            buy_notional_max=notionals[-1],
            examples=examples,
        )
        by_hour = {}
        for row in positions[pid]:
            by_hour.setdefault(row["opened"] // HOUR * HOUR, set()).add(row["symbol"])
        choices[pid] = by_hour
    pairs = {}
    for left, right in combinations(PIDS, 2):
        common = sorted(choices[left].keys() & choices[right].keys())
        single = [t for t in common if len(choices[left][t]) == len(choices[right][t]) == 1]
        mismatches = [t for t in single if choices[left][t] != choices[right][t]]
        pairs[f"{left}/{right}"] = dict(
            common_opening_hours=len(common),
            single_symbol_hours=len(single),
            same_symbol_hours=len(single) - len(mismatches),
            different_symbol_hours=len(mismatches),
            multiple_symbol_hours=len(common) - len(single),
            monthly={
                month: dict(
                    n=sum(
                        datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m") == month
                        for t in single
                    ),
                    same=sum(
                        datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m") == month
                        and choices[left][t] == choices[right][t]
                        for t in single
                    ),
                )
                for month in ("2026-07", "2026-08", "2026-09", "2026-10", "2026-11")
            },
            mismatches=[
                dict(
                    hour=datetime.fromtimestamp(t / 1000, UTC).isoformat(),
                    left=sorted(choices[left][t]),
                    right=sorted(choices[right][t]),
                )
                for t in mismatches
            ],
        )
    return dict(
        method="saved_minute_metadata_and_joint_opening_inventory_v1",
        hashes={**hashes, Path(__file__).name: digest(Path(__file__))},
        minute_inventory=inventory,
        conflicting_symbol_minutes=len(conflicts),
        metadata=metadata,
        order_evidence=order_stats,
        paired_openings=pairs,
        limitations=[
            "Local open-automata archives only; no historical order filters found.",
            "Minute endpoint coverage does not prove intermediate continuity.",
            "Selected-symbol returns cannot establish ranks across the universe.",
            "Shared opening hours exclude ongoing positions and idle hours.",
            "Same choice does not prove identical signals or account ownership.",
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k != "hashes"}
    summary["paired_openings"] = {
        k: {field: value for field, value in v.items() if field != "mismatches"}
        for k, v in result["paired_openings"].items()
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
