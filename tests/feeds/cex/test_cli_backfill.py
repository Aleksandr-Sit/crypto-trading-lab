"""Шов R05.1: `lab data backfill --venue ... --symbols ... --tf ... --days N` с прогрессом."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import ccxt

from lab.cli import main
from lab.data import CandleStore
from lab.feeds.cex import FakeTransport, make_feed

PERP = "BTC/USDT:USDT"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_data_backfill_command_reports_progress_and_resume(tmp_path, monkeypatch, capsys):
    t = FakeTransport("bybit")
    t.seed_ohlcv(PERP, "1h", T0, 72, start_price=Decimal("50000"))
    t.now = T0 + timedelta(hours=72)
    monkeypatch.setattr(
        "lab.data.backfill_cex.make_feed",
        lambda venue, tr=None, *, quota=None: make_feed(venue, t, quota=quota),
    )
    monkeypatch.setattr("lab.data.backfill_cex._utcnow", lambda: T0 + timedelta(hours=72))

    t.fail_next = ccxt.NetworkError("bybit: connection lost")
    code = main(
        [
            "data",
            "backfill",
            "--venue",
            "bybit",
            "--symbols",
            PERP,
            "--tf",
            "1h",
            "--days",
            "3",
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1 and "прерван" in out and "connection lost" in out and "повтор" in out.lower()

    code = main(
        [
            "data",
            "backfill",
            "--venue",
            "bybit",
            "--symbols",
            f"{PERP},ETH/USDT:USDT",
            "--tf",
            "1h",
            "--days",
            "3",
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "72/72" in out and "ETH/USDT:USDT" in out
    assert CandleStore(tmp_path).count("bybit", PERP, "1h") == 72
