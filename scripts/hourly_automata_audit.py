"""Offline reconstruction diagnostics, not a profitability test or strategy search."""

import argparse
import bisect
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

PIDS = ("4883987534523154688", "4894215989067406336", "4693119134299763969")
HOUR = 3_600_000
M5 = 300_000


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def iso(t):
    return datetime.fromtimestamp(t / 1000, UTC).isoformat()


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def audit(root):
    out, portfolios = {}, {}
    for pid in PIDS:
        pos_path = root / f"hourly_{pid}_2026-11-07.json"
        ord_path = root / f"hourly_{pid}_orders_2026-11-07.json"
        data, orders = read(pos_path), read(ord_path)["orders"]
        rows = data["positions"]
        portfolios[pid] = rows
        events = sorted([(r["opened"], 1) for r in rows] + [(r["closed"], -1) for r in rows])
        cur = peak = 0
        for _, inc in events:
            cur += inc
            peak = max(peak, cur)
        by_key = {}
        for order in orders:
            by_key.setdefault((order["symbol"], order["side"]), []).append(order)
        matches, residuals, price_errors, qty_errors = [], [], [], []
        for row in rows:
            for side, field, price_field in (
                ("BUY", "opened", "avgCost"),
                ("SELL", "closed", "avgClosePrice"),
            ):
                candidates = by_key.get((row["symbol"], side), [])
                if not candidates:
                    continue
                best = min(candidates, key=lambda o: abs(o["orderUpdateTime"] - row[field]))
                if abs(best["orderUpdateTime"] - row[field]) > 30_000:
                    continue
                matches.append((side, row["positionId"]))
                residuals.append(abs(best["orderUpdateTime"] - row[field]))
                price_errors.append(abs(float(best["avgPrice"]) / float(row[price_field]) - 1))
                quantity = row["maxOpenInterest"] if side == "BUY" else row["closedVolume"]
                if float(quantity):
                    qty_errors.append(abs(float(best["executedQty"]) / float(quantity) - 1))
        durations = [(r["closed"] - r["opened"]) / HOUR for r in rows]
        grouped = []
        order_start = min(o["orderUpdateTime"] for o in orders)
        order_end = max(o["orderUpdateTime"] for o in orders)
        for row in rows:
            if row["opened"] < order_start or row["closed"] > order_end:
                continue
            legs = {}
            for side in ("BUY", "SELL"):
                chosen = [
                    o
                    for o in by_key.get((row["symbol"], side), [])
                    if row["opened"] - 1 <= o["orderUpdateTime"] <= row["closed"] + 1
                ]
                quantity = sum(float(o["executedQty"]) for o in chosen)
                legs[side] = dict(
                    n=len(chosen),
                    quantity=quantity,
                    average_price=sum(
                        float(o["executedQty"]) * float(o["avgPrice"]) for o in chosen
                    )
                    / quantity
                    if quantity
                    else None,
                )
            if not legs["BUY"]["quantity"] or not legs["SELL"]["quantity"]:
                continue
            grouped.append(
                dict(
                    position_id=row["positionId"],
                    symbol=row["symbol"],
                    legs=legs,
                    buy_quantity_error=legs["BUY"]["quantity"] - float(row["closedVolume"]),
                    sell_quantity_error=legs["SELL"]["quantity"] - float(row["closedVolume"]),
                    buy_price_relative_error=abs(
                        legs["BUY"]["average_price"] / float(row["avgCost"]) - 1
                    ),
                    sell_price_relative_error=abs(
                        legs["SELL"]["average_price"] / float(row["avgClosePrice"]) - 1
                    ),
                )
            )
        scan_path = root / f"hourly_{pid}_scan_2026-10-25.json"
        scan_ids = {r["positionId"] for r in read(scan_path)["rows"]}
        end_to_start = []
        by_open = sorted(rows, key=lambda r: r["opened"])
        for a, b in zip(by_open, by_open[1:], strict=False):
            end_to_start.append((b["opened"] - a["closed"]) / 1000)
        out[pid] = dict(
            hashes={pos_path.name: digest(pos_path), ord_path.name: digest(ord_path)},
            positions=len(rows),
            reported_total=data["total"],
            unique_ids=len({r["positionId"] for r in rows}),
            sides=dict(Counter(r["side"] for r in rows)),
            first_open=iso(min(r["opened"] for r in rows)),
            last_open=iso(max(r["opened"] for r in rows)),
            max_concurrent=peak,
            median_holding_hours=median(durations),
            max_holding_hours=max(durations),
            integer_hour_within30s=sum(abs(v - round(v)) <= 30 / 3600 for v in durations),
            order_count=len(orders),
            matched_position_legs=len(matches),
            matched_buys=sum(s == "BUY" for s, _ in matches),
            matched_sells=sum(s == "SELL" for s, _ in matches),
            max_match_time_error_ms=max(residuals, default=None),
            max_match_price_relative_error=max(price_errors, default=None),
            max_match_quantity_relative_error=max(qty_errors, default=None),
            median_order_execution_delay_ms=median(
                o["orderUpdateTime"] - o["orderTime"] for o in orders
            ),
            switch_within30s=sum(abs(v) <= 30 for v in end_to_start),
            gap_over30s=sum(v > 30 for v in end_to_start),
            overlap_over30s=sum(v < -30 for v in end_to_start),
            prior_scan_n=len(scan_ids),
            prior_scan_ids_present=len(scan_ids & {r["positionId"] for r in rows}),
            grouped_complete_positions=len(grouped),
            grouped_quantity_mismatches=sum(
                abs(r["buy_quantity_error"]) > 1e-6 or abs(r["sell_quantity_error"]) > 1e-6
                for r in grouped
            ),
            grouped_max_price_relative_error=max(
                max(r["buy_price_relative_error"], r["sell_price_relative_error"]) for r in grouped
            ),
            grouped_multiple_orders=[
                r for r in grouped if r["legs"]["BUY"]["n"] > 1 or r["legs"]["SELL"]["n"] > 1
            ],
            opening_hours=dict(
                Counter(datetime.fromtimestamp(r["opened"] / 1000, UTC).hour for r in rows)
            ),
        )
    return out, portfolios


