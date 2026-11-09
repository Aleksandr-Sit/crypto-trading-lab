"""Freeze September first LIMIT SELL x10 placements, disjoint from old Long ranks."""

import argparse
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

import automata_revisit_probe as probe

PID = "5220829154176782593"
PREFIX = "shenming_short10_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"shenming_{PID}{suffix}.json"
        for suffix in ("_scan_2026-10-25", "_2026-11-07", "_orders_2026-11-07")
    ]
    destination = args.root / f"{PREFIX}_{PID}_2026-11-09.json"
    assert not destination.exists(), "Cohort already frozen"
    old, current, raw = map(probe.read, paths)
    positions = current["positions"]
    by_id = {p["positionId"]: p for p in positions}
    assert len(by_id) == len(positions) == 485
    assert all(by_id[p["positionId"]] == p for p in old["rows"])
    assigned = defaultdict(list)
    for index, order in enumerate(raw["orders"]):
        matches = [
            p
            for p in positions
            if p["symbol"] == order["symbol"]
            and p["side"].upper() == order["positionSide"]
            and p["opened"] - 2 <= order["orderUpdateTime"] <= p["closed"] + 2
        ]
        assert len(matches) == 1
        assigned[matches[0]["positionId"]].append((index, order))
    cohort, strata = [], defaultdict(list)
    for p in positions:
        stream = sorted(assigned[p["positionId"]], key=lambda v: (v[1]["orderUpdateTime"], v[0]))
        entry_side = "BUY" if p["side"] == "Long" else "SELL"
        entries = [o for _, o in stream if o["side"] == entry_side]
        exits = [o for _, o in stream if o["side"] != entry_side]
        entry_qty = sum(Fraction(str(o["executedQty"])) for o in entries)
        assert entry_qty == sum(Fraction(str(o["executedQty"])) for o in exits)
        assert entry_qty == Fraction(str(p["closedVolume"]))
        balance, peak = Fraction(0), Fraction(0)
        for _, order in stream:
            balance += Fraction(str(order["executedQty"])) * (
                1 if order["side"] == entry_side else -1
            )
            assert balance >= 0
            peak = max(peak, balance)
        assert balance == 0 and peak == Fraction(str(p["maxOpenInterest"]))
        first = entries[0]
        group = (
            f"{probe.day(p['opened'])[:7]}_{p['side']}_x{p['leverage']}_"
            f"{p['isolated']}_{first['type']}"
        )
        strata[group].append(dict(position_id=p["positionId"], day=probe.day(first["orderTime"])))
        if group == "2026-09_Short_x10_Isolated_LIMIT":
            assert first["orderTime"] == min(o["orderTime"] for o in entries)
            assert first["orderTime"] <= p["opened"] + 2 <= first["orderUpdateTime"] + 4
            cohort.append(
                dict(
                    p,
                    position_opened=p["opened"],
                    opened=first["orderTime"],
                    first_entry_order=first,
                    additional_entry_orders=entries[1:],
                )
            )
    previous_path = args.root / "shenming_audit_2026-11-07.json"
    previous = probe.read(previous_path)
    old_ids = {r["position_id"] for r in previous["rank_entries"]}
    assert len(old_ids) == 318 and previous["rank_top10"] == 43
    assert len(cohort) == 81 and not old_ids & {p["positionId"] for p in cohort}
    payload = dict(
        positions=cohort,
        total=81,
        complete=False,
        excluded_position_ids=sorted(set(by_id) - {p["positionId"] for p in cohort}),
        strata={
            k: dict(n=len(v), days=sorted({x["day"] for x in v}), positions=v)
            for k, v in strata.items()
        },
        old_rank_ids=sorted(old_ids),
        old_rank_top10=43,
        old_audit_sha256=probe.sha(previous_path),
        sources={p.name: probe.sha(p) for p in paths},
        script_sha256=probe.sha(Path(__file__)),
        scope="September first LIMIT SELL Short x10 Isolated; placement before execution",
    )
    # Shared research helpers use the original campaign's fixed artifact suffix.
    # Keep that interface while recording the actual freeze timestamp inside the split.
    helper_source = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not helper_source.exists()
    probe.save(destination, payload)
    probe.save(helper_source, payload)
    probe.PREFIX, probe.PID = PREFIX, PID
    probe.prepare(args.root)
    path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    split = probe.read(path)
    split["holdout_scope"] = (
        "New pre-placement minute features for September Short x10 Isolated LIMIT, "
        "disjoint from all 318 prior October Long rank tests. Five development days "
        "and three holdout days; other side/leverage/type/month strata not pooled. "
        "Initial limit price, amendments/cancellations, universe and full rule unknown."
    )
    probe.save(path, split)
    print({k: dict(n=len(v["sample"]), days=v["days"]) for k, v in split["phases"].items()})


if __name__ == "__main__":
    main()
