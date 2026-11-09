"""Evaluate frozen placement states; keep execution feasibility as a separate outcome."""

import argparse
from fractions import Fraction
from pathlib import Path

import btctonghui_market_revisit as outcomes
from automata_return_condition_check import evaluate
from automata_revisit_probe import read, save, sha

PREFIX = "wetothemoon_first_revisit"


def audit(root, split, hypotheses):
    phases = {
        phase: evaluate(root, PREFIX, phase, split, hypotheses)
        for phase in ("development", "holdout")
    }
    execution = {}
    for phase, part in split["phases"].items():
        rows, sources = [], {}
        for position in part["sample"]:
            name = f"{position['positionId']}.json"
            path = root / f"{PREFIX}_outcomes_2026-11-08" / name
            sources[str(path.relative_to(root))] = sha(path)
            bars = read(path)["request"]["response"]
            order = position["first_entry_order"]
            price = Fraction(str(order["avgPrice"]))
            preplacement = read(root / f"{PREFIX}_market_{phase}_2026-11-08" / name)["bars"]
            low, high = min(Fraction(b[3]) for b in bars), max(Fraction(b[2]) for b in bars)
            rows.append(
                dict(
                    position_id=position["positionId"],
                    symbol=position["symbol"],
                    public_ohlc_feasible=low <= price <= high,
                    execution_vs_preplacement_close=float(
                        price / Fraction(preplacement[-1][4]) - 1
                    ),
                    placement_to_update_ms=order["orderUpdateTime"] - order["orderTime"],
                    update_minus_position_opened_ms=order["orderUpdateTime"]
                    - position["position_opened"],
                    observed_price=float(price),
                    public_low=float(low),
                    public_high=float(high),
                )
            )
        feasible = sum(row["public_ohlc_feasible"] for row in rows)
        execution[phase] = dict(
            rows=rows,
            sources=sources,
            summary=dict(
                n=len(rows),
                feasible_executions=feasible,
                infeasible_executions=len(rows) - feasible,
            ),
        )
    supported = [
        spec["id"]
        for spec in hypotheses["hypotheses"]
        if all(part["summary"][spec["id"]]["meets_frozen_screen"] for part in phases.values())
    ]
    feasible = all(part["summary"]["infeasible_executions"] == 0 for part in execution.values())
    return dict(
        status="entry_candidate" if supported and feasible else "rule_unidentified",
        supported_conditions=supported,
        public_execution_consistency=feasible,
        profitability_tested=False,
        hypotheses=hypotheses,
        hypotheses_sha256=sha(root / f"{PREFIX}_entry_hypotheses_2026-11-08.json"),
        split_sha256=sha(root / f"{PREFIX}_entry_split_2026-11-08.json"),
        script_sha256=sha(Path(__file__)),
        return_evaluator_sha256=sha(Path(__file__).with_name("automata_return_condition_check.py")),
        outcome_fetcher_sha256=sha(Path(outcomes.__file__)),
        phases=phases,
        execution=execution,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch-outcomes", "audit"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    split = read(args.root / f"{PREFIX}_entry_split_2026-11-08.json")
    hypotheses = read(args.root / f"{PREFIX}_entry_hypotheses_2026-11-08.json")
    assert hypotheses["split_sha256"] == sha(args.root / f"{PREFIX}_entry_split_2026-11-08.json")
    if args.command == "fetch-outcomes":
        # Reuse the archived outcome request format without changing the earlier audit code.
        outcomes.PREFIX = PREFIX
        outcomes.fetch_outcomes(args.root, split)
    else:
        assert args.out is not None
        report = audit(args.root, split, hypotheses)
        save(args.out, report)
        print(report["status"], report["supported_conditions"])
        for phase, part in report["phases"].items():
            for key, summary in part["summary"].items():
                print(phase, key, {k: v for k, v in summary.items() if k != "day_counts"})
            print(phase, "execution", report["execution"][phase]["summary"])


if __name__ == "__main__":
    main()
