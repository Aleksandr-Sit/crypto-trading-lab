"""Evaluate frozen first-placement states and audit execution-price feasibility."""

import argparse
import time
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from automata_return_condition_check import evaluate
from automata_revisit_probe import read, save, sha

PREFIX = "btctonghui_first_revisit"


def fetch_outcomes(root, split):
    import httpx

    folder = root / f"{PREFIX}_outcomes_2026-11-08"
    folder.mkdir(exist_ok=True)
    with httpx.Client(timeout=30) as client:
        for phase, part in split["phases"].items():
            for position in part["sample"]:
                path = folder / f"{position['positionId']}.json"
                if path.exists():
                    continue
                order = position["first_entry_order"]
                start = order["orderTime"] // 60000 * 60000
                end = order["orderUpdateTime"] // 60000 * 60000 + 59999
                params = dict(
                    symbol=position["symbol"],
                    interval="1m",
                    startTime=start,
                    endTime=end,
                    limit=1500,
                )
                for attempt in range(4):
                    record = dict(params=params, fetched_at=datetime.now(UTC).isoformat())
                    try:
                        response = client.get(
                            "https://fapi.binance.com/fapi/v1/klines", params=params
                        )
                        response.raise_for_status()
                        bars = response.json()
                        assert isinstance(bars, list) and len(bars) == (end - start + 1) // 60000
                        assert all(b[0] == start + i * 60000 for i, b in enumerate(bars))
                        record["response"] = bars
                        break
                    except (httpx.HTTPError, AssertionError, ValueError) as exc:
                        record["error"] = str(exc)
                        save(
                            folder / f"error_{position['positionId']}_{time.time_ns()}.json", record
                        )
                        time.sleep(2 * (attempt + 1))
                else:
                    raise RuntimeError("Outcome fetch failed")
                save(path, dict(phase=phase, position_id=position["positionId"], request=record))
                print(phase, position["symbol"], "outcome", len(bars), flush=True)
                time.sleep(0.15)


def audit(root, split, hypotheses):
    phases = {
        phase: evaluate(root, PREFIX, phase, split, hypotheses)
        for phase in ("development", "holdout")
    }
    pricing = {}
    spec = hypotheses["price_hypothesis"]
    for phase, part in split["phases"].items():
        rows, sources = [], {}
        for position in part["sample"]:
            name = f"{position['positionId']}.json"
            market_path = root / f"{PREFIX}_market_{phase}_2026-11-08" / name
            outcome_path = root / f"{PREFIX}_outcomes_2026-11-08" / name
            sources[str(outcome_path.relative_to(root))] = sha(outcome_path)
            previous = Fraction(read(market_path)["bars"][-1][4])
            order = position["first_entry_order"]
            price = Fraction(str(order["avgPrice"]))
            ratio = price / previous
            bars = read(outcome_path)["request"]["response"]
            low = min(Fraction(b[3]) for b in bars)
            high = max(Fraction(b[2]) for b in bars)
            rows.append(
                dict(
                    position_id=position["positionId"],
                    symbol=position["symbol"],
                    first_fill_vs_previous_close=float(ratio - 1),
                    price_signature_hit=abs(ratio - Fraction(spec["target_ratio"]))
                    <= Fraction(spec["absolute_ratio_tolerance"]),
                    public_ohlc_feasible=low <= price <= high,
                    observed_price=float(price),
                    public_low=float(low),
                    public_high=float(high),
                    placement_to_update_ms=order["orderUpdateTime"] - order["orderTime"],
                )
            )
        hits = sum(row["price_signature_hit"] for row in rows)
        feasible = sum(row["public_ohlc_feasible"] for row in rows)
        pricing[phase] = dict(
            rows=rows,
            sources=sources,
            summary=dict(
                n=len(rows),
                signature_hits=hits,
                signature_coverage=hits / len(rows),
                feasible_executions=feasible,
                infeasible_executions=len(rows) - feasible,
                meets_signature_coverage=hits / len(rows) >= spec["min_coverage"],
            ),
        )
    supported = [
        spec["id"]
        for spec in hypotheses["hypotheses"]
        if all(part["summary"][spec["id"]]["meets_frozen_screen"] for part in phases.values())
    ]
    all_feasible = all(part["summary"]["infeasible_executions"] == 0 for part in pricing.values())
    return dict(
        status="entry_candidate" if supported and all_feasible else "rule_unidentified",
        supported_preplacement_conditions=supported,
        price_signature_replicated=all(
            part["summary"]["meets_signature_coverage"] for part in pricing.values()
        ),
        public_execution_consistency=all_feasible,
        profitability_tested=False,
        hypotheses=hypotheses,
        hypotheses_sha256=sha(root / f"{PREFIX}_entry_hypotheses_2026-11-08.json"),
        split_sha256=sha(root / f"{PREFIX}_entry_split_2026-11-08.json"),
        script_sha256=sha(Path(__file__)),
        return_evaluator_sha256=sha(Path(__file__).with_name("automata_return_condition_check.py")),
        phases=phases,
        pricing=pricing,
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
        fetch_outcomes(args.root, split)
    else:
        assert args.out is not None
        report = audit(args.root, split, hypotheses)
        save(args.out, report)
        print(
            {
                key: value
                for key, value in report.items()
                if key
                in (
                    "status",
                    "supported_preplacement_conditions",
                    "price_signature_replicated",
                    "public_execution_consistency",
                )
            }
        )
        for phase in report["phases"]:
            print(phase, report["phases"][phase]["summary"])
            print(phase, "pricing", report["pricing"][phase]["summary"])


if __name__ == "__main__":
    main()
