"""Consolidate saved position snapshots without modifying their source files."""

import argparse
import hashlib
import json
from pathlib import Path

from crash_bounce_audit import merge_positions, ms, trader_summary


def consolidate(paths: list[Path], as_of: str) -> dict:
    cutoff = ms(as_of)
    positions, sources = [], []
    for path in sorted(set(paths)):
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
        if isinstance(data, dict) and data.get("fetched_at"):
            if ms(data["fetched_at"]) > cutoff:
                raise ValueError(f"snapshot newer than as-of: {path}")
        rows = data if isinstance(data, list) else data["positions"]
        positions = merge_positions(positions, rows)
        sources.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                        "positions": len(rows),
                        "snapshot_time_known": isinstance(data, dict)
                        and bool(data.get("fetched_at"))})
    return {"sources": sources, "summary": trader_summary(positions, as_of),
            "positions": positions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve() in {p.resolve() for p in args.sources}:
        raise ValueError("output must not overwrite a source")
    result = consolidate(args.sources, args.as_of)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
