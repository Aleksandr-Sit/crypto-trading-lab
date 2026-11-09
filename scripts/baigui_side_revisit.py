"""Evaluate frozen direction predictions with separate Long/Short coverage."""

import argparse
from collections import Counter
from fractions import Fraction
from pathlib import Path
from statistics import mean

from automata_revisit_probe import read, save, sha

PREFIX = "baigui_side_revisit"


def matches(bars, index, side, spec):
    difference = Fraction(bars[index][4]) - Fraction(bars[index - spec["lag_minutes"]][4])
    if difference == 0:
        return False
    prediction = "Long" if difference > 0 else "Short"
    if spec["relationship"] == "reversal":
        prediction = "Short" if prediction == "Long" else "Long"
    else:
        assert spec["relationship"] == "continuation"
    return prediction == side


def evaluate(root, phase, split, hypotheses):
    rows, sources = [], {}
    for position in split["phases"][phase]["sample"]:
        path = root / f"{PREFIX}_market_{phase}_2026-11-08" / f"{position['positionId']}.json"
        sources[str(path.relative_to(root))] = sha(path)
        bars = read(path)["bars"]
        assert len(bars) == 2881 and bars[-1][6] < position["opened"] // 60000 * 60000
        assert position["opened"] == position["first_entry_order"]["orderTime"]
        tests = {}
        for spec in hypotheses["hypotheses"]:
            background = [
                matches(bars, 2880 - hours * 60, position["side"], spec)
                for hours in range(2, 25, 2)
            ]
            tests[spec["id"]] = dict(
                entry_hit=matches(bars, 2880, position["side"], spec),
                background=background,
                background_hits=sum(background),
            )
        rows.append(
            dict(
                position_id=position["positionId"],
                symbol=position["symbol"],
                side=position["side"],
                day=position["opened"] // 86400000,
                decision_time=position["opened"],
                first_fill_vs_previous_close=float(
                    Fraction(str(position["first_entry_order"]["avgPrice"])) / Fraction(bars[-1][4])
                    - 1
                ),
                additional_entry_order_count=len(position["additional_entry_orders"]),
                tests=tests,
            )
        )
    summaries = {}
    criteria = hypotheses["candidate_criteria"]
    for spec in hypotheses["hypotheses"]:
        key = spec["id"]
        hits = sum(row["tests"][key]["entry_hit"] for row in rows)
        coverage = hits / len(rows)
        background_hits = sum(row["tests"][key]["background_hits"] for row in rows)
        background_rate = background_hits / (len(rows) * 12)
        ratio = coverage / background_rate if background_rate else None
        days = {
            str(day): dict(n=len(group), hits=sum(row["tests"][key]["entry_hit"] for row in group))
            for day in sorted({row["day"] for row in rows})
            for group in [[row for row in rows if row["day"] == day]]
        }
        by_side = {
            side: dict(
                n=len(group),
                hits=sum(row["tests"][key]["entry_hit"] for row in group),
                coverage=mean(row["tests"][key]["entry_hit"] for row in group),
            )
            for side in ("Long", "Short")
            for group in [[row for row in rows if row["side"] == side]]
        }
        macro = mean(v["hits"] / v["n"] for v in days.values())
        passes = (
            coverage >= criteria["min_entry_coverage"]
            and coverage - background_rate >= criteria["min_advantage_over_background"]
            and ratio is not None
            and ratio >= criteria["min_frequency_ratio"]
            and macro >= criteria["min_macro_day_coverage"]
            and all(v["coverage"] >= criteria["min_side_coverage"] for v in by_side.values())
        )
        summaries[key] = dict(
            n=len(rows),
            hits=hits,
            coverage=coverage,
            background_n=len(rows) * 12,
            background_hits=background_hits,
            background_rate=background_rate,
            advantage_over_background=coverage - background_rate,
            frequency_ratio=ratio,
            day_counts=days,
            macro_day_coverage=macro,
            by_side=by_side,
            meets_frozen_screen=passes,
        )
    counts = Counter(row["side"] for row in rows)
    return dict(
        sources=sources,
        rows=rows,
        summary=summaries,
        side_counts=dict(counts),
        majority_side_baseline=max(counts.values()) / len(rows),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    split_path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    hypotheses_path = args.root / f"{PREFIX}_entry_hypotheses_2026-11-08.json"
    split, hypotheses = read(split_path), read(hypotheses_path)
    assert hypotheses["split_sha256"] == sha(split_path)
    phases = {
        phase: evaluate(args.root, phase, split, hypotheses) for phase in ("development", "holdout")
    }
    supported = [
        spec["id"]
        for spec in hypotheses["hypotheses"]
        if all(part["summary"][spec["id"]]["meets_frozen_screen"] for part in phases.values())
    ]
    report = dict(
        status="direction_candidate" if supported else "rule_unidentified",
        supported_conditions=supported,
        profitability_tested=False,
        symbol_selection_recovered=False,
        hypotheses=hypotheses,
        hypotheses_sha256=sha(hypotheses_path),
        split_sha256=sha(split_path),
        script_sha256=sha(Path(__file__)),
        phases=phases,
    )
    save(args.out, report)
    print(report["status"], supported)
    for phase, part in phases.items():
        print(phase, "sides", part["side_counts"], "baseline", part["majority_side_baseline"])
        for key, summary in part["summary"].items():
            print(phase, key, {k: v for k, v in summary.items() if k != "day_counts"})


if __name__ == "__main__":
    main()
