"""Collect public hourly candles for reconstruction, outside production services.

Each symbol response is archived separately; errors remain explicit and resumable.
"""

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx

HOUR = 3_600_000
LOCK = threading.Lock()
NEXT_REQUEST = 0.0


def throttle():
    global NEXT_REQUEST
    with LOCK:
        now = time.monotonic()
        wait = max(0, NEXT_REQUEST - now)
        NEXT_REQUEST = max(now, NEXT_REQUEST) + 0.2
    if wait:
        time.sleep(wait)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    symbols = json.loads(args.symbols.read_text(encoding="utf-8-sig"))
    start = int(datetime(2026, 9, 7, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(2026, 10, 22, tzinfo=UTC).timestamp() * 1000)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "symbols.json").write_text(json.dumps(symbols), encoding="utf-8")

    def fetch(symbol):
        path = args.out / f"{symbol}.json"
        if path.exists() and json.loads(path.read_text())["complete"]:
            return symbol, "cached"
        rows, pages, cursor, error = [], [], start, None
        with httpx.Client(timeout=30) as client:
            try:
                while cursor < end:
                    throttle()
                    params = dict(
                        symbol=symbol, interval="1h", startTime=cursor, endTime=end - 1, limit=499
                    )
                    response = client.get("https://fapi.binance.com/fapi/v1/klines", params=params)
                    response.raise_for_status()
                    batch = response.json()
                    pages.append(
                        dict(
                            params=params,
                            fetched_at=datetime.now(UTC).isoformat(),
                            used_weight=response.headers.get("x-mbx-used-weight-1m"),
                        )
                    )
                    if not isinstance(batch, list):
                        raise ValueError("Unexpected non-list response")
                    rows.extend(batch)
                    if not batch or len(batch) < 499:
                        break
                    next_cursor = batch[-1][0] + HOUR
                    if next_cursor <= cursor:
                        raise ValueError("Non-advancing candle pagination")
                    cursor = next_cursor
            except (httpx.HTTPError, ValueError) as exc:
                error = str(exc)
        record = dict(
            symbol=symbol,
            start=start,
            end_exclusive=end,
            complete=error is None,
            error=error,
            pages=pages,
            rows=rows,
        )
        path.write_text(json.dumps(record), encoding="utf-8")
        return symbol, error or f"ok:{len(rows)}"

    selected = sorted(s for s, meta in symbols.items() if meta["onboard"] < end)
    failures = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, (symbol, status) in enumerate(pool.map(fetch, selected), 1):
            if not status.startswith(("ok:", "cached")):
                failures.append((symbol, status))
                print(symbol, status, flush=True)
            if i % 25 == 0:
                print(f"{i}/{len(selected)} errors={len(failures)}", flush=True)
    manifest = dict(
        start=start,
        end_exclusive=end,
        symbols=len(selected),
        failures=failures,
        finished_at=datetime.now(UTC).isoformat(),
    )
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    main()
