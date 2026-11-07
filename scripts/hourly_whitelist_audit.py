"""Necessary-condition check for a fixed universe using only prior traded symbols.

No whitelist is fitted. A previously traded contract must be in any fixed list
that explains the account, even if the rest of that list is unknown.
"""

import argparse
import json
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from hourly_automata_audit import HOUR, PIDS, digest, iso, read


def audit(root):
    source = root / "hourly_reconstruction_audit_2026-11-07.json"
    saved = read(source)["selection_diagnostics"]["diagnostics"]
    hashes = {source.name: digest(source), Path(__file__).name: digest(Path(__file__))}
    prior, trade_times = {}, {}
    for pid in PIDS:
        path = root / f"hourly_{pid}_2026-11-07.json"
        hashes[path.name] = digest(path)
        first = {}
        times = defaultdict(list)
        for row in read(path)["positions"]:
            first[row["symbol"]] = min(first.get(row["symbol"], row["opened"]), row["opened"])
            times[row["symbol"]].append(row["opened"])
        prior[pid] = first
        trade_times[pid] = {s: sorted(v) for s, v in times.items()}
    universe = root / "hourly_universe1h_2026-11-07"
    bars = {}
    symbols = set().union(*(set(v) for v in prior.values()))
    for symbol in sorted(symbols):
        path = universe / f"{symbol}.json"
        if path.exists():
            hashes[f"candles/{path.name}"] = digest(path)
            record = read(path)
            if record["complete"]:
                bars[symbol] = {r[0]: r for r in record["rows"]}
    frames, records, summary = {}, defaultdict(list), {}
    for pid in PIDS:
        for entry in saved[pid]["entries"]:
            opened = int(datetime.fromisoformat(entry["opened"]).timestamp() * 1000)
            t = opened // HOUR * HOUR
            month_start = int(
                datetime.fromisoformat(entry["opened"])
                .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                .timestamp()
                * 1000
            )
            if t not in frames:
                frame = {}
                for symbol, rows in bars.items():
                    history = [rows.get(t - h * HOUR) for h in range(1, 50)]
                    if any(r is None for r in history):
                        continue
                    c = float(history[0][4])
                    frame[symbol] = {
                        **{f"r{h}h": c / float(history[h][4]) - 1 for h in (1, 4, 12, 24, 48)},
                        "qv1h": float(history[0][7]),
                        "qv24h": sum(float(r[7]) for r in history[:24]),
                    }
                frames[t] = frame
            frame = frames[t]
            if entry["symbol"] not in frame:
                raise ValueError("Selected symbol unexpectedly missing")
            selected = frame[entry["symbol"]]
            for name, value in selected.items():
                if abs(value - entry["features"][name]) > 1e-8 * max(1, abs(value)):
                    raise ValueError(f"Feature alignment failure: {pid} {t} {name}")
            eligible = {s: v for s, v in frame.items() if prior[pid].get(s, t) < t}
            for h in (1, 4, 12, 24, 48):
                stronger = [s for s, v in eligible.items() if v[f"r{h}h"] > selected[f"r{h}h"]]
                dominant = [
                    s
                    for s in stronger
                    if all(eligible[s][q] >= selected[q] for q in ("qv1h", "qv24h"))
                ]
                recent = {
                    s
                    for s in eligible
                    if bisect_left(trade_times[pid][s], t)
                    > bisect_left(trade_times[pid][s], month_start)
                }
                witness = next((s for s in dominant if s in recent), None)
                if witness is None:
                    witness = dominant[0] if dominant else (stronger[0] if stronger else None)
                records[pid].append(
                    dict(
                        opened=entry["opened"],
                        symbol=entry["symbol"],
                        lookback_hours=h,
                        known_prior_symbols=len(eligible),
                        stronger_prior_symbols=len(stronger),
                        turnover_dominant_prior_symbols=len(dominant),
                        stronger_prior_same_month=sum(s in recent for s in stronger),
                        dominant_prior_same_month=sum(s in recent for s in dominant),
                        example=dict(
                            symbol=witness,
                            first_prior_open=iso(prior[pid][witness]),
                            last_prior_open=iso(
                                trade_times[pid][witness][
                                    bisect_left(trade_times[pid][witness], t) - 1
                                ]
                            ),
                            features=eligible[witness],
                        )
                        if witness
                        else None,
                    )
                )
        groups = {}
        for month in ("2026-09", "2026-10"):
            for h in (1, 4, 12, 24, 48):
                rows = [
                    r
                    for r in records[pid]
                    if r["opened"].startswith(month) and r["lookback_hours"] == h
                ]
                groups[f"{month}/{h}h"] = dict(
                    n=len(rows),
                    fixed_list_only_impossible=sum(r["stronger_prior_symbols"] > 0 for r in rows),
                    fixed_list_and_minimum_turnovers_impossible=sum(
                        r["turnover_dominant_prior_symbols"] > 0 for r in rows
                    ),
                    fixed_within_month_list_impossible=sum(
                        r["stronger_prior_same_month"] > 0 for r in rows
                    ),
                    fixed_within_month_and_minimum_turnovers_impossible=sum(
                        r["dominant_prior_same_month"] > 0 for r in rows
                    ),
                )
        summary[pid] = groups
    delay_path = root / "hourly_delay_2026-11-07" / "audit.json"
    hashes["delay/audit.json"] = digest(delay_path)
    joint_cases = []
    for case in read(delay_path)["cases"]:
        first = prior[PIDS[2]].get(case["competitor"])
        known = first is not None and first < case["boundary"]
        times = trade_times[PIDS[2]].get(case["competitor"], [])
        month_start = int(
            datetime.fromisoformat(case["opened"])
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
            * 1000
        )
        recent = bisect_left(times, case["boundary"]) > bisect_left(times, month_start)
        joint_cases.append(
            dict(
                opened=case["opened"],
                symbol=case["symbol"],
                competitor=case["competitor"],
                first_prior_open=iso(first) if first is not None else None,
                last_prior_open=iso(times[bisect_left(times, case["boundary"]) - 1])
                if known
                else None,
                prior_to_decision=known,
                contradicts_fixed_list_and_delay=known
                and case["stronger_competitor_remains"] is True,
                contradicts_monthly_list_and_delay=recent
                and case["stronger_competitor_remains"] is True,
            )
        )
    joint_summary = {}
    for month in ("2026-09", "2026-10"):
        rows = [r for r in joint_cases if r["opened"].startswith(month)]
        joint_summary[month] = dict(
            preselected_pairs=len(rows),
            competitor_previously_traded=sum(r["prior_to_decision"] for r in rows),
            contradicts_fixed_list_and_delay=sum(
                r["contradicts_fixed_list_and_delay"] for r in rows
            ),
            contradicts_monthly_list_and_delay=sum(
                r["contradicts_monthly_list_and_delay"] for r in rows
            ),
        )
    profiles_path = root / "hourly_profiles_2026-11-07.json"
    hashes[profiles_path.name] = digest(profiles_path)
    profiles = read(profiles_path)
    descriptions = {}
    for pid in PIDS:
        response = profiles["responses"][pid]
        body = response["body"]
        if response["status"] != 200 or body.get("code") != "000000" or not body.get("data"):
            raise ValueError(f"Incomplete public profile: {pid}")
        descriptions[pid] = body["data"].get("description")
    return dict(
        method="prior_traded_symbols_fixed_universe_necessary_condition_v1",
        hashes=hashes,
        summary=summary,
        entries=dict(records),
        profile_fetched_at=profiles["fetched_at"],
        profile_descriptions=descriptions,
        joint_fixed_list_and_delay_summary=joint_summary,
        joint_fixed_list_and_delay_cases=joint_cases,
        limitations=[
            "Assumes a fixed list, not time-varying exclusions.",
            "Prior symbol must have opened strictly before the decision hour.",
            "Hourly closed prices; no order-time rankings or historical size rules.",
            "No blocker is necessary, not sufficient for reproducing the rule.",
            "September and October remain exploratory, no profitability computed.",
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