def ranks(portfolios, universe_path, allow_incomplete=False):
    failures = []
    if universe_path.is_dir():
        metadata = read(universe_path / "symbols.json")
        manifest = read(universe_path / "manifest.json")
        failures = manifest["failures"]
        if failures and not allow_incomplete:
            raise ValueError("Universe has failed symbols; resolve before ranking")
        bars = {}
        for symbol in metadata:
            path = universe_path / f"{symbol}.json"
            if not path.exists():
                continue
            record = read(path)
            if not record["complete"]:
                if allow_incomplete:
                    continue
                raise ValueError(f"Incomplete symbol {symbol}")
            bars[symbol] = [
                [r[0], *map(float, r[1:5]), float(r[7]), r[8], float(r[10])] for r in record["rows"]
            ]
        step = HOUR
        evidence_hashes = {path.name: digest(path) for path in sorted(universe_path.glob("*.json"))}
        universe_hash = hashlib.sha256(
            json.dumps(evidence_hashes, sort_keys=True).encode()
        ).hexdigest()
    else:
        universe = read(universe_path)
        bars, metadata = universe["k5m"], universe["symbols"]
        step = M5
        universe_hash = digest(universe_path)
    per_hour = HOUR // step
    indices = {s: {r[0]: i for i, r in enumerate(rows)} for s, rows in bars.items()}
    # The closed hourly boundary is used even though orders arrive 1-3 minutes later.
    times = sorted({r["opened"] // HOUR * HOUR for rows in portfolios.values() for r in rows})
    ranks_by_time = {}
    for t in times:
        values = {}
        for symbol, rows in bars.items():
            i = indices[symbol].get(t - step)
            warmup = 48 * per_hour
            if i is None or i < warmup or rows[i - warmup][0] != t - step - 48 * HOUR:
                continue
            if metadata[symbol]["onboard"] >= t:
                continue
            c = rows[i][4]
            features = {f"r{h}h": c / rows[i - h * per_hour][4] - 1 for h in (1, 4, 12, 24, 48)}
            features["qv24h"] = sum(r[5] for r in rows[i - 24 * per_hour + 1 : i + 1])
            features["qv1h"] = sum(r[5] for r in rows[i - per_hour + 1 : i + 1])
            features["age_days"] = (t - metadata[symbol]["onboard"]) / 86_400_000
            values[symbol] = features
        if not values:
            continue
        mapped = {s: {} for s in values}
        for name in next(iter(values.values())):
            ordered = sorted(v[name] for v in values.values())
            for s, v in values.items():
                # Midrank handles ties; extremes range from zero to one.
                lo, hi = bisect.bisect_left(ordered, v[name]), bisect.bisect_right(ordered, v[name])
                mapped[s][name] = (lo + hi - 1) / (2 * max(1, len(ordered) - 1))
        ranks_by_time[t] = dict(n=len(values), ranks=mapped, features=values)
    result = {}
    for pid, positions in portfolios.items():
        records = []
        for r in positions:
            t = r["opened"] // HOUR * HOUR
            frame = ranks_by_time.get(t)
            if not frame or r["symbol"] not in frame["ranks"]:
                continue
            records.append(
                dict(
                    symbol=r["symbol"],
                    opened=iso(r["opened"]),
                    universe_n=frame["n"],
                    ranks=frame["ranks"][r["symbol"]],
                    features=frame["features"][r["symbol"]],
                )
            )
        stats = {}
        if records:
            for name in records[0]["ranks"]:
                vals = [r["ranks"][name] for r in records]
                stats[name] = dict(
                    median_rank=median(vals),
                    bottom10=sum(v <= 0.1 for v in vals),
                    top10=sum(v >= 0.9 for v in vals),
                    top1=sum(v >= 1 - 1e-12 for v in vals),
                    top5=sum(r["ranks"][name] >= 1 - 4 / (r["universe_n"] - 1) for r in records),
                )
        result[pid] = dict(n=len(records), statistics=stats, entries=records)
    return dict(
        universe_hash=universe_hash, step_ms=step, universe_failures=failures, diagnostics=result
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--universe", type=Path)
    parser.add_argument("--allow-incomplete-universe", action="store_true")
    args = parser.parse_args()
    result, portfolios = audit(args.root)
    out = dict(audit=result)
    if args.universe:
        out["selection_diagnostics"] = ranks(
            portfolios, args.universe, args.allow_incomplete_universe
        )
    print(json.dumps(out, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
