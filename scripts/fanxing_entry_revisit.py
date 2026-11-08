"""Frozen pre-order Short-entry conditions, with BTC as a separate market reference."""

import argparse
from decimal import Decimal
from pathlib import Path
from statistics import mean

from automata_revisit_probe import read, save, sha

PREFIX = "fanxing_short_revisit"
BENCHMARK = "fanxing_benchmark_revisit"


def condition(own, btc, index, feature):
    close, previous = Decimal(own[index][4]), Decimal(own[index - 60][4])
    if feature == "coin_return_60m":
        return close > previous
    assert feature == "coin_minus_btc_return_60m"
    return close * Decimal(btc[index - 60][4]) > Decimal(btc[index][4]) * previous


def evaluate(root, phase, split, benchmark_split, hypotheses):
    positions = split["phases"][phase]["sample"]
    references = benchmark_split["phases"][phase]["sample"]
    assert [p["positionId"] for p in positions] == [p["positionId"] for p in references]
    sources, rows = {}, []
    for position, reference in zip(positions, references, strict=True):
        pid = position["positionId"]
        own_path = root / f"{PREFIX}_market_{phase}_2026-11-08" / f"{pid}.json"
        btc_path = root / f"{BENCHMARK}_market_{phase}_2026-11-08" / f"{pid}.json"
        sources[str(own_path.relative_to(root))] = sha(own_path)
        sources[str(btc_path.relative_to(root))] = sha(btc_path)
        own, btc = read(own_path)["bars"], read(btc_path)["bars"]
        assert len(own) == len(btc) == 2881
        assert [bar[0] for bar in own] == [bar[0] for bar in btc]
        assert position["side"] == "Short" and reference["symbol"] == "BTCUSDT"
        order = position["first_entry_order"]
        assert order["type"] == "MARKET" and order["side"] == "SELL"
        assert order["orderTime"] == position["opened"] <= order["orderUpdateTime"]
        assert own[-1][6] < order["orderTime"] // 60000 * 60000
        index = len(own) - 1
        tests = {}
        for spec in hypotheses["hypotheses"]:
            assert spec["operator"] == "gt" and spec["threshold"] == "0"
            background = [
                condition(own, btc, index - hours * 60, spec["feature"])
                for hours in range(2, 25, 2)
            ]
            tests[spec["id"]] = dict(
                entry_hit=condition(own, btc, index, spec["feature"]),
                background_hits=sum(background),
                background=background,
            )
        own_return = Decimal(own[index][4]) / Decimal(own[index - 60][4]) - 1
        btc_return = Decimal(btc[index][4]) / Decimal(btc[index - 60][4]) - 1
        rows.append(
            dict(
                position_id=pid,
                symbol=position["symbol"],
                day=order["orderTime"] // 86400000,
                decision_time=order["orderTime"],
                position_opened=position["position_opened"],
                return_60m=str(own_return),
                btc_return_60m=str(btc_return),
                relative_return_60m=str(own_return - btc_return),
                tests=tests,
            )
        )
    summaries = {}
    for spec in hypotheses["hypotheses"]:
        key = spec["id"]
        hits = sum(row["tests"][key]["entry_hit"] for row in rows)
        coverage = hits / len(rows)
        background_n = len(rows) * 12
        background_hits = sum(row["tests"][key]["background_hits"] for row in rows)
        background_rate = background_hits / background_n
        ratio = coverage / background_rate if background_rate else None
        days = {
            str(date): dict(n=len(g), hits=sum(row["tests"][key]["entry_hit"] for row in g))
            for date in sorted({row["day"] for row in rows})
            for g in [[row for row in rows if row["day"] == date]]
        }
        macro = mean(value["hits"] / value["n"] for value in days.values())
        criteria = hypotheses["candidate_criteria"]
        passes = (
            coverage >= criteria["min_entry_coverage"]
            and coverage - background_rate >= criteria["min_advantage_over_background"]
            and ratio is not None
            and ratio >= criteria["min_frequency_ratio"]
            and macro >= criteria["min_macro_day_coverage"]
        )
        summaries[key] = dict(
            n=len(rows),
            hits=hits,
            coverage=coverage,
            background_n=background_n,
            background_hits=background_hits,
            background_rate=background_rate,
            advantage_over_background=coverage - background_rate,
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
    args = parser.parse_args()
    split_path = args.root / f"{PREFIX}_entry_split_2026-11-08.json"
    benchmark_path = args.root / f"{BENCHMARK}_entry_split_2026-11-08.json"
    hypothesis_path = args.root / f"{PREFIX}_entry_hypotheses_2026-11-08.json"
    split, benchmark, hypotheses = map(read, (split_path, benchmark_path, hypothesis_path))
    assert sha(split_path) == hypotheses["split_sha256"]
    assert sha(benchmark_path) == hypotheses["benchmark_split_sha256"]
    phases = {
        phase: evaluate(args.root, phase, split, benchmark, hypotheses)
        for phase in ("development", "holdout")
    }
    supported = [
        spec["id"]
        for spec in hypotheses["hypotheses"]
        if all(phases[phase]["summary"][spec["id"]]["meets_frozen_screen"] for phase in phases)
    ]
    report = dict(
        status="entry_candidate" if supported else "rule_unidentified",
        supported_short_entry_conditions=supported,
        profitability_tested=False,
        scope="Pre-order conditions for sampled complete Short flows only; BTC leg not inferred",
        hypotheses=hypotheses,
        hypotheses_sha256=sha(hypothesis_path),
        split_sha256=sha(split_path),
        benchmark_split_sha256=sha(benchmark_path),
        script_sha256=sha(Path(__file__)),
        market_script_sha256=sha(Path(__file__).with_name("automata_revisit_probe.py")),
        phases=phases,
    )
    save(args.out, report)
    print({"status": report["status"], "supported": supported})
    for phase, part in phases.items():
        for key, value in part["summary"].items():
            print(phase, key, {k: v for k, v in value.items() if k != "day_counts"})


if __name__ == "__main__":
    main()
