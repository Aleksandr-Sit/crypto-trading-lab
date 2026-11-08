"""Check frozen direction hypotheses at known entries, with matched time controls."""

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path
from statistics import mean

from automata_revisit_probe import read, save, sha


def prediction(bars, index, lag, mode):
    difference = Decimal(bars[index][4]) - Decimal(bars[index - lag][4])
    if difference == 0:
        return None
    positive = difference > 0
    if mode == "contrarian":
        positive = not positive
    return "Long" if positive else "Short"


def evaluate(root, prefix, phase, split, hypotheses):
    positions = split["phases"][phase]["sample"]
    sources, rows = {}, []
    for position in positions:
        path = root / f"{prefix}_market_{phase}_2026-11-08" / f"{position['positionId']}.json"
        sources[str(path.relative_to(root))] = sha(path)
        data = read(path)
        bars = data["bars"]
        assert len(bars) == 2881 and bars[-1][6] < position["opened"] // 60000 * 60000
        tests = {}
        for spec in hypotheses["hypotheses"]:
            predicted = prediction(bars, len(bars) - 1, spec["lag_minutes"], spec["mode"])
            controls = [
                prediction(bars, len(bars) - 1 - hours * 60, spec["lag_minutes"], spec["mode"])
                for hours in range(2, 25, 2)
            ]
            tests[spec["id"]] = dict(
                predicted=predicted,
                agrees=predicted == position["side"],
                background_predictions=controls,
                background_agrees=sum(p == position["side"] for p in controls),
            )
        rows.append(
            dict(
                position_id=position["positionId"],
                symbol=position["symbol"],
                side=position["side"],
                day=position["opened"] // 86400000,
                tests=tests,
            )
        )
    sides = Counter(row["side"] for row in rows)
    assert set(sides) == {"Long", "Short"}, "Direction comparison requires both sides"
    majority = max(sides.values()) / len(rows)
    summaries = {}
    for spec in hypotheses["hypotheses"]:
        key = spec["id"]
        correct = sum(row["tests"][key]["agrees"] for row in rows)
        coverage = sum(row["tests"][key]["predicted"] is not None for row in rows) / len(rows)
        agreement = correct / len(rows)
        class_recalls = {
            side: sum(row["tests"][key]["agrees"] for row in rows if row["side"] == side) / n
            for side, n in sides.items()
        }
        balanced = mean(class_recalls.values())
        background_n = 12 * len(rows)
        background_hits = sum(row["tests"][key]["background_agrees"] for row in rows)
        background_agreement = background_hits / background_n
        day_agreements = {
            str(date): dict(n=len(g), agrees=sum(row["tests"][key]["agrees"] for row in g))
            for date in sorted({row["day"] for row in rows})
            for g in [[row for row in rows if row["day"] == date]]
        }
        criteria = hypotheses["candidate_criteria"]
        passes = (
            coverage >= criteria["min_sample_coverage"]
            and agreement >= criteria["min_agreement"]
            and balanced >= criteria["min_balanced_accuracy"]
            and agreement - majority >= criteria["min_advantage_over_majority"]
            and agreement - background_agreement
            >= criteria["min_advantage_over_matched_background"]
        )
        summaries[key] = dict(
            n=len(rows),
            agrees=correct,
            coverage=coverage,
            agreement=agreement,
            balanced_accuracy=balanced,
            recalls=class_recalls,
            majority_side_agreement=majority,
            background_n=background_n,
            background_agrees=background_hits,
            background_reference_agreement=background_agreement,
            advantage_over_majority=agreement - majority,
            advantage_over_background=agreement - background_agreement,
            day_counts=day_agreements,
            macro_day_agreement=mean(
                value["agrees"] / value["n"] for value in day_agreements.values()
            ),
            meets_frozen_screen=passes,
        )
    return dict(sides=dict(sides), sources=sources, rows=rows, summary=summaries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--prefix", default="bncopy16")
    args = parser.parse_args()
    split_path = args.root / f"{args.prefix}_entry_split_2026-11-08.json"
    hypothesis_path = args.root / f"{args.prefix}_entry_hypotheses_2026-11-08.json"
    split, hypotheses = read(split_path), read(hypothesis_path)
    assert hypotheses["split_sha256"] == sha(split_path)
    phases = {
        phase: evaluate(args.root, args.prefix, phase, split, hypotheses)
        for phase in ("development", "holdout")
    }
    supported = [
        spec["id"]
        for spec in hypotheses["hypotheses"]
        if all(phases[phase]["summary"][spec["id"]]["meets_frozen_screen"] for phase in phases)
    ]
    report = dict(
        status="entry_candidate" if supported else "rule_unidentified",
        scope="Direction at observed entries only; background has no ground-truth side labels",
        supported_direction_candidates=supported,
        profitability_tested=False,
        hypotheses=hypotheses,
        hypotheses_sha256=sha(hypothesis_path),
        split_sha256=sha(split_path),
        script_sha256=sha(Path(__file__)),
        market_script_sha256=sha(Path(__file__).with_name("automata_revisit_probe.py")),
        phases=phases,
    )
    save(args.out, report)
    print({"status": report["status"], "supported": supported})
    for phase, values in phases.items():
        for key, summary in values["summary"].items():
            print(
                phase, key, {k: v for k, v in summary.items() if k not in ("day_counts", "recalls")}
            )


if __name__ == "__main__":
    main()
