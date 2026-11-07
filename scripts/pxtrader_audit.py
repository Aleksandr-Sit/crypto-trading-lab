"""Offline PXTRADER position/timing audit. No price search or profitability test."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from open_automata import features

PID = "4810377016221347072"
HOUR = 3_600_000


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iso(t):
    return datetime.fromtimestamp(t / 1000, UTC).isoformat()


def timing(rows):
    closed = [r for r in rows if r["closed"]]
    durations = [(r["closed"] - r["opened"]) / 1000 for r in closed]
    peak = active = 0
    for _, delta in sorted(
        [(r["opened"], 1) for r in closed] + [(r["closed"], -1) for r in closed]
    ):
        active += delta
        peak = max(active, peak)
    f = features(rows)

    def exact_hour(t):
        return min(t % HOUR, HOUR - t % HOUR) <= 30_000

    return dict(
        n=len(rows),
        sides=dict(Counter(r["side"] for r in rows)),
        leverage=dict(Counter(r["leverage"] for r in rows)),
        margin=dict(Counter(r["isolated"] for r in rows)),
        symbols=len({r["symbol"] for r in rows}),
        first_open=iso(min(r["opened"] for r in rows)),
        last_open=iso(max(r["opened"] for r in rows)),
        first_close=iso(min(r["closed"] for r in closed)),
        last_close=iso(max(r["closed"] for r in closed)),
        peak_closed_position_overlap=peak,
        opened_first3min=sum(r["opened"] % HOUR < 180_000 for r in rows),
        opened_within30s_of_hour=sum(exact_hour(r["opened"]) for r in rows),
        closed_within30s_of_hour=sum(exact_hour(r["closed"]) for r in closed),
        one_hour_30s=sum(abs(t - 3600) <= 30 for t in durations),
        integer_hour_30s=sum(
            round(t / 3600) >= 1 and abs(t - round(t / 3600) * 3600) <= 30 for t in durations
        ),
        holding_min_seconds=min(durations),
        holding_median_seconds=median(durations),
        holding_max_seconds=max(durations),
        peak_notional_median=median(
            float(r["avgCost"]) * float(r["maxOpenInterest"]) for r in rows
        ),
        timer={
            k: f[k]
            for k in (
                "timer_loss_share",
                "timer_loss_mode_s",
                "timer_all_share",
                "timer_all_mode_s",
            )
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    paths = [
        args.root / f"pxtrader_{PID}{suffix}"
        for suffix in ("_scan_2026-10-25.json", "_2026-11-07.json", "_orders_2026-11-07.json")
    ]
    old, new, orders = map(read, paths)
    old_map = {r["positionId"]: r for r in old["rows"]}
    new_map = {r["positionId"]: r for r in new["positions"]}
    rows = sorted((old_map | new_map).values(), key=lambda r: r["opened"])
    by_month = defaultdict(list)
    by_second = defaultdict(list)
    for r in rows:
        by_month[iso(r["opened"])[:7]].append(r)
        if r["closed"]:
            by_second[r["closed"] // 1000].append(r)
    groups = sorted(by_second.values(), key=lambda g: -len(g))
    baskets = []
    for g in groups[:5]:
        baskets.append(
            dict(
                n=len(g),
                first_close=iso(min(r["closed"] for r in g)),
                last_close=iso(max(r["closed"] for r in g)),
                close_span_ms=max(r["closed"] for r in g) - min(r["closed"] for r in g),
                first_open=iso(min(r["opened"] for r in g)),
                last_open=iso(max(r["opened"] for r in g)),
                position_ids=[r["positionId"] for r in g],
            )
        )
    early = [r for r in rows if iso(r["opened"])[:7] < "2026-08"]
    late = [r for r in rows if iso(r["opened"])[:7] >= "2026-08"]
    by_open = sorted(early, key=lambda r: r["opened"])
    switches = [
        dict(previous_id=a["positionId"], next_id=b["positionId"], gap_ms=b["opened"] - a["closed"])
        for a, b in zip(by_open, by_open[1:], strict=False)
    ]
    peer_comparison, peer_sources = {}, {}
    own_hours = defaultdict(set)
    for r in early:
        own_hours[r["opened"] // HOUR].add(r["symbol"])
    for peer in ("4883987534523154688", "4894215989067406336", "4693119134299763969"):
        path = args.root / f"hourly_{peer}_2026-11-07.json"
        peer_sources[path.name] = sha(path)
        peer_rows = read(path)["positions"]
        peer_hours = defaultdict(set)
        for r in peer_rows:
            if r["side"] == "Long":
                peer_hours[r["opened"] // HOUR].add(r["symbol"])
        common = sorted(own_hours.keys() & peer_hours.keys())
        unique = [t for t in common if len(own_hours[t]) == len(peer_hours[t]) == 1]
        peer_comparison[peer] = dict(
            first_open=iso(min(r["opened"] for r in peer_rows)),
            last_open=iso(max(r["opened"] for r in peer_rows)),
            common_new_open_hours=len(common),
            unambiguous_hours=len(unique),
            identical_symbols=sum(own_hours[t] == peer_hours[t] for t in unique),
        )
    report = dict(
        portfolio_id=PID,
        status="rule_unidentified",
        profitability_tested=False,
        script_sha256=sha(Path(__file__)),
        feature_script_sha256=sha(Path(__file__).with_name("open_automata.py")),
        sources={p.name: sha(p) for p in paths},
        peer_sources=peer_sources,
        peer_comparison=peer_comparison,
        old_rows=len(old["rows"]),
        old_unique=len(old_map),
        new_rows=len(new["positions"]),
        new_unique=len(new_map),
        reported_new_total=new["total"],
        new_not_in_scan=sorted(new_map.keys() - old_map.keys()),
        old_missing_current=sorted(old_map.keys() - new_map.keys()),
        changed_shared=[k for k in old_map.keys() & new_map.keys() if old_map[k] != new_map[k]],
        union_unique=len(rows),
        union_closed=sum(bool(r["closed"]) for r in rows),
        order_count=len(orders["orders"]),
        reported_order_total=orders["total"],
        profile_description=new["detail"]["description"],
        overall=timing(rows),
        months={month: timing(g) for month, g in sorted(by_month.items())},
        early_regime=timing(early),
        late_regime=timing(late),
        early_switches=switches,
        largest_second_groups=baskets,
        position_volume_differences=[
            r["positionId"] for r in rows if r["closedVolume"] != r["maxOpenInterest"]
        ],
    )
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "new_unique",
                    "union_unique",
                    "changed_shared",
                    "order_count",
                    "early_regime",
                    "late_regime",
                )
            }
        )
    )
    print("largest groups", [{k: v for k, v in g.items() if k != "position_ids"} for g in baskets])


if __name__ == "__main__":
    main()
