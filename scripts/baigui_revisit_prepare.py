"""Freeze first orders from complete Long/Short flows without using profitability."""

import argparse
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import automata_revisit_probe as probe

PID = "5073563794731136000"
PREFIX = "baigui_side_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    source = args.root / f"baigui_{PID}_2026-11-07.json"
    order_source = args.root / f"baigui_{PID}_orders_rawpass_2026-11-07.json"
    destination = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not destination.exists(), "Cohort already frozen"
    positions, orders = probe.read(source)["positions"], probe.read(order_source)["orders"]
    assigned = defaultdict(list)
    unmatched = []
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
    cohort, excluded = [], []
    for position in positions:
        fills = sorted(assigned[position["positionId"]], key=lambda o: o["orderUpdateTime"])
        side = "BUY" if position["side"] == "Long" else "SELL"
        entries = [o for o in fills if o["side"] == side]
        exits = [o for o in fills if o["side"] != side]
        qty = Decimal(str(position["closedVolume"]))
        sums = [
            sum((Decimal(str(o["executedQty"])) for o in leg), Decimal(0))
            for leg in (entries, exits)
        ]
        inventory = minimum = peak = Decimal(0)
        for order in fills:
            inventory += Decimal(str(order["executedQty"])) * (1 if order["side"] == side else -1)
            minimum, peak = min(minimum, inventory), max(peak, inventory)
        complete = (
            bool(entries and exits)
            and sums == [qty, qty]
            and inventory == 0
            and minimum == 0
            and peak == Decimal(str(position["maxOpenInterest"]))
        )
        if not complete:
            excluded.append(position["positionId"])
            continue
        order = entries[0]
        assert order["type"] == "MARKET" and order["orderTime"] <= order["orderUpdateTime"]
        assert position["leverage"] == "5" and position["isolated"] == "Cross"
        cohort.append(
            dict(
                position,
                position_opened=position["opened"],
                opened=order["orderTime"],
                first_entry_order=order,
                additional_entry_orders=entries[1:],
            )
        )
    assert len(cohort) == 459 and len(excluded) == 498 and len(unmatched) == 14
    probe.save(
        destination,
        dict(
            positions=cohort,
            total=len(cohort),
            complete=False,
            scope="First orders of complete archived x5 Cross flows; incomplete decision universe",
            excluded_position_ids=excluded,
            unmatched_order_indices=unmatched,
            sources={source.name: probe.sha(source), order_source.name: probe.sha(order_source)},
            script_sha256=probe.sha(Path(__file__)),
        ),
    )
    probe.PREFIX, probe.PID = PREFIX, PID
    probe.prepare(args.root)
    path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    split = probe.read(path)
    split["holdout_scope"] = (
        "Unseen pre-first-order market features only; executions previously inspected. "
        "Long/Short share the same x5 Cross regime and day split, with separate side summaries. "
        "No cross-sectional universe or labeled nonentries for symbol selection."
    )
    probe.save(path, split)
    for phase, part in split["phases"].items():
        print(phase, part["days"][0], part["days"][-1])


if __name__ == "__main__":
    main()
