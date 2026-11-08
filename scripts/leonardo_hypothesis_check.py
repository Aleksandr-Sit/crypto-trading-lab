"""Evaluate frozen fill-association hypotheses; do not infer trading profitability."""

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from statistics import median

from leonardo_entry_probe import features, read, save, sha


def evaluate(root, phase, hypotheses):
    split = read(root / "leonardo_entry_split_2026-11-08.json")
    sample = split["phases"][phase]["sample"]
    rows = []
    sources = {}
    for position in sample:
        pid = position["positionId"]
        market = root / f"leonardo_market_{phase}_2026-11-08" / f"{pid}.json"
        order_path = root / f"leonardo_orders_{phase}_2026-11-08" / f"{pid}.json"
        sources[str(market.relative_to(root))] = sha(market)
        sources[str(order_path.relative_to(root))] = sha(order_path)
        bars = read(market)["bars"]
        orders = read(order_path)["response"]["data"]["list"]
        matching = [
            o
            for o in orders
            if o["symbol"] == position["symbol"]
            and o["side"] == "BUY"
            and o["positionSide"] in ("LONG", "BOTH")
            and abs(o["orderUpdateTime"] - position["opened"]) <= 10
        ]
        first = matching[0] if len(matching) == 1 else None
        index = len(bars) - 1
        entry_features = features(bars, index)
        tests = {}
        for hypothesis in hypotheses["hypotheses"]:
            background = []
            if hypothesis["kind"] == "completed_minute":

                def condition(value, spec=hypothesis):
                    return (
                        value["return_1m"] <= spec["return_1m_lte"]
                        and value["relative_volume_1m"] is not None
                        and value["relative_volume_1m"] >= spec["relative_volume_gte"]
                    )

                hit = condition(entry_features)
                background = [
                    condition(features(bars, index - hours * 60)) for hours in range(2, 25, 2)
                ]
            else:
                factor = 1 - Decimal(str(hypothesis["drop"]))
                hit = (
                    Decimal(str(first["avgPrice"])) <= Decimal(str(bars[index][4])) * factor
                    if first
                    else None
                )
                for hours in range(2, 25, 2):
                    j = index - hours * 60
                    # Known prior close sets a hypothetical limit level; next-bar low
                    # is the observed event outcome, not a decision-time feature.
                    background.append(
                        Decimal(str(bars[j + 1][3])) <= Decimal(str(bars[j][4])) * factor
                    )
            tests[hypothesis["id"]] = dict(
                entry_hit=hit, background_hits=sum(background), background_n=len(background)
            )
        placed_bar = None
        if first:
            placed_stamp = first["orderTime"] // 60000 * 60000 - 60000
            placed_bar = next((b for b in bars if b[0] == placed_stamp), None)
        rows.append(
            dict(
                position_id=pid,
                symbol=position["symbol"],
                day=position["opened"] // 86400000,
                first_order=first,
                first_order_matches=len(matching),
                first_order_covers_closed_volume=(
                    Decimal(str(first["executedQty"])) == Decimal(str(position["closedVolume"]))
                )
                if first
                else None,
                previous_closed_minute_close=bars[index][4],
                previous_closed_minute_start=bars[index][0],
                placement_previous_close=placed_bar[4] if placed_bar else None,
                tests=tests,
            )
        )
    summaries = {}
    for hypothesis in hypotheses["hypotheses"]:
        key = hypothesis["id"]
        valid = [r for r in rows if r["tests"][key]["entry_hit"] is not None]
        hits = sum(r["tests"][key]["entry_hit"] for r in valid)
        bg_n = sum(r["tests"][key]["background_n"] for r in valid)
        bg_hits = sum(r["tests"][key]["background_hits"] for r in valid)
        coverage = hits / len(valid) if valid else None
        lower_bound = hits / len(rows)
        upper_bound = (hits + len(rows) - len(valid)) / len(rows)
        bg_rate = bg_hits / bg_n if bg_n else None
        ratio = coverage / bg_rate if bg_rate and coverage is not None else None
        conservative_ratio = lower_bound / bg_rate if bg_rate else None
        criteria = hypotheses["candidate_criteria"]
        summaries[key] = dict(
            entries=len(valid),
            entry_hits=hits,
            entry_coverage=coverage,
            coverage_lower_bound=lower_bound,
            coverage_upper_bound=upper_bound,
            unknown_entries=len(rows) - len(valid),
            background_n=bg_n,
            background_hits=bg_hits,
            background_rate=bg_rate,
            frequency_ratio=ratio,
            matched_days=len({r["day"] for r in valid if r["tests"][key]["entry_hit"]}),
            sampled_days=len({r["day"] for r in valid}),
            day_counts={
                str(date): dict(n=len(g), hits=sum(r["tests"][key]["entry_hit"] for r in g))
                for date in sorted({r["day"] for r in valid})
                for g in [[r for r in valid if r["day"] == date]]
            },
            meets_candidate_screen=(
                conservative_ratio is not None
                and lower_bound >= criteria["min_entry_coverage"]
                and conservative_ratio >= criteria["min_entry_to_background_ratio"]
            ),
        )
    delays = [
        (r["first_order"]["orderUpdateTime"] - r["first_order"]["orderTime"]) / 1000
        for r in rows
        if r["first_order"]
    ]
    return dict(
        phase=phase,
        sources=sources,
        rows=rows,
        summary=summaries,
        first_order_types=dict(Counter(r["first_order"]["type"] for r in rows if r["first_order"])),
        placement_to_fill_s=dict(min=min(delays), median=median(delays), max=max(delays)),
        first_order_covers_closed_volume=sum(
            r["first_order_covers_closed_volume"] is True for r in rows
        ),
        first_fill_association_only=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    path = args.root / "leonardo_entry_hypotheses_2026-11-08.json"
    hypotheses = read(path)
    assert hypotheses["split_sha256"] == sha(args.root / "leonardo_entry_split_2026-11-08.json")
    report = dict(
        status="rule_unidentified",
        profitability_tested=False,
        hypotheses=hypotheses,
        hypotheses_sha256=sha(path),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        feature_script_sha256=sha(Path(__file__).with_name("leonardo_entry_probe.py")),
        phases={
            phase: evaluate(args.root, phase, hypotheses) for phase in ("development", "holdout")
        },
    )
    if any(
        all(
            report["phases"][phase]["summary"][spec["id"]]["meets_candidate_screen"]
            for phase in ("development", "holdout")
        )
        for spec in hypotheses["hypotheses"]
    ):
        report["status"] = "entry_candidate"
    save(args.out, report)
    print(
        json.dumps(
            {phase: report["phases"][phase]["summary"] for phase in report["phases"]}, indent=2
        )
    )


if __name__ == "__main__":
    main()
