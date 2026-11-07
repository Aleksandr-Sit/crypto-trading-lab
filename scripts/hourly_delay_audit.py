"""Collect bounded public minute windows for preselected 24h counterexamples.

Only the competitors already saved in the turnover audit are tested. This checks
price-return ordering at orderTime, not turnover filters or full-universe ranks.
"""

import argparse
import json
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fetch_hourly_reconstruction import throttle
from hourly_automata_audit import HOUR, digest, read

PID = "4693119134299763969"
MINUTE = 60_000


def prepare(root):
    source = root / "hourly_liquidity_audit_2026-11-07.json"
    orders_path = root / f"hourly_{PID}_orders_2026-11-07.json"
    orders = [o for o in read(orders_path)["orders"] if o["side"] == "BUY"]
    cases, intervals = [], defaultdict(list)
    for entry in read(source)["entries"][PID]:
        if entry["lookback_hours"] != 24 or not entry["example"]:
            continue
        opened = int(datetime.fromisoformat(entry["opened"]).timestamp() * 1000)
        matches = [
            o
            for o in orders
            if o["symbol"] == entry["symbol"] and abs(o["orderUpdateTime"] - opened) <= 30_000
        ]
        if len(matches) != 1:
            raise ValueError(f"Non-unique opening BUY: {entry['opened']} {entry['symbol']}")
        order = matches[0]
        boundary = opened // HOUR * HOUR
        last = order["orderTime"] // MINUTE * MINUTE - MINUTE
        if not boundary - MINUTE <= last < boundary + 10 * MINUTE:
            raise ValueError("Unexpected order phase")
        competitor = entry["example"]["symbol"]
        cases.append(
            dict(
                symbol=entry["symbol"],
                competitor=competitor,
                opened=entry["opened"],
                order_time=order["orderTime"],
                boundary=boundary,
                last_closed_minute=last,
            )
        )
        for symbol in (entry["symbol"], competitor):
            for offset in (0, -24 * HOUR):
                intervals[symbol].append((boundary - MINUTE + offset, last + offset))
    queries = []
    for symbol, windows in sorted(intervals.items()):
        merged = []
        for start, end in sorted(windows):
            if merged and start <= merged[-1][1] + MINUTE:
                merged[-1][1] = max(end, merged[-1][1])
            else:
                merged.append([start, end])
        for start, end in merged:
            if (end - start) // MINUTE + 1 > 499:
                raise ValueError("Window exceeds bounded request")
            queries.append(dict(symbol=symbol, start=start, end=end))
    return dict(
        method="saved_24h_competitor_at_opening_buy_v1",
        source_hashes={source.name: digest(source), orders_path.name: digest(orders_path)},
        cases=cases,
        queries=queries,
    )


