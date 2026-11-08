"""Bounded entry research with a frozen chronological market-feature holdout."""

import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

PID = "4937955534211236097"
PREFIX = "bncopy16"
MINUTE = 60000


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def day(stamp):
    return datetime.fromtimestamp(stamp / 1000, UTC).strftime("%Y-%m-%d")


def evenly(rows, count):
    if len(rows) <= count:
        return rows
    return [rows[round(i * (len(rows) - 1) / (count - 1))] for i in range(count)]


def prepare(root):
    path = root / f"{PREFIX}_entry_split_2026-11-08.json"
    if path.exists():
        raise SystemExit("Split already frozen; refusing to overwrite")
    source = root / f"{PREFIX}_{PID}_2026-11-08.json"
    data = read(source)
    rows = sorted(data["positions"], key=lambda r: (r["opened"], r["positionId"]))
    days = sorted({day(r["opened"]) for r in rows})
    assert len(days) >= 6
    boundary = len(days) * 2 // 3
    phases = {}
    for name, selected_days, count in (
        ("development", days[:boundary], 30),
        ("holdout", days[boundary:], 20),
    ):
        candidates = [
            r
            for date in selected_days
            for r in evenly([r for r in rows if day(r["opened"]) == date], 3)
        ]
        phases[name] = dict(days=selected_days, sample=evenly(candidates, count))
    save(
        path,
        dict(
            frozen_at=datetime.now(UTC).isoformat(),
            source_sha256=sha(source),
            script_sha256=sha(Path(__file__)),
            available_positions=len(rows),
            reported_total=data["total"],
            complete_position_history=data["complete"],
            holdout_scope=(
                "Entry market features only; position timing, exit prices "
                "and initial order slice were already inspected"
            ),
            phases=phases,
        ),
    )
    print({k: dict(days=len(v["days"]), sample=len(v["sample"])) for k, v in phases.items()})


def fetch(root, phase):
    import httpx

    split = read(root / f"{PREFIX}_entry_split_2026-11-08.json")
    if phase == "holdout":
        assert (root / f"{PREFIX}_entry_hypotheses_2026-11-08.json").exists(), (
            "Freeze hypotheses first"
        )
    folder = root / f"{PREFIX}_market_{phase}_2026-11-08"
    folder.mkdir(exist_ok=True)
    with httpx.Client(timeout=30) as client:
        for row in split["phases"][phase]["sample"]:
            path = folder / f"{row['positionId']}.json"
            if path.exists():
                continue
            # Exclude the entire minute containing the fill: only completed bars.
            end = row["opened"] // MINUTE * MINUTE - 1
            start = end // MINUTE * MINUTE - 2880 * MINUTE
            cursor, bars, requests = start, {}, []
            while cursor <= end:
                params = dict(
                    symbol=row["symbol"], interval="1m", startTime=cursor, endTime=end, limit=1500
                )
                for attempt in range(4):
                    record = dict(params=params, fetched_at=datetime.now(UTC).isoformat())
                    try:
                        response = client.get(
                            "https://fapi.binance.com/fapi/v1/klines", params=params
                        )
                        response.raise_for_status()
                        record["response"] = response.json()
                        assert isinstance(record["response"], list) and record["response"]
                        break
                    except (httpx.HTTPError, AssertionError, ValueError) as exc:
                        record["error"] = str(exc)
                        save(folder / f"error_{row['positionId']}_{time.time_ns()}.json", record)
                        time.sleep(2 * (attempt + 1))
                else:
                    raise RuntimeError(f"Market fetch failed for {row['symbol']}")
                requests.append(record)
                for bar in record["response"]:
                    bars[bar[0]] = bar
                cursor = record["response"][-1][0] + MINUTE
                time.sleep(0.15)
            ordered = sorted(bars.values(), key=lambda b: b[0])
            assert len(ordered) == 2881
            assert all(b[0] == start + i * MINUTE and b[6] <= end for i, b in enumerate(ordered))
            save(
                path,
                dict(
                    position_id=row["positionId"],
                    symbol=row["symbol"],
                    requests=requests,
                    bars=ordered,
                ),
            )
            print(phase, row["symbol"], row["positionId"], len(ordered), flush=True)


def features(bars, index):
    close = float(bars[index][4])
    result = {
        f"return_{lag}m": close / float(bars[index - lag][4]) - 1 for lag in (1, 5, 15, 60, 1440)
    }
    high, low = float(bars[index][2]), float(bars[index][3])
    result["range_1m"] = high / low - 1
    result["close_location_1m"] = (close - low) / (high - low) if high != low else 0.5
    volumes = [float(b[7]) for b in bars[index - 60 : index]]
    result["relative_volume_1m"] = (
        float(bars[index][7]) / (sum(volumes) / 60) if sum(volumes) else None
    )
    return result


def describe(root, phase):
    split = read(root / f"{PREFIX}_entry_split_2026-11-08.json")
    rows, controls, sources = [], [], {}
    folder = root / f"{PREFIX}_market_{phase}_2026-11-08"
    for row in split["phases"][phase]["sample"]:
        path = folder / f"{row['positionId']}.json"
        data = read(path)
        sources[path.name] = sha(path)
        bars = data["bars"]
        index = len(bars) - 1
        value = features(bars, index)
        # Descriptive average-position execution price, NOT a known first-fill trigger.
        value["avg_entry_vs_previous_close"] = float(row["avgCost"]) / float(bars[index][4]) - 1
        rows.append(
            dict(
                position_id=row["positionId"],
                symbol=row["symbol"],
                side=row["side"],
                day=day(row["opened"]),
                features=value,
            )
        )
        for hours in range(2, 25, 2):
            controls.append(
                dict(
                    position_id=row["positionId"],
                    hours_before=hours,
                    features=features(bars, index - hours * 60),
                )
            )
    summary = {}
    for key in rows[0]["features"]:
        entry = sorted(r["features"][key] for r in rows if r["features"][key] is not None)
        background = sorted(
            r["features"].get(key) for r in controls if r["features"].get(key) is not None
        )
        summary[key] = dict(
            entry_median=median(entry),
            entry_min=min(entry),
            entry_max=max(entry),
            background_median=median(background) if background else None,
        )
    report = dict(
        phase=phase,
        split_sha256=sha(root / f"{PREFIX}_entry_split_2026-11-08.json"),
        sources=sources,
        entries=rows,
        background=controls,
        summary=summary,
        days=dict(Counter(r["day"] for r in rows)),
        background_is_not_labeled_nonentries=True,
    )
    save(root / f"{PREFIX}_entry_{phase}_2026-11-08.json", report)
    print(json.dumps(summary, indent=2))


def main():
    global PID, PREFIX
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "fetch", "describe"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--phase", choices=("development", "holdout"), default="development")
    parser.add_argument("--prefix", default=PREFIX)
    parser.add_argument("--pid", default=PID)
    args = parser.parse_args()
    PREFIX, PID = args.prefix, args.pid
    if args.command == "prepare":
        prepare(args.root)
    elif args.command == "fetch":
        fetch(args.root, args.phase)
    else:
        describe(args.root, args.phase)


if __name__ == "__main__":
    main()
