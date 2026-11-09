"""Freeze chronology/leverage strata before probing the long-duration x10 regime."""

import argparse
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import automata_revisit_probe as probe

PID = "5240250023760188416"
PREFIX = "longname_long10_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    source = args.root / f"longname_{PID}_2026-11-07.json"
    order_source = args.root / f"longname_{PID}_orders_rawpass_2026-11-07.json"
    destination = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not destination.exists(), "Cohort already frozen"
    positions, orders = probe.read(source)["positions"], probe.read(order_source)["orders"]
    assigned = defaultdict(list)
    for order in orders:
        matches = [
            p
            for p in positions
            if p["symbol"] == order["symbol"]
            and order["positionSide"] == "BOTH"
            and p["opened"] - 10 <= order["orderUpdateTime"] <= p["closed"] + 10
        ]
        assert len(matches) == 1
        assigned[matches[0]["positionId"]].append(order)
    start = int(datetime(2026, 10, 7, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(2026, 11, 4, 15, 5, tzinfo=UTC).timestamp() * 1000)
    groups, cohort = defaultdict(list), []
    for position in positions:
        assert position["side"] == "Long" and position["isolated"] == "Cross"
        buys = sorted(
            (o for o in assigned[position["positionId"]] if o["side"] == "BUY"),
            key=lambda o: o["orderUpdateTime"],
        )
        assert buys
        regime = (
            "early"
            if position["opened"] < start
            else "late75m"
            if position["opened"] >= end
            else "long60h"
        )
        group = f"{regime}_x{position['leverage']}"
        groups[group].append(position["positionId"])
        if group != "long60h_x10":
            continue
        order = buys[0]
        assert order["type"] == "MARKET"
        assert order["orderTime"] <= order["orderUpdateTime"]
        cohort.append(
            dict(
                position,
                position_opened=position["opened"],
                opened=order["orderTime"],
                first_entry_order=order,
                additional_entry_orders=buys[1:],
            )
        )
    assert len(cohort) == 15 and sum(map(len, groups.values())) == 45
    probe.save(
        destination,
        dict(
            positions=cohort,
            total=15,
            complete=False,
            scope="Chronological long-duration x10 regime; not filtered by exit duration",
            all_regime_position_ids=dict(groups),
            regime_boundary_ms=dict(start=start, late75m_start=end),
            boundary_origin="Previous execution audit; fixed before new market features",
            sources={source.name: probe.sha(source), order_source.name: probe.sha(order_source)},
            script_sha256=probe.sha(Path(__file__)),
        ),
    )
    probe.PREFIX, probe.PID = PREFIX, PID
    probe.prepare(args.root)
    path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    split = probe.read(path)
    split["holdout_scope"] = (
        "Unseen pre-first-order market features in long-duration x10 chronology only. "
        "Execution records and regime boundaries previously inspected. Late 75m x10 "
        "has only two days, long x5 five days; neither declared validated or pooled."
    )
    probe.save(path, split)
    for phase, part in split["phases"].items():
        print(phase, part["days"][0], part["days"][-1])


if __name__ == "__main__":
    main()
