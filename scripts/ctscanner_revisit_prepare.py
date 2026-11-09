"""Freeze early ETH Long x20 proxies, excluding previously tested short trades."""

import argparse
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

import automata_revisit_probe as probe

PID = "5030491098292789505"
PREFIX = "ctscanner_early_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"ctscanner_{PID}{suffix}.json"
        for suffix in ("_scan_2026-10-25", "_2026-11-07", "_orders_2026-11-07")
    ]
    destination = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not destination.exists(), "Cohort already frozen"
    old, current, raw = map(probe.read, paths)
    old_map = {p["positionId"]: p for p in old["rows"]}
    updated = {p["positionId"]: p for p in current["positions"]}
    assert all(old_map[k] == p for k, p in updated.items())
    positions = sorted((old_map | updated).values(), key=lambda p: p["opened"])
    assert len(positions) == 87
    assigned = defaultdict(list)
    for order in raw["orders"]:
        matches = [
            p
            for p in positions
            if p["symbol"] == order["symbol"]
            and p["side"].upper() == order["positionSide"]
            and p["opened"] - 2 <= order["orderUpdateTime"] <= p["closed"] + 2
        ]
        assert len(matches) == 1
        assigned[matches[0]["positionId"]].append(order)
    cohort, strata = [], defaultdict(list)
    for p in positions:
        stream = assigned.get(p["positionId"], [])
        if stream:
            entry_side = "BUY" if p["side"] == "Long" else "SELL"
            entries = [o for o in stream if o["side"] == entry_side]
            exits = [o for o in stream if o["side"] != entry_side]
            volume = sum(Fraction(str(o["executedQty"])) for o in entries)
            assert volume == sum(Fraction(str(o["executedQty"])) for o in exits)
            assert volume == Fraction(str(p["maxOpenInterest"]))
            assert volume == Fraction(str(p["closedVolume"]))
        date = probe.day(p["opened"])
        regime = (
            "early"
            if date < "2026-09-15"
            else "sep15_16"
            if date < "2026-09-25"
            else "sep25_29"
            if date < "2026-10-01"
            else "oct15"
        )
        group = f"{regime}_{p['side']}_x{p['leverage']}_{p['isolated']}"
        strata[group].append(dict(position_id=p["positionId"], day=date))
        if group == "early_Long_x20_Cross":
            assert not stream and p["symbol"] == "ETHUSDT"
            assert p["opened"] % 900000 <= 15000
            cohort.append(
                dict(p, position_opened=p["opened"], timing_basis="position_opened_proxy")
            )
    previous = probe.read(args.root / "ctscanner_audit_2026-11-07.json")
    # The old diagnostic used only positions held <=5m, including future exit selection.
    old_test_ids = {p["positionId"] for p in positions if p["closed"] - p["opened"] <= 300000}
    assert len(old_test_ids) == 70 and not old_test_ids & {p["positionId"] for p in cohort}
    assert len(cohort) == 9 and len(assigned) == 75 and len(raw["orders"]) == 153
    probe.save(
        destination,
        dict(
            positions=cohort,
            total=9,
            complete=False,
            excluded_position_ids=sorted(
                {p["positionId"] for p in positions} - {p["positionId"] for p in cohort}
            ),
            strata={
                k: dict(n=len(v), days=sorted({x["day"] for x in v}), positions=v)
                for k, v in strata.items()
            },
            old_test_ids=sorted(old_test_ids),
            old_audit_sha256=probe.sha(args.root / "ctscanner_audit_2026-11-07.json"),
            old_thresholds_pct=previous["diagnostic_thresholds_pct"],
            sources={p.name: probe.sha(p) for p in paths},
            script_sha256=probe.sha(Path(__file__)),
            scope="Early ETH Long x20 quarter-hour openings; no orders, no known placement time",
        ),
    )
    probe.PREFIX, probe.PID = PREFIX, PID
    probe.prepare(args.root)
    path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    split = probe.read(path)
    split["holdout_scope"] = (
        "New pre-position-opening market context in early ETH Long x20, disjoint from "
        "all 70 previous 2%/5% event diagnostics. No order timing/type/first-fill evidence. "
        "Seven active days; small sample. Other regimes not pooled and not retested."
    )
    probe.save(path, split)
    print({k: dict(n=len(v["sample"]), days=v["days"]) for k, v in split["phases"].items()})


if __name__ == "__main__":
    main()
