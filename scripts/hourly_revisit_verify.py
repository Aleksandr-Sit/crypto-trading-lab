"""Verify archived hourly evidence without network requests or new hypotheses."""

import argparse
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    source = root / "hourly_whitelist_audit_2026-11-07.json"
    recheck = root / "hourly_whitelist_recheck_2026-11-09.json"
    saved = read(source)
    if saved != read(recheck):
        raise ValueError("Whitelist recomputation differs from saved evidence")
    verified = {}
    for name, expected in saved["hashes"].items():
        if name.startswith("candles/"):
            path = root / "hourly_universe1h_2026-11-07" / name.removeprefix("candles/")
        elif name.startswith("delay/"):
            path = root / "hourly_delay_2026-11-07" / name.removeprefix("delay/")
        elif name.endswith(".py"):
            path = Path(__file__).parent / name
        else:
            path = root / name
        actual = digest(path)
        if actual != expected:
            raise ValueError(f"Source hash mismatch: {name}")
        verified[name] = actual
    delay_path = root / "hourly_delay_2026-11-07" / "audit.json"
    delay = read(delay_path)
    delay_root = delay_path.parent
    for name, expected in delay["candle_hashes"].items():
        path = delay_root / name
        if digest(path) != expected:
            raise ValueError(f"Minute source hash mismatch: {name}")
    result = {
        "method": "hourly_archived_evidence_revisit_v1",
        "date": "2026-11-09",
        "portfolio_ids": ["4883987534523154688", "4894215989067406336", "4693119134299763969"],
        "status": "rule_unidentified",
        "decision": "defer_until_new_dated_signal_or_filter_source",
        "profitability_tested": False,
        "new_market_requests": 0,
        "new_hypotheses_tested": 0,
        "fresh_independent_validation": False,
        "whitelist_recomputation_equal": True,
        "verified_whitelist_source_hashes": verified,
        "verified_minute_source_count": len(delay["candle_hashes"]),
        "audit_hashes": {
            source.name: digest(source),
            recheck.name: digest(recheck),
            "hourly_delay_2026-11-07/audit.json": digest(delay_path),
        },
        "delay_summary": delay["summary"],
        "joint_summary": saved["joint_fixed_list_and_delay_summary"],
        "limitations": [
            "September and October were already explored; no fresh holdout claimed.",
            "No new dated operator signal, universe or rejection log found in reviewed archive.",
            "No claim that an external source cannot exist.",
            "Counterexamples reject only the previously specified simple explanations.",
            "No intraminute, spread, dynamic universe or size-filter reconstruction.",
            "Rule unidentified does not imply unprofitable.",
        ],
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Verified {len(verified)} hourly sources and {len(delay['candle_hashes'])} minute sources"
    )


if __name__ == "__main__":
    main()
