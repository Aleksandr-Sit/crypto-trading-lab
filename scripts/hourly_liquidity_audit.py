"""Offline necessary-condition check for minimum-turnover selection filters.

A stronger-return contract with at least as much turnover as the observed choice
cannot be removed by a minimum-turnover threshold that retains that choice.
No thresholds are fitted and no profitability is calculated.
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from hourly_automata_audit import HOUR, PIDS, digest, iso, read


def diagnose(root):
    universe = root / "hourly_universe1h_2026-11-07"
    metadata = read(universe / "symbols.json")
    manifest = read(universe / "manifest.json")
    source = root / "hourly_reconstruction_audit_2026-11-07.json"
    saved = read(source)["selection_diagnostics"]["diagnostics"]
    bars = {}
    hashes = {source.name: digest(source), Path(__file__).name: digest(Path(__file__))}
    for path in sorted(universe.glob("*.json")):
        hashes[path.name] = digest(path)
        if path.stem not in metadata:
            continue
        record = read(path)
        if record["complete"]:
            bars[path.stem] = {r[0]: r for r in record["rows"]}
    requested = defaultdict(list)
    for pid in PIDS:
        for entry in saved[pid]["entries"]:
            t = int(datetime.fromisoformat(entry["opened"]).timestamp() * 1000)
            requested[t // HOUR * HOUR].append((pid, entry))
    records = defaultdict(list)
    for t, entries in sorted(requested.items()):
        frame = {}
        for symbol, rows in bars.items():
            history = [rows.get(t - h * HOUR) for h in range(1, 50)]
            if any(r is None for r in history) or metadata[symbol]["onboard"] >= t:
                continue
            close = float(history[0][4])
            frame[symbol] = {
                **{f"r{h}h": close / float(history[h][4]) - 1 for h in (1, 4, 12, 24, 48)},
                "qv1h": float(history[0][7]),
                "qv24h": sum(float(r[7]) for r in history[:24]),
            }
        for pid, entry in entries:
            symbol = entry["symbol"]
            if symbol not in frame:
                continue
            if len(frame) != entry["universe_n"]:
                raise ValueError(f"Universe alignment mismatch: {pid} {t}")
            selected = frame[symbol]
            # Independently verify feature alignment with the previous audit.
            for name, value in selected.items():
                if abs(value - entry["features"][name]) > 1e-8 * max(1, abs(value)):
                    raise ValueError(f"Feature mismatch: {pid} {t} {name}")
            for h in (1, 4, 12, 24, 48):
                feature = f"r{h}h"
                stronger = [(s, v) for s, v in frame.items() if v[feature] > selected[feature]]
                blockers = {
                    mode: [s for s, v in stronger if all(v[q] >= selected[q] for q in fields)]
                    for mode, fields in {
                        "minimum_qv1h": ("qv1h",),
                        "minimum_qv24h": ("qv24h",),
                        "both_minimum_turnovers": ("qv1h", "qv24h"),
                    }.items()
                }
                records[pid].append(
                    dict(
                        opened=entry["opened"],
                        boundary=iso(t),
                        symbol=symbol,
                        lookback_hours=h,
                        universe_n=len(frame),
                        selected=selected,
                        stronger_n=len(stronger),
                        blockers={k: len(v) for k, v in blockers.items()},
                        example=next(
                            (
                                dict(symbol=s, **frame[s])
                                for s in blockers["both_minimum_turnovers"]
                            ),
                            None,
                        ),
                    )
                )
    summaries = {}
    for pid, rows in records.items():
        groups = {}
        for month in ("2026-09", "2026-10"):
            for h in (1, 4, 12, 24, 48):
                subset = [
                    r for r in rows if r["opened"].startswith(month) and r["lookback_hours"] == h
                ]
                groups[f"{month}/{h}h"] = dict(
                    n=len(subset),
                    already_max_return=sum(r["stronger_n"] == 0 for r in subset),
                    impossible={
                        mode: sum(r["blockers"][mode] > 0 for r in subset)
                        for mode in ("minimum_qv1h", "minimum_qv24h", "both_minimum_turnovers")
                    },
                )
        summaries[pid] = groups
    return dict(
        method="turnover_dominance_necessary_condition_v1",
        hashes=hashes,
        universe_failures=manifest["failures"],
        limitations=[
            "Exploratory September and October; neither is a new holdout.",
            "Incomplete historical universe; hourly boundary, not order-time quotes.",
            "Does not test spreads, depth, minimum notional, or quantity steps.",
            "Absence of blockers is necessary, not sufficient for a common threshold.",
        ],
        summary=summaries,
        entries=dict(records),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(args.root)
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
