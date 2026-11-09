"""Freeze early Long x2 position-time proxies, excluding the August basket."""

import argparse
from pathlib import Path

import automata_revisit_probe as probe

PID = "4810377016221347072"
PREFIX = "pxtrader_early_revisit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"pxtrader_{PID}{suffix}.json"
        for suffix in ("_scan_2026-10-25", "_2026-11-07", "_orders_2026-11-07")
    ]
    old, current, orders = map(probe.read, paths)
    original = {p["positionId"]: p for p in old["rows"]}
    updated = {p["positionId"]: p for p in current["positions"]}
    assert not updated.keys() - original.keys()
    assert all(original[k] == p for k, p in updated.items())
    assert not orders["orders"]
    early, late = [], []
    for p in original.values():
        if probe.day(p["opened"]) < "2026-08-01":
            assert p["side"] == "Long" and p["leverage"] == "2" and p["isolated"] == "Cross"
            # Exclude the opening minute. Position time is NOT order placement time.
            early.append(dict(p, position_opened=p["opened"], timing_basis="position_opened_proxy"))
        else:
            assert p["side"] == "Short"
            late.append(p["positionId"])
    assert len(early) == 109 and len(late) == 22
    destination = args.root / f"{PREFIX}_{PID}_2026-11-08.json"
    assert not destination.exists(), "Cohort already frozen"
    probe.save(
        destination,
        dict(
            positions=early,
            total=109,
            complete=False,
            excluded_position_ids=late,
            sources={p.name: probe.sha(p) for p in paths},
            script_sha256=probe.sha(Path(__file__)),
            scope="Early Long x2 Cross; position opening proxy, no order evidence",
        ),
    )
    probe.PREFIX, probe.PID = PREFIX, PID
    probe.prepare(args.root)
    path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    split = probe.read(path)
    split["holdout_scope"] = (
        "Unseen completed-bar features before position opening, NOT before known order placement. "
        "Execution timing, order type, adds and initial limit price unknown. "
        "August Short basket excluded; one active day, no independent validation."
    )
    probe.save(path, split)
    print({k: v["days"] for k, v in split["phases"].items()})


if __name__ == "__main__":
    main()
