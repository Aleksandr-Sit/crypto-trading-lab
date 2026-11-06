#!/bin/bash
# Read-only archive/public-book checks using the currently running worker.
set -euo pipefail
ssh vps-trader 'cd /opt/crypto-trading-lab && docker compose -f deploy/docker-compose.yml exec -T worker uv run python -' <<'PY'
import json
from datetime import datetime, timedelta, timezone

from lab.data import CandleStore
from lab.feeds.cex.feed import make_feed
from lab.ops.measure import data_root

now = datetime.now(timezone.utc)
store = CandleStore(data_root())
feed = make_feed("binance")
result = {"captured_at": now.isoformat(), "instruments": {}}
for instrument in ("BTC/USDT", "PAXG/USDT"):
    item = {}
    try:
        bars = sorted(store.read("binance", instrument, "1d", now - timedelta(days=800), now),
                      key=lambda bar: bar.ts)
        item.update(bars=len(bars), first=bars[0].ts.isoformat() if bars else None,
                    last=bars[-1].ts.isoformat() if bars else None,
                    gaps=sum(b.ts - a.ts != timedelta(days=1)
                             for a, b in zip(bars, bars[1:])),
                    nonpositive_volume=sum(bar.volume <= 0 for bar in bars))
    except Exception as error:
        item["archive_error"] = type(error).__name__
    try:
        book = feed.book(instrument)
        item.update(book_ts=book.ts.isoformat(),
                    book_age_s=(datetime.now(timezone.utc) - book.ts).total_seconds(),
                    bid=str(book.bids[0].price) if book.bids else None,
                    ask=str(book.asks[0].price) if book.asks else None)
    except Exception as error:
        item["book_error"] = type(error).__name__
    result["instruments"][instrument] = item
print(json.dumps(result, indent=2))
PY
