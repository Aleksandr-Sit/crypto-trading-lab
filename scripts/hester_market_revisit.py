"""Exploratory BTC context of independent basket openings; no holdout claim."""

import argparse
from collections import Counter
from fractions import Fraction
from pathlib import Path

from automata_revisit_probe import day, read, save, sha

PREFIX = "hester_basket_revisit"


def baskets(positions):
    groups = []
    for position in sorted(positions, key=lambda p: (p["opened"], p["positionId"])):
        if (
            not groups
            or position["side"] != groups[-1][-1]["side"]
            or position["opened"] - groups[-1][-1]["opened"] > 600000
        ):
            groups.append([])
        groups[-1].append(position)
    return groups


def sign(value):
    return 1 if value > 0 else -1 if value < 0 else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    source = args.root / "hester_5127540263600794368_merged_2026-11-07.json"
    cohort_path = args.root / f"{PREFIX}_5127540263600794368_2026-11-08.json"
    split_path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    cohort, split = read(cohort_path), read(split_path)
    assert cohort["source_sha256"] == sha(source)
    assert split["source_sha256"] == sha(cohort_path) and split["exploration_only"]
    assert split["phases"]["holdout"]["sample"] == []
    groups = baskets(read(source)["positions"])
    large = [g for g in groups if len(g) >= 30]
    assert len(large) == len(cohort["positions"]) == 11
    rows, sources = [], {}
    for group, position in zip(large, cohort["positions"], strict=True):
        assert [p["positionId"] for p in group] == position["episode_position_ids"]
        assert position["positionId"] == group[0]["positionId"]
        path = (
            args.root / f"{PREFIX}_market_development_2026-11-08" / f"{position['positionId']}.json"
        )
        sources[str(path.relative_to(args.root))] = sha(path)
        data = read(path)
        assert data["symbol"] == "BTCUSDT"
        bars = data["bars"]
        assert len(bars) == 2881 and bars[-1][6] < position["opened"] // 60000 * 60000
        features, controls = {}, {}
        for lag in (1, 5, 15, 60, 1440):
            key = f"return_{lag}m"
            j = len(bars) - 1
            features[key] = float(Fraction(bars[j][4]) / Fraction(bars[j - lag][4]) - 1)
            controls[key] = [
                float(Fraction(bars[j - hour * 60][4]) / Fraction(bars[j - hour * 60 - lag][4]) - 1)
                for hour in range(2, 25, 2)
            ]
        rows.append(
            dict(
                first_position_id=position["positionId"],
                side=position["side"],
                first_opened=group[0]["opened"],
                last_opened=group[-1]["opened"],
                n=len(group),
                btc_features=features,
                background=controls,
            )
        )
    summaries = {}
    for side in ("Short", "Long"):
        subset = [r for r in rows if r["side"] == side]
        direction = 1 if side == "Long" else -1
        summaries[side] = {}
        for lag in (1, 5, 15, 60, 1440):
            key = f"return_{lag}m"
            summaries[side][key] = dict(
                episodes=len(subset),
                momentum_matches=sum(sign(r["btc_features"][key]) == direction for r in subset),
                contrarian_matches=sum(sign(r["btc_features"][key]) == -direction for r in subset),
                zero_returns=sum(r["btc_features"][key] == 0 for r in subset),
                background_n=len(subset) * 12,
                background_momentum_reference_matches=sum(
                    sign(v) == direction for r in subset for v in r["background"][key]
                ),
            )
    first_days = set(split["prospective_chronological_split"]["first_days"])
    early = [r for r in rows if day(r["first_opened"]) in first_days]
    late = [r for r in rows if day(r["first_opened"]) not in first_days]
    report = dict(
        status="rule_unidentified",
        stage="exploratory_market_context_only",
        validation_performed=False,
        profitability_tested=False,
        analysis_unit="basket cluster, not individual position; independence not established",
        source_sha256=sha(source),
        cohort_sha256=sha(cohort_path),
        split_sha256=sha(split_path),
        script_sha256=sha(Path(__file__)),
        market_script_sha256=sha(Path(__file__).with_name("automata_revisit_probe.py")),
        sources=sources,
        rows=rows,
        summaries=summaries,
        grouping=dict(
            total_positions=4028,
            all_groups=len(groups),
            large_groups=len(large),
            large_group_positions=sum(map(len, large)),
            small_group_sizes=[len(g) for g in groups if len(g) < 30],
        ),
        prospective_split=dict(
            early_sides=dict(Counter(r["side"] for r in early)),
            late_sides=dict(Counter(r["side"] for r in late)),
            late_always_long_baseline=1.0,
        ),
        validation_limitation=split["validation_not_run_reason"],
    )
    save(args.out, report)
    print(report["grouping"], report["prospective_split"])
    print(summaries)


if __name__ == "__main__":
    main()