def fetch(plan, out):
    stop = threading.Event()
    local = threading.local()
    clients = []

    def one(query):
        path = out / f"{query['symbol']}_{query['start']}_{query['end']}.json"
        if path.exists():
            return read(path)["complete"]
        if stop.is_set():
            return False
        params = dict(
            symbol=query["symbol"],
            interval="1m",
            startTime=query["start"],
            endTime=query["end"] + MINUTE - 1,
            limit=499,
        )
        record = dict(
            query=query,
            params=params,
            complete=False,
            rows=[],
            error=None,
            fetched_at=datetime.now(UTC).isoformat(),
        )
        try:
            if not hasattr(local, "client"):
                local.client = httpx.Client(timeout=25)
                clients.append(local.client)
            throttle()
            response = local.client.get("https://fapi.binance.com/fapi/v1/klines", params=params)
            record["http_status"] = response.status_code
            record["used_weight"] = response.headers.get("x-mbx-used-weight-1m")
            if response.status_code in (418, 429):
                stop.set()
            response.raise_for_status()
            rows = response.json()
            record["rows"] = rows
            expected = list(range(query["start"], query["end"] + 1, MINUTE))
            if not isinstance(rows, list) or [r[0] for r in rows] != expected:
                raise ValueError("Missing, duplicate, or unexpected minute timestamps")
            if any(float(r[4]) <= 0 or r[6] >= params["endTime"] + 1 for r in rows):
                raise ValueError("Invalid candle price/close time")
            record["complete"] = True
        except (httpx.HTTPError, ValueError, TypeError, IndexError) as exc:
            record["error"] = str(exc)
        path.write_text(json.dumps(record), encoding="utf-8")
        return record["complete"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        good = 0
        for n, complete in enumerate(pool.map(one, plan["queries"]), 1):
            good += complete
            if n % 100 == 0 or n == len(plan["queries"]):
                print(f"{n}/{len(plan['queries'])} complete={good}", flush=True)
    for client in clients:
        client.close()


def analyze(plan, out, root=None):
    bars, hashes, failures = defaultdict(dict), {}, []
    for query in plan["queries"]:
        name = f"{query['symbol']}_{query['start']}_{query['end']}.json"
        path = out / name
        if not path.exists():
            failures.append(dict(query=query, error="not fetched"))
            continue
        hashes[name] = digest(path)
        record = read(path)
        if record["query"] != query or not record["complete"]:
            failures.append(dict(query=query, error=record["error"]))
            continue
        for row in record["rows"]:
            bars[query["symbol"]][row[0]] = float(row[4])
    boundary_comparisons = 0
    if root is not None:
        source = read(root / "hourly_liquidity_audit_2026-11-07.json")
        for entry in source["entries"][PID]:
            if entry["lookback_hours"] != 24 or not entry["example"]:
                continue
            t = int(datetime.fromisoformat(entry["boundary"]).timestamp() * 1000) - MINUTE
            for symbol, features in (
                (entry["symbol"], entry["selected"]),
                (entry["example"]["symbol"], entry["example"]),
            ):
                rows = bars[symbol]
                if t in rows and t - 24 * HOUR in rows:
                    actual = rows[t] / rows[t - 24 * HOUR] - 1
                    if abs(actual - features["r24h"]) > 1e-10:
                        raise ValueError(f"Hourly/minute boundary disagreement: {symbol} {t}")
                    boundary_comparisons += 1
    cases = []
    for case in plan["cases"]:
        result = dict(case)
        t = case["last_closed_minute"]
        try:
            a, b = bars[case["symbol"]], bars[case["competitor"]]
            selected = a[t] / a[t - 24 * HOUR] - 1
            competitor = b[t] / b[t - 24 * HOUR] - 1
            margins = [
                b[m] / b[m - 24 * HOUR] - a[m] / a[m - 24 * HOUR]
                for m in range(case["boundary"] - MINUTE, t + 1, MINUTE)
            ]
            result.update(
                selected_return=selected,
                competitor_return=competitor,
                stronger_competitor_remains=competitor > selected,
                stronger_at_all_closed_minutes=min(margins) > 0,
                minimum_return_margin=min(margins),
            )
        except KeyError:
            result["stronger_competitor_remains"] = None
        cases.append(result)
    summary = {}
    for month, total in (("2026-09", 304), ("2026-10", 282)):
        rows = [r for r in cases if r["opened"].startswith(month)]
        summary[month] = dict(
            original_entries=total,
            preselected_counterexamples=len(rows),
            evaluated=sum(r["stronger_competitor_remains"] is not None for r in rows),
            competitor_still_stronger=sum(r["stronger_competitor_remains"] is True for r in rows),
            competitor_no_longer_stronger=sum(
                r["stronger_competitor_remains"] is False for r in rows
            ),
            missing=sum(r["stronger_competitor_remains"] is None for r in rows),
            stronger_at_all_closed_minutes=sum(
                r.get("stronger_at_all_closed_minutes") is True for r in rows
            ),
        )
    return dict(
        method=plan["method"],
        verified_hourly_minute_boundaries=boundary_comparisons,
        plan_sha256=digest(out / "plan.json"),
        script_sha256=digest(Path(__file__)),
        summary=summary,
        failures=failures,
        candle_hashes=hashes,
        cases=cases,
        limitations=[
            "Exploratory months, not a new holdout.",
            "Last closed minute prices, not intra-minute quotes.",
            "No contemporaneous turnover, depth, or order-size filters tested.",
            "One preselected competitor; no full-universe ranking inferred.",
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    plan = prepare(args.root)
    plan_path = args.out / "plan.json"
    if plan_path.exists() and read(plan_path) != plan:
        raise ValueError("Existing collection plan differs; use a new output directory")
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(f"Fixed plan: {len(plan['cases'])} pairs, {len(plan['queries'])} queries", flush=True)
    if args.fetch:
        fetch(plan, args.out)
    result = analyze(plan, args.out, args.root)
    (args.out / "audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2), flush=True)


if __name__ == "__main__":
    main()
