"""Read-only, time-bounded diagnostics. Never certifies real exchange execution."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path


def ms(value: str) -> int:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("as-of requires a timezone")
    return int(stamp.timestamp() * 1000)


def bounds(d1: str, d2: str, as_of: int) -> tuple[int, int]:
    lo = ms(d1 + "T00:00:00+00:00")
    hi = min(ms((date.fromisoformat(d2) + timedelta(days=1)).isoformat()
                + "T00:00:00+00:00"), as_of)
    if hi <= lo:
        raise ValueError("empty or reversed reporting interval")
    return lo, hi


def read_rows(out: Path, as_of: int) -> tuple[list[dict], list[dict]]:
    rows, errors = [], []
    for path in sorted((out / "journal").glob("*.jsonl")):
        if path.stem > datetime.fromtimestamp(as_of / 1000, UTC).date().isoformat():
            continue
        with path.open(encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                try:
                    row = json.loads(line)
                    if row.get("recv", row.get("t", as_of)) < as_of:
                        rows.append(row)
                except (ValueError, TypeError):
                    errors.append({"file": path.name, "line": number})
    return rows, errors


def union_ms(intervals: list[tuple[int, int]], lo: int, hi: int) -> int:
    end, total = lo, 0
    for a, b in sorted(intervals):
        a, b = max(a, lo, end), min(b, hi)
        if b > a:
            total += b - a
            end = b
    return total


def deployment_pauses(rows: list[dict], hi: int) -> list[tuple[int, int]]:
    start = None
    intervals = []
    for r in rows:
        if r.get("k") == "drain" and r.get("paused"):
            if start is None:
                start = r["recv"]
        elif r.get("k") == "start" or (r.get("k") == "drain" and not r.get("paused")):
            if start is not None:
                intervals.append((start, r["recv"]))
                start = None
    if start is not None:
        intervals.append((start, hi))
    return intervals


def quality(rows: list[dict], lo: int, hi: int) -> dict:
    gaps, lag, reconnect = [], [], []
    lag_samples = stale_samples = 0
    per_connection = {}
    observed_masks = {}
    degraded_seconds = []
    for r in rows:
        k = r.get("k")
        if k == "gap":
            gaps.append((r["T0"], r["T1"]))
        elif k == "lat" and lo <= r["t"] < hi:
            observed_masks[r["t"]] = (observed_masks.get(r["t"], 0)
                                       | r.get("quality_seconds", 0))
            for second in range(60):
                timestamp = r["t"] + second * 1000
                if r.get("degraded_seconds", 0) & (1 << second):
                    if degraded_seconds and degraded_seconds[-1][1] == timestamp:
                        degraded_seconds[-1] = (degraded_seconds[-1][0], timestamp + 1000)
                    else:
                        degraded_seconds.append((timestamp, timestamp + 1000))
            lag_samples += r.get("quality_samples", 0)
            stale_samples += r.get("stale_samples", 0)
            if r.get("stale_from_ms") is not None:
                lag.append((r["stale_from_ms"], r["stale_to_ms"]))
            if (r.get("p50") or 0) > 5000:
                lag.append((r["t"], r["t"] + 60_000))
        elif k == "quality":
            lag.append((r["from_ms"], r["to_ms"]))
        elif k == "conn":
            conn = r.get("conn")
            if r.get("event") == "close":
                per_connection.setdefault(conn, r["recv"])
            elif r.get("event") == "open" and conn in per_connection:
                reconnect.append((per_connection.pop(conn), r["recv"]))
        elif k == "start" and r.get("restart_gap_s") is not None:
            reconnect.append((int(r["recv"] - r["restart_gap_s"] * 1000), r["recv"]))
    reconnect += [(t, hi) for t in per_connection.values()]
    pauses = deployment_pauses(rows, hi)
    intervals = gaps + lag + reconnect + degraded_seconds + pauses
    required_seconds = (hi + 999) // 1000 - lo // 1000
    observed_seconds = 0
    for minute, mask in observed_masks.items():
        first = max(0, (lo // 1000 * 1000 - minute) // 1000)
        last = min(60, ((hi + 999) // 1000 * 1000 - minute) // 1000)
        if last > first:
            observed_seconds += (mask & (((1 << (last - first)) - 1) << first)).bit_count()
    return {"gap_records": sum(a < hi and b >= lo for a, b in gaps),
            "gap_union_ms": union_ms(gaps, lo, hi),
            "stale_union_ms": union_ms(lag, lo, hi),
            "disconnect_union_ms": union_ms(reconnect, lo, hi),
            "deployment_pause_ms": union_ms(pauses, lo, hi),
            "affected_union_ms": union_ms(intervals, lo, hi),
            "quality_samples": lag_samples, "stale_samples": stale_samples,
            "observed_seconds": observed_seconds,
            "required_seconds": required_seconds,
            "coverage_complete": required_seconds == observed_seconds,
            "note": "Affected time is an estimate across any symbol/connection; "
                    "legacy journals lack continuous per-connection freshness coverage."}


def account_stats(rows: list[dict], lo: int, hi: int) -> dict:
    exits = {}
    fills = {}
    lost = Counter()
    for r in rows:
        if r.get("k") == "start" and r.get("recv", 0) >= hi:
            continue
        if r.get("k") == "fill" and r["t_in"] >= hi:
            continue
        if r.get("k") == "exit" and r["t_out"] >= hi:
            continue
        if r.get("k") == "start":
            for key in fills.keys() - exits.keys():
                lost[key[0]] += 1
            fills = {}
            exits = {k: v for k, v in exits.items()}
        elif r.get("k") in ("fill", "exit"):
            key = (r["acct"], r["sym"], r["t"], r["t_in"])
            (fills if r["k"] == "fill" else exits)[key] = r
    names = sorted(({k[0] for k in fills.keys() | exits.keys()} | set(lost)) - {"m"})
    result = {}
    for name in names:
        all_closed = sorted((r for k, r in exits.items() if k[0] == name and "pnl" in r),
                            key=lambda r: (r["t_out"], r["sym"], r["t_in"]))
        selected = [r for r in all_closed if lo <= r["t_out"] < hi]
        opening = sum(r["pnl"] for r in all_closed if r["t_out"] < lo)
        cumulative = opening
        peak = opening
        low, drawdown = 0.0, 0.0
        for r in selected:
            cumulative += r["pnl"]
            peak = max(peak, cumulative)
            low = min(low, cumulative - opening)
            drawdown = max(drawdown, peak - cumulative)
        pending = [r for k, r in fills.items() if k[0] == name and k not in exits]
        # Fixed fills, adverse total round-trip execution cost. Not a fill/queue model.
        stress = {str(bps): sum(r["pnl"] - r["amt"] * bps / 10_000
                               for r in selected) for bps in (0, 2, 5, 10, 25)}
        result[name] = {"closed_trades": len(selected),
                        "realized_pnl_d0": sum(r["pnl"] for r in selected),
                        "opening_cumulative_pnl_d0": opening,
                        "lowest_period_realized_change_d0": low,
                        "max_realized_drawdown_d0": drawdown,
                        "pending_positions": len(pending), "lost_positions": lost[name],
                        "unrealized_pnl": None, "mark_to_market_drawdown": None,
                        "stress_extra_round_trip_bps": stress,
                        "execution_verified": False}
    return result


def reconcile(rows: list[dict], archive: Path, lo: int, hi: int) -> dict:
    pauses = deployment_pauses(rows, hi)
    done = {tuple(k.rsplit(":", 1)) for k in
            (archive / "sim_done.txt").read_text().split()}
    gaps = defaultdict(list)
    for r in rows:
        if r.get("k") == "gap":
            gaps[r["sym"]].append((r["T0"], r["T1"]))
    bot = {(r["sym"], r["t"]): r for r in rows
           if r.get("k") == "exit" and r["acct"] == "m" and r["t_out"] < hi}
    fills = {(r["sym"], r["t"]) for r in rows
             if r.get("k") == "fill" and r["acct"] == "m"}
    counts = Counter()
    missing = []
    compared = set()
    archive_net = bot_net = 0.0
    with (archive / "sim.jsonl").open() as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("var") != "limit5|side" or not lo <= r["t"] < hi:
                continue
            key = (r["sym"], r["t"])
            if any(a <= r["t_in"] < b for a, b in pauses):
                counts["excluded_deployment_pause"] += 1
                continue
            if (r["sym"], r["day"]) not in done:
                counts["archive_incomplete"] += 1
                continue
            if r.get("t_out", hi) >= hi:
                counts["outcome_after_cutoff"] += 1
                continue
            if any(a <= r["t_out"] and r["t"] <= b for a, b in gaps[r["sym"]]):
                counts["excluded_gap"] += 1
                continue
            compared.add(key)
            b = bot.get(key)
            if b is None:
                counts["missing"] += 1
                missing.append({"symbol": r["sym"], "minute": r["t"],
                                "reason": "outcome_not_recorded" if key in fills
                                else "signal_not_recorded",
                                "cause_resolved": False, "archive_net": r["net"]})
            elif b["how"] != r["how"] or abs(b["net"] - r["net"]) >= 1e-9:
                counts["mismatch"] += 1
            else:
                counts["matched"] += 1
            if b is not None:
                archive_net += r["net"]
                bot_net += b["net"]
    counts["comparable"] = len(compared)
    share = counts["matched"] / len(compared) if compared else None
    delta = (abs(bot_net - archive_net) / abs(archive_net) if archive_net
             else (0.0 if bot_net == 0 else None))
    passed = (share is not None and share >= .90 and delta is not None and delta <= .10
              and not counts["archive_incomplete"])
    return {"counts": dict(counts), "missing": missing, "matched_share": share,
            "matched_archive_net": archive_net, "matched_bot_net": bot_net,
            "net_relative_difference": delta, "observed_criterion_passed": passed,
            "verified": False, "note": "Coverage and restart exclusions still require review."}


def evaluate_cells(summary: dict, accounts: dict, reconciliation: dict, health: dict) -> list:
    """Apply the frozen criteria per original cell; unknown evidence blocks readiness."""
    result = []
    for name, cell in zip(("x1", "x25", "x5"), summary.get("cells") or [summary], strict=False):
        account = accounts.get(name, {})
        sample = cell.get("days", 0) >= 84 and cell.get("trades", 0) >= 300
        k3 = reconciliation.get("observed_criterion_passed") is True
        k4 = (health["affected_union_ms"] <= health["window_ms"] * .01
              if health.get("coverage_complete") else None)
        k5 = (account["realized_pnl_d0"] > 0 if account.get("closed_trades")
              and not account.get("lost_positions") and not account.get("pending_positions")
              else None)
        checks = {"sample_ready": sample, "k1": cell.get("k1") is True,
                  "k2": cell.get("k2") is True, "k3_observed": k3, "k4": k4, "k5": k5}
        result.append({"account": name, "checks": checks,
                       "ready_for_owner_review": all(v is True for v in checks.values()),
                       "real_execution_verified": False})
    return result


def position_key(p: dict) -> tuple:
    # positionId is the exchange identity. Never change it to a synthetic order.
    if p.get("positionId") is None or p.get("opened") is None or not p.get("symbol"):
        raise ValueError("position identity missing")
    return p["symbol"], str(p["positionId"])


def merge_positions(old: list[dict], new: list[dict]) -> list[dict]:
    merged = {}
    for p in [*old, *new]:
        key = position_key(p)
        prior = merged.get(key)
        timestamp = p.get("updateTime") or 0
        previous_timestamp = (prior.get("updateTime") or 0) if prior else -1
        downgrade = (prior and prior.get("closed") is not None and p.get("closed") is None
                     and timestamp == previous_timestamp)
        if prior is None or (timestamp >= previous_timestamp and not downgrade):
            merged[key] = dict(p)
    return sorted(merged.values(), key=lambda p: (p["opened"], p["symbol"], str(p["positionId"])))


def trader_summary(positions: list[dict], fetched_at: str) -> dict:
    closed = [p for p in positions if p.get("closed") is not None]
    fast, other = [], []
    for p in closed:
        duration = p["closed"] - p["opened"]
        # Descriptive cohort only: duration is not proof of recovered entry rules.
        (fast if p.get("side") == "Long" and str(p.get("leverage")) == "1"
         and 0 <= duration <= 8 * 60_000 else other).append(p)
    def stats(group):
        return {"count": len(group), "closing_pnl_usdt":
                sum(float(p.get("closingPnl") or 0) for p in group)}
    return {"fetched_at": fetched_at, "positions": len(positions),
            "open_or_partial": len(positions) - len(closed), "closed": stats(closed),
            "fast_long_x1_candidate_only": stats(fast), "other_closed": stats(other),
            "first_opened": min((p["opened"] for p in positions), default=None),
            "last_closed": max((p["closed"] for p in closed), default=None),
            "complete_history": False, "rule_identified": False}


def build(out: Path, archive: Path, d1: str, d2: str, as_of: int) -> dict:
    lo, hi = bounds(d1, d2, as_of)
    rows, errors = read_rows(out, as_of)
    health = quality(rows, lo, hi)
    health["window_ms"] = hi - lo
    accounts = account_stats(rows, lo, hi)
    comparison = reconcile(rows, archive, lo, hi)
    if errors:
        comparison["observed_criterion_passed"] = False
    summary_path = archive / "forward_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    # A daily archive summary cannot authorize a different requested time window.
    if summary.get("start") != d1 or summary.get("through") != d2:
        summary = {}
    cells = evaluate_cells(summary, accounts, comparison, health) if summary else []
    return {"schema_version": 1, "from_ms": lo, "through_exclusive_ms": hi,
            "data_as_of_exclusive_ms": as_of,
            "time_basis": "realized exits by t_out; only records received before cutoff",
            "journal_errors": errors, "quality": health,
            "accounts": accounts, "cell_criteria": cells,
            "reconciliation": comparison,
            "execution_verified": False, "ready": False,
            "limitations": ["No order book, order acknowledgments or real fills",
                            "Stress keeps fills fixed; queue and capacity remain unverified",
                            "Unrealized PnL and mark-to-market drawdown are unknown"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--from", dest="d1", required=True)
    parser.add_argument("--to", dest="d2", required=True)
    parser.add_argument("--as-of", required=True, help="ISO timestamp with timezone")
    parser.add_argument("--json", type=Path, required=True)
    a = parser.parse_args()
    result = build(a.out, a.archive, a.d1, a.d2, ms(a.as_of))
    a.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"diagnostics saved: {a.json}; real execution remains unverified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
