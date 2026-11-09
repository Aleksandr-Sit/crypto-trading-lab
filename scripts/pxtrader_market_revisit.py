"""Evaluate PXTRADER conditions before position opening; order timing is unknown."""

import argparse
from fractions import Fraction
from pathlib import Path
from statistics import mean

from automata_revisit_probe import read, save, sha


def condition(bars, index, spec):
    for clause in spec["clauses"]:
        feature = clause["feature"]
        assert feature.startswith("return_") and feature.endswith("m")
        lag = int(feature[7:-1])
        assert lag in (1, 5, 15, 60, 1440)
        value = Fraction(bars[index][4]) / Fraction(bars[index - lag][4]) - 1
        threshold = Fraction(clause["threshold"])
        operator = clause["operator"]
        assert operator in ("gt", "lt", "gte", "lte")
        passes = {
            "gt": value > threshold,
            "lt": value < threshold,
            "gte": value >= threshold,
            "lte": value <= threshold,
        }[operator]
        if not passes:
            return False
    return True


def evaluate(root, prefix, phase, split, hypotheses):
    rows, sources = [], {}
    for position in split["phases"][phase]["sample"]:
        path = root / f"{prefix}_market_{phase}_2026-11-08" / f"{position['positionId']}.json"
        sources[str(path.relative_to(root))] = sha(path)
        bars = read(path)["bars"]
        assert position["timing_basis"] == "position_opened_proxy"
        assert len(bars) == 2881 and bars[-1][6] < position["opened"] // 60000 * 60000
        index = len(bars) - 1
        tests = {}
        for spec in hypotheses["hypotheses"]:
            background = [condition(bars, index - hour * 60, spec) for hour in range(2, 25, 2)]
            tests[spec["id"]] = dict(
                entry_hit=condition(bars, index, spec),
                background=background,
                background_hits=sum(background),
            )
        rows.append(
            dict(
                position_id=position["positionId"],
                symbol=position["symbol"],
                side=position["side"],
                position_opened_proxy=position["opened"],
                day=position["opened"] // 86400000,
                tests=tests,
            )
        )
    summaries = {}
    for spec in hypotheses["hypotheses"]:
        key = spec["id"]
        hits = sum(r["tests"][key]["entry_hit"] for r in rows)
        coverage = hits / len(rows)
        bg_n = len(rows) * 12
        bg_hits = sum(r["tests"][key]["background_hits"] for r in rows)
        bg_rate = bg_hits / bg_n
        ratio = coverage / bg_rate if bg_rate else None
        days = {
            str(date): dict(n=len(g), hits=sum(r["tests"][key]["entry_hit"] for r in g))
            for date in sorted({r["day"] for r in rows})
            for g in [[r for r in rows if r["day"] == date]]
        }
        macro = mean(v["hits"] / v["n"] for v in days.values())
        criteria = hypotheses["candidate_criteria"]
        passes = (
            coverage >= criteria["min_entry_coverage"]
            and coverage - bg_rate >= criteria["min_advantage_over_background"]
            and ratio is not None
            and ratio >= criteria["min_frequency_ratio"]
            and macro >= criteria["min_macro_day_coverage"]
        )
        summaries[key] = dict(
            n=len(rows),
            hits=hits,
            coverage=coverage,
            background_n=bg_n,
            background_hits=bg_hits,
            background_rate=bg_rate,
            advantage_over_background=coverage - bg_rate,
            frequency_ratio=ratio,
            day_counts=days,
            macro_day_coverage=macro,
            meets_frozen_screen=passes,
        )
    return dict(sources=sources, rows=rows, summary=summaries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--prefix", default="pxtrader_early_revisit")
    args = parser.parse_args()
    split_path = args.root / f"{args.prefix}_entry_split_2026-11-08.json"
    hypotheses_path = args.root / f"{args.prefix}_entry_hypotheses_2026-11-08.json"
    split, hypotheses = read(split_path), read(hypotheses_path)
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
        full_rule_status="rule_unidentified",
        supported_conditions=supported,
        profitability_tested=False,
        order_timing_known=False,
        symbol_selection_recovered=False,
        scope=hypotheses["scope"],
        hypotheses=hypotheses,
        hypotheses_sha256=sha(hypotheses_path),
        split_sha256=sha(split_path),
        script_sha256=sha(Path(__file__)),
        market_script_sha256=sha(Path(__file__).with_name("automata_revisit_probe.py")),
        phases=phases,
    )
    save(args.out, report)
    print({"status": report["status"], "supported": supported})
    for phase, part in phases.items():
        for key, summary in part["summary"].items():
            print(phase, key, {k: v for k, v in summary.items() if k != "day_counts"})


if __name__ == "__main__":
    main()
