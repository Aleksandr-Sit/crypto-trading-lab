"""Freeze the largest homogeneous short entry stratum without pooling leverage."""

import argparse
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

import automata_revisit_probe as probe

PID = "5238239613252372224"
PREFIX = "yang_short20_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"yang_{PID}{suffix}.json"
        for suffix in (
            "_scan_2026-10-25",
            "_2026-11-07",
            "_orders_2026-11-07",
            "_orders_rawpass_2026-11-07",
        )
    ]
    destination = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not destination.exists(), "Cohort already frozen"
    old, current, first_pass, raw = map(probe.read, paths)
    positions = current["positions"]
    by_id = {p["positionId"]: p for p in positions}
    assert len(by_id) == len(positions) == 1199
    assert all(by_id[p["positionId"]] == p for p in old["rows"])

    def key(o):
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

    assert {key(o) for o in first_pass["orders"]} <= {key(o) for o in raw["orders"]}
    orders = raw["orders"]
    assert len({key(o) for o in orders}) == len(orders) == 2467
    assigned, unmatched = defaultdict(list), []
    for index, order in enumerate(orders):
        matches = [
            p
            for p in positions
            if p["symbol"] == order["symbol"]
            and p["side"].upper() == order["positionSide"]
            and p["opened"] - 10 <= order["orderUpdateTime"] <= p["closed"] + 10
        ]
        assert len(matches) <= 1
        if matches:
            assigned[matches[0]["positionId"]].append(order)
        else:
            unmatched.append(index)
    groups, cohort = defaultdict(list), []
    for position in positions:
        entry = "BUY" if position["side"] == "Long" else "SELL"
        stream = sorted(assigned[position["positionId"]], key=lambda o: o["orderUpdateTime"])
        entries = [o for o in stream if o["side"] == entry]
        exits = [o for o in stream if o["side"] != entry]
        assert entries and exits
        first = entries[0]
        group = f"{position['side']}_x{position['leverage']}_{position['isolated']}_{first['type']}"
        groups[group].append(
            dict(position_id=position["positionId"], placement_day=probe.day(first["orderTime"]))
        )
        if group != "Short_x20_Cross_MARKET":
            continue
        assert len(entries) == len(exits) == 1
        qty = Fraction(str(first["executedQty"]))
        assert qty == Fraction(str(exits[0]["executedQty"]))
        assert qty == Fraction(str(position["maxOpenInterest"]))
        assert qty == Fraction(str(position["closedVolume"]))
        assert first["orderTime"] <= first["orderUpdateTime"]
        assert abs(first["orderUpdateTime"] - position["opened"]) <= 10
        cohort.append(
            dict(
                position,
                position_opened=position["opened"],
                opened=first["orderTime"],
                first_entry_order=first,
                additional_entry_orders=entries[1:],
            )
        )
    assert len(cohort) == 410 and len(unmatched) == 2
    assert max(groups, key=lambda k: len(groups[k])) == "Short_x20_Cross_MARKET"
    probe.save(
        destination,
        dict(
            positions=cohort,
            total=410,
            complete=False,
            scope=(
                "Largest side/leverage/margin/first-order-type stratum; "
                "selected by count before market"
            ),
            strata={
                k: dict(n=len(v), days=sorted({x["placement_day"] for x in v}), positions=v)
                for k, v in groups.items()
            },
            unmatched_order_indices=unmatched,
            excluded_position_ids=sorted(set(by_id) - {p["positionId"] for p in cohort}),
            sources={p.name: probe.sha(p) for p in paths},
            script_sha256=probe.sha(Path(__file__)),
        ),
    )
    probe.PREFIX, probe.PID = PREFIX, PID
    probe.prepare(args.root)
    path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    split = probe.read(path)
    split["holdout_scope"] = (
        "Unseen market features before first MARKET SELL in Short x20 Cross only. "
        "Other Short leverage and all Long strata remain untested, not pooled. "
        "No duration/exit-profit filter; early exits and timer exceptions retained. "
        "Existing Long AERO/AXS volume deficits and unmatched JCT pair remain unresolved."
    )
    probe.save(path, split)
    print({k: dict(n=len(v["sample"]), days=v["days"]) for k, v in split["phases"].items()})


if __name__ == "__main__":
    main()
