"""Freeze first LIMIT placements in the archived Long x10 Cross regime."""

import argparse
from collections import defaultdict
from pathlib import Path

import automata_revisit_probe as probe

PID = "5108763538520982528"
PREFIX = "btctonghui_first_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    source = args.root / f"btctonghui_{PID}_2026-11-07.json"
    order_source = args.root / f"btctonghui_{PID}_orders_rawpass_2026-11-07.json"
    destination = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not destination.exists(), "Cohort already frozen"
    positions = probe.read(source)["positions"]
    orders = probe.read(order_source)["orders"]
    assigned = defaultdict(list)
    for order in orders:
        matches = [
            p
            for p in positions
            if p["symbol"] == order["symbol"]
            and p["side"].upper() == order["positionSide"]
            and p["opened"] - 1500 <= order["orderUpdateTime"] <= p["closed"] + 1500
        ]
        assert len(matches) == 1
        assigned[matches[0]["positionId"]].append(order)
    cohort, excluded, additions = [], [], []
    for position in positions:
        buys = sorted(
            (o for o in assigned[position["positionId"]] if o["side"] == "BUY"),
            key=lambda o: o["orderUpdateTime"],
        )
        assert buys and all(o["type"] == "LIMIT" for o in buys)
        first = buys[0]
        assert first["orderTime"] <= first["orderUpdateTime"]
        assert all(first["orderTime"] <= o["orderTime"] for o in buys[1:])
        if position["leverage"] != "10" or position["isolated"] != "Cross":
            excluded.append(position["positionId"])
            continue
        assert position["side"] == "Long"
        assert abs(first["orderUpdateTime"] - position["opened"]) <= 1500
        cohort.append(
            dict(
                position,
                position_opened=position["opened"],
                opened=first["orderTime"],
                first_entry_order=first,
                additional_entry_orders=buys[1:],
            )
        )
        additions.extend(buys[1:])
    assert len(cohort) == 139 and len(excluded) == 44
    probe.save(
        destination,
        dict(
            positions=cohort,
            total=len(cohort),
            complete=False,
            scope="First executed LIMIT placements, Long x10 Cross only; not full decision log",
            excluded_position_ids=excluded,
            additional_buys_excluded=len(additions),
            sources={source.name: probe.sha(source), order_source.name: probe.sha(order_source)},
            script_sha256=probe.sha(Path(__file__)),
        ),
    )
    probe.PREFIX, probe.PID = PREFIX, PID
    probe.prepare(args.root)
    path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    split = probe.read(path)
    split["holdout_scope"] = (
        "Unseen market features before first executed LIMIT orderTime, not fill time. "
        "Order archive may omit cancelled/replaced orders. Position/order execution "
        "was previously inspected. Added buys and other leverage regimes excluded."
    )
    probe.save(path, split)
    for phase, part in split["phases"].items():
        print(phase, part["days"][0], part["days"][-1])


if __name__ == "__main__":
    main()
