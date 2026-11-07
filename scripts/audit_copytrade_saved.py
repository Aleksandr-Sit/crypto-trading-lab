"""Read-only audit of saved copytrade evidence; no API calls or source writes.

Run on the data host: python3 audit_copytrade_saved.py /path/to/copytrade
"""

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def main():
    root = Path(sys.argv[1])
    hashes = {}
    for path in [
        root / "forward.jsonl",
        root / "portfolios.jsonl",
        root / "candidates.json",
        *sorted((root / "snapshots").glob("*.json")),
    ]:
        with path.open("rb") as source:
            hashes[str(path.relative_to(root))] = hashlib.file_digest(source, "sha256").hexdigest()
    cache = {}
    duplicate_rows = 0
    for line in (root / "portfolios.jsonl").open():
        row = json.loads(line)
        duplicate_rows += row["address"] in cache
        cache[row["address"]] = row["portfolio"]
    results = [
        json.loads(line)
        for line in (root / "forward.jsonl").read_text().splitlines()
        if line.strip()
    ]
    snaps = []
    for path in sorted((root / "snapshots").glob("*.json")):
        snap = json.loads(path.read_text())
        traders = snap["traders"]
        when = int(datetime.fromisoformat(snap["date"]).replace(tzinfo=UTC).timestamp() * 1000)
        rows = []
        lag = []
        ends = []
        invalid = []
        for trader in traders:
            address = trader["address"]
            if address not in cache:
                continue
            data = dict(cache[address]).get("allTime") or {}
            series = [data.get("accountValueHistory", []), data.get("pnlHistory", [])]
            if any(any(a[0] >= b[0] for a, b in zip(s, s[1:], strict=False)) for s in series):
                invalid.append(address)
            before = [[p for p in s if p[0] <= when] for s in series]
            if (
                not all(before)
                or float(before[0][-1][1]) <= 0
                or not series[1]
                or series[1][-1][0] <= when
            ):
                continue
            ret = (
                (float(series[1][-1][1]) - float(before[1][-1][1])) / float(before[0][-1][1]) * 100
            )
            rows.append((trader["sharpe"], ret))
            lag.append((when - before[1][-1][0]) / 86400000)
            ends.append(series[1][-1][0])
        rows.sort(reverse=True, key=lambda r: r[0])
        saved = [r for r in results if r["snapshot"] == snap["date"]]
        snaps.append(
            dict(
                date=snap["date"],
                n=len(traders),
                unique=len({t["address"] for t in traders}),
                cached=sum(t["address"] in cache for t in traders),
                recomputable=len(rows),
                invalid_series=invalid,
                lev_set=sum(t.get("lev_set") is not None for t in traders),
                saved=saved,
                baseline_lag_days_range=[min(lag), max(lag)] if lag else None,
                endpoint_utc_range=[
                    datetime.fromtimestamp(v / 1000, UTC).isoformat()
                    for v in [min(ends), max(ends)]
                ]
                if ends
                else None,
            )
        )
    candidates = json.loads((root / "candidates.json").read_text())
    print(
        json.dumps(
            dict(
                hashes=hashes,
                cache_n=len(cache),
                duplicate_cache_rows=duplicate_rows,
                candidates_n=len(candidates),
                nonpositive_pnl_candidates=sum(c["pnl_all"] <= 0 for c in candidates),
                forward_rows=len(results),
                snapshots=snaps,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
