"""Audit first LIMIT placements and day sufficiency without pooling regimes."""

import argparse
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

from automata_revisit_probe import day, read, save, sha

PID = "5095694990932973056"


def audit(root):
    sources = [
        root / f"directional_{PID}_merged_2026-11-07.json",
        root / f"directional_{PID}_orders_2026-11-07.json",
    ]
    positions = read(sources[0])["positions"]
    orders = read(sources[1])["orders"]
    assert len(positions) == len({p["positionId"] for p in positions}) == 1686
    assert len(orders) == 52
    assigned = defaultdict(list)
    for order in orders:
        matches = [
            p
            for p in positions
            if p["symbol"] == order["symbol"]
            and p["side"].upper() == order["positionSide"]
            and p["opened"] - 2 <= order["orderUpdateTime"]
            and (p["closed"] is None or order["orderUpdateTime"] <= p["closed"] + 2)
        ]
        assert len(matches) == 1
        assigned[matches[0]["positionId"]].append(order)
    groups = defaultdict(list)
    excluded = []
    for p in positions:
        stream = assigned.get(p["positionId"], [])
        if not stream:
            excluded.append(p["positionId"])
            continue
        entry_side = "BUY" if p["side"] == "Long" else "SELL"
        entries = sorted(
            (o for o in stream if o["side"] == entry_side), key=lambda o: o["orderUpdateTime"]
        )
        exits = [o for o in stream if o["side"] != entry_side]
        assert len(entries) == len(exits) == 1
        entry = entries[0]
        assert entry["type"] == "LIMIT"
        assert entry["orderTime"] <= entry["orderUpdateTime"]
        assert abs(entry["orderUpdateTime"] - p["opened"]) <= 2
        opened = sum(Fraction(str(o["executedQty"])) for o in entries)
        closed = sum(Fraction(str(o["executedQty"])) for o in exits)
        assert opened == Fraction(str(p["maxOpenInterest"]))
        assert closed == Fraction(str(p["closedVolume"]))
        assert opened >= closed
        if p["closed"] is not None:
            assert opened == closed
        groups[f"{p['side']}_x{p['leverage']}_{p['isolated']}"].append(
            dict(
                position_id=p["positionId"],
                symbol=p["symbol"],
                position_opened=p["opened"],
                decision_time=entry["orderTime"],
                placement_day=day(entry["orderTime"]),
                first_entry_order=entry,
                exit_order=exits[0],
                remaining_qty=str(opened - closed),
            )
        )
    strata = {}
    for name, rows in sorted(groups.items()):
        days = sorted({r["placement_day"] for r in rows})
        strata[name] = dict(n=len(rows), days=days, eligible=len(days) >= 6, positions=rows)
    assert len(assigned) == 26 and len(excluded) == 1660
    assert not any(g["eligible"] for g in strata.values())
    return dict(
        portfolio_id=PID,
        status="rule_unidentified",
        profitability_tested=False,
        market_test_performed=False,
        independent_holdout_available=False,
        decision="Insufficient placement days in every side/leverage/margin stratum",
        min_active_days=6,
        matched_orders=52,
        covered_positions=26,
        excluded_position_ids=excluded,
        strata=strata,
        pooled_days=sorted({r["placement_day"] for g in groups.values() for r in g}),
        sources={p.name: sha(p) for p in sources},
        script_sha256=sha(Path(__file__)),
        limitations=[
            "Coverage is not the full entry universe; cancelled placements absent",
            "First fill avgPrice does not identify the original LIMIT price",
            "August common-close mechanism has no order coverage",
            "No hypotheses rejected by market evidence in this revisit",
            "Return requires new same-regime active days or independent configuration",
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    result = audit(args.root)
    save(args.out, result)
    print({k: dict(n=v["n"], days=v["days"]) for k, v in result["strata"].items()})


if __name__ == "__main__":
    main()
