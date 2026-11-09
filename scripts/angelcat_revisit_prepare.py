"""Freeze first MARKET BUY x5 entries, keeping adds and other regimes separate."""

import argparse
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

import automata_revisit_probe as probe

PID = "5262575735183470080"
PREFIX = "angelcat_first_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"angelcat_{PID}{suffix}.json"
        for suffix in ("_scan_2026-10-25", "_2026-11-07", "_orders_2026-11-07")
    ]
    destination = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not destination.exists(), "Cohort already frozen"
    old, current, raw = map(probe.read, paths)
    positions = current["positions"]
    by_id = {p["positionId"]: p for p in positions}
    assert len(by_id) == len(positions) == 187
    assert all(by_id[p["positionId"]] == p for p in old["rows"])
    assigned, unmatched = defaultdict(list), []
    for index, order in enumerate(raw["orders"]):
        matches = [
            p
            for p in positions
            if p["symbol"] == order["symbol"]
            and p["side"].upper() == order["positionSide"]
            and p["opened"] - 2 <= order["orderUpdateTime"] <= p["closed"] + 2
        ]
        assert len(matches) <= 1
        if matches:
            assigned[matches[0]["positionId"]].append(order)
        else:
            assert order["side"] == "BUY"
            unmatched.append(index)
    groups, cohort = defaultdict(list), []
    for position in positions:
        assert position["side"] == "Long" and position["isolated"] == "Cross"
        stream = sorted(assigned[position["positionId"]], key=lambda o: o["orderUpdateTime"])
        buys = [o for o in stream if o["side"] == "BUY"]
        sells = [o for o in stream if o["side"] == "SELL"]
        assert buys and len(sells) == 1
        qty = sum(Fraction(str(o["executedQty"])) for o in buys)
        assert qty == Fraction(str(sells[0]["executedQty"]))
        assert qty == Fraction(str(position["maxOpenInterest"]))
        assert qty == Fraction(str(position["closedVolume"]))
        balance, peak = Fraction(0), Fraction(0)
        for order in stream:
            balance += Fraction(str(order["executedQty"])) * (1 if order["side"] == "BUY" else -1)
            assert balance >= 0
            peak = max(peak, balance)
        assert balance == 0 and peak == qty
        first = buys[0]
        assert first["orderTime"] <= position["opened"] + 2
        assert first["orderUpdateTime"] >= position["opened"] - 2
        assert first["orderTime"] <= first["orderUpdateTime"]
        group = f"Long_x{position['leverage']}_Cross_{first['type']}"
        groups[group].append(
            dict(position_id=position["positionId"], placement_day=probe.day(first["orderTime"]))
        )
        if group == "Long_x5_Cross_MARKET":
            assert abs(first["orderUpdateTime"] - position["opened"]) <= 2
            cohort.append(
                dict(
                    position,
                    position_opened=position["opened"],
                    opened=first["orderTime"],
                    first_entry_order=first,
                    additional_entry_orders=buys[1:],
                )
            )
    assert len(cohort) == 150 and len(unmatched) == 51
    probe.save(
        destination,
        dict(
            positions=cohort,
            total=150,
            complete=False,
            scope="Order-covered first MARKET BUY Long x5 Cross; additional BUYs not new entries",
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
        "Unseen pre-first-MARKET-BUY features in Long x5 Cross only. Six active days, "
        "four development and two holdout; small descriptive validation, not full rule recovery. "
        "Other leverages and first-LIMIT regimes have fewer than six days; not pooled. "
        "Closed-position coverage excludes 51 residual BUYs and unobserved open state."
    )
    probe.save(path, split)
    print({k: dict(n=len(v["sample"]), days=v["days"]) for k, v in split["phases"].items()})


if __name__ == "__main__":
    main()
