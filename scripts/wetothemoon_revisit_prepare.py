"""Freeze order-covered LIMIT placements, preserving the old position scan."""

import argparse
from collections import defaultdict
from pathlib import Path

import automata_revisit_probe as probe

PID = "5085926543686119937"
PREFIX = "wetothemoon_first_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"wetothemoon_{PID}{suffix}.json"
        for suffix in (
            "_scan_2026-10-25",
            "_2026-11-07",
            "_orders_rawpass_2026-11-07",
            "_orders50_rawpass_2026-11-07",
        )
    ]
    destination = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not destination.exists(), "Cohort already frozen"
    old, current, first, second = map(probe.read, paths)
    original = {p["positionId"]: p for p in old["rows"]}
    updated = {p["positionId"]: p for p in current["positions"]}
    assert all(p == updated[k] for k, p in original.items() if k in updated)
    positions = list((original | updated).values())

    def key(order):
        return tuple(sorted(order.items()))

    assert {key(o) for o in first["orders"]} == {key(o) for o in second["orders"]}
    assigned = defaultdict(list)
    for order in first["orders"]:
        matches = [
            p
            for p in positions
            if p["symbol"] == order["symbol"]
            and order["positionSide"] == "BOTH"
            and p["side"] == "Long"
            and p["opened"] - 10 <= order["orderUpdateTime"] <= p["closed"] + 10
        ]
        assert len(matches) == 1
        assigned[matches[0]["positionId"]].append(order)
    cohort, excluded = [], []
    for position in positions:
        buys = [o for o in assigned[position["positionId"]] if o["side"] == "BUY"]
        if not buys:
            excluded.append(position["positionId"])
            continue
        assert len(buys) == 1
        order = buys[0]
        assert order["type"] == "LIMIT" and order["orderTime"] <= order["orderUpdateTime"]
        assert order["orderTime"] <= position["opened"] + 10
        assert position["leverage"] == "1" and position["isolated"] == "Cross"
        cohort.append(
            dict(
                position,
                position_opened=position["opened"],
                opened=order["orderTime"],
                first_entry_order=order,
            )
        )
    assert len(cohort) == 30 and len(excluded) == 56
    probe.save(
        destination,
        dict(
            positions=cohort,
            total=30,
            complete=False,
            scope="Order-covered Long x1 Cross LIMIT placements; cancelled orders absent",
            excluded_position_ids=excluded,
            sources={path.name: probe.sha(path) for path in paths},
            script_sha256=probe.sha(Path(__file__)),
        ),
    )
    probe.PREFIX, probe.PID = PREFIX, PID
    probe.prepare(args.root)
    path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    split = probe.read(path)
    split["holdout_scope"] = (
        "Unseen pre-placement market features only; all execution records previously "
        "inspected. Group by placement day; filled/cancelled universe incomplete."
    )
    probe.save(path, split)
    for phase, part in split["phases"].items():
        print(phase, part["days"][0], part["days"][-1])


if __name__ == "__main__":
    main()
