import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Signal, StopSpec, StrategyManifest
from lab.core.measure import run

START = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW = (START, START + timedelta(seconds=10))


class RoundTrip:
    def on_bar(self, bar):
        return []

    def __init__(self, *, params=None, branch="cex-spot", instrument="SYN/USD", order="market"):
        self.manifest = StrategyManifest(
            slug="tape-test",
            branch=branch,
            venue="bybit",
            source_kind="test",
            instruments=[instrument],
            timeframe="1m",
            params={"execution_data": "trades", **(params or {})},
            stop=StopSpec(max_dd_pct=Decimal(50)),
        )
        self.order = order

    def on_event(self, event):
        seq = event.payload["sequence"]
        if seq not in (0, 2):
            return []
        return [
            Signal(
                strategy_id="x",
                decided_at=event.ts,
                instrument=self.manifest.instruments[0],
                side="buy" if seq == 0 else "sell",
                size=Decimal(1),
                price_ref=event.payload["price"],
                inputs_hash="h",
                ttl_s=5,
                meta={"order_type": self.order},
            )
        ]


def prints():
    return [
        {
            "sequence": i,
            "ts": (START + timedelta(milliseconds=250 * i)).isoformat(),
            "price": str(price),
            "qty": "20",
            "side": side,
        }
        for i, (price, side) in enumerate(
            [(100, "buy"), (99, "sell"), (101, "buy"), (102, "buy"), (99, "sell")]
        )
    ]


def write_tape(tmp_path, rows=None, **header_changes):
    header = {
        "format": "lab-trades/v1",
        "venue": "bybit",
        "instrument": "SYN/USD",
        "covered_from": WINDOW[0].isoformat(),
        "covered_to": WINDOW[1].isoformat(),
        "first_sequence": 0,
        "last_sequence": 4,
        **header_changes,
    }
    path = tmp_path / "trades.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in [header, *(prints() if rows is None else rows)]) + "\n",
        encoding="utf-8",
    )
    return path


def measure(path, strategy=None, **kwargs):
    return run(
        "cex-spot-tape-test",
        "backtest",
        WINDOW,
        strategy=strategy or RoundTrip(),
        tape_path=path,
        capital=Decimal(1000),
        **kwargs,
    )


def test_correct_aggressor_side_latency_and_no_double_spread(tmp_path):
    m = measure(write_tape(tmp_path))
    assert m.status == "ok"
    (t,) = m.trades
    assert t.entry_price == 101 and t.exit_price == 99
    assert t.opened_at == START + timedelta(milliseconds=500)
    assert t.closed_at == START + timedelta(seconds=1)
    assert t.pnl_net == Decimal("-2.2")  # -2 price move -0.101 entry fee -0.099 exit fee
    assert t.costs.slippage == 0
    assert m.metrics.execution["fills"] == 2
    assert m.metrics.execution["prints"] == 5
    assert m.metrics.trade_source == "simulation"


@pytest.mark.parametrize(
    "defect",
    [
        "gap",
        "duplicate",
        "reverse_time",
        "truncate",
        "empty",
        "zero_qty",
        "negative_price",
        "side",
        "naive",
        "outside",
    ],
)
def test_bad_tape_is_incomplete_without_metrics(tmp_path, defect):
    rows = prints()
    if defect == "gap":
        del rows[1]
    elif defect == "duplicate":
        rows[1]["sequence"] = 0
    elif defect == "reverse_time":
        rows[3]["ts"] = START.isoformat()
    elif defect == "truncate":
        rows.pop()
    elif defect == "empty":
        rows = []
    elif defect == "zero_qty":
        rows[1]["qty"] = "0"
    elif defect == "negative_price":
        rows[1]["price"] = "-1"
    elif defect == "side":
        rows[1]["side"] = "unknown"
    elif defect == "naive":
        rows[1]["ts"] = "2026-01-01T00:00:00"
    elif defect == "outside":
        rows[-1]["ts"] = WINDOW[1].isoformat()
    m = measure(write_tape(tmp_path, rows))
    assert m.status == "incomplete" and m.metrics is None and m.trades == []


@pytest.mark.parametrize(
    "changes",
    [{"venue": "binance"}, {"instrument": "OTHER/USD"}, {"covered_to": "2026-01-02T00:00:00Z"}],
)
def test_header_must_match_rules_and_window(tmp_path, changes):
    assert measure(write_tape(tmp_path, **changes)).status == "incomplete"


def test_required_tape_cannot_fall_back_to_candles(tmp_path):
    m = run("cex-spot-tape-test", "backtest", WINDOW, strategy=RoundTrip(), candles=[])
    assert m.status == "incomplete" and "лента" in m.reason


def test_unsupported_execution_is_explicit(tmp_path):
    path = write_tape(tmp_path)
    assert measure(path, RoundTrip(order="limit")).status == "incomplete"
    assert (
        measure(path, RoundTrip(branch="cex-perp", instrument="SYN/USD:USD")).status == "incomplete"
    )
    assert (
        measure(path, walk_forward=(timedelta(seconds=1), timedelta(seconds=1))).status
        == "incomplete"
    )
    assert run("x", "paper", WINDOW, strategy=RoundTrip(), tape_path=path).status == "incomplete"


def test_no_execution_on_same_timestamp(tmp_path):
    rows = prints()
    for row in rows:
        row["ts"] = START.isoformat()
    m = measure(write_tape(tmp_path, rows))
    assert m.status == "ok" and m.metrics.execution["fills"] == 0
    assert m.metrics.execution["expired"] == 2


def test_ttl_expires_before_first_eligible_print(tmp_path):
    rows = prints()
    for i, row in enumerate(rows):
        row["ts"] = (START + timedelta(seconds=2 * i)).isoformat()
    m = measure(write_tape(tmp_path, rows), RoundTrip(params={"execution_latency_ms": 5000}))
    assert m.status == "ok" and m.metrics.execution["fills"] == 0


def test_cash_inventory_and_shared_print_volume(tmp_path):
    class ManyBuys(RoundTrip):
        def on_event(self, event):
            result = super().on_event(event)
            return result * 3 if event.payload["sequence"] == 0 else []

    rows = prints()
    rows[2]["qty"] = "10"
    rows[3]["side"] = "sell"
    m = measure(write_tape(tmp_path, rows), ManyBuys())
    assert m.status == "ok"
    assert Decimal(m.metrics.execution["open_position"]) == 1
    assert m.metrics.execution["fills"] == 1
    m = run(
        "x",
        "backtest",
        WINDOW,
        strategy=ManyBuys(),
        tape_path=write_tape(tmp_path),
        capital=Decimal(1),
    )
    assert m.status == "ok"
    assert Decimal(m.metrics.execution["open_position"]) < Decimal("0.01")


def test_content_and_latency_change_cache_key(tmp_path, session):
    from lab.core.registry import Registry

    strategy = RoundTrip()
    record = Registry(session).add(strategy.manifest)
    path = write_tape(tmp_path)

    def run_once(instance):
        return run(
            record.id, "backtest", WINDOW, strategy=instance, tape_path=path, session=session
        )

    first = run_once(strategy)
    repeat = run_once(RoundTrip())
    assert first.id == repeat.id and repeat.cached
    rows = prints()
    rows[2]["price"] = "103"
    write_tape(tmp_path, rows)
    changed = run_once(RoundTrip())
    assert changed.data_hash != first.data_hash and changed.id != first.id
    delayed = run_once(RoundTrip(params={"execution_latency_ms": 500}))
    assert delayed.id != changed.id


def test_selling_without_inventory_never_opens_short(tmp_path):
    class Seller(RoundTrip):
        def on_event(self, event):
            return [s.model_copy(update={"side": "sell"}) for s in super().on_event(event)]

    m = measure(write_tape(tmp_path), Seller())
    assert m.status == "ok" and m.trades == []
    assert m.metrics.execution["open_position"] == "0"
    assert m.metrics.execution["fills"] == 0


def test_mark_to_market_stop_cancels_pending_buys(tmp_path):
    class Buyer(RoundTrip):
        def on_event(self, event):
            return [
                Signal(
                    strategy_id="x",
                    decided_at=event.ts,
                    instrument="SYN/USD",
                    side="buy",
                    size=Decimal(1),
                    price_ref=event.payload["price"],
                    inputs_hash="h",
                    ttl_s=5,
                )
            ]

    strategy = Buyer()
    strategy.manifest = strategy.manifest.model_copy(
        update={"stop": StopSpec(max_dd_pct=Decimal("0.05"))}
    )
    rows = prints()
    rows[1]["side"] = "buy"
    rows[2].update(side="sell", price="1")
    m = measure(write_tape(tmp_path, rows), strategy)
    assert m.status == "ok"
    assert m.metrics.stopped_at == START + timedelta(milliseconds=500)
    assert m.metrics.blocked_signals >= 1
    assert m.metrics.execution["fills"] == 1


@pytest.mark.parametrize("params", [{"execution_latency_ms": 0}, {"max_participation": "0"}])
def test_invalid_execution_config_is_incomplete(tmp_path, params):
    assert measure(write_tape(tmp_path), RoundTrip(params=params)).status == "incomplete"


def test_past_decision_cannot_be_inserted(tmp_path):
    class Late(RoundTrip):
        def on_event(self, event):
            return [
                s.model_copy(update={"decided_at": START - timedelta(seconds=1)})
                for s in super().on_event(event)
            ]

    assert measure(write_tape(tmp_path), Late()).status == "incomplete"


def test_cli_wrapper_uses_header_window_and_registers_attempt(
    tmp_path, session, monkeypatch, capsys
):
    from contextlib import contextmanager

    from sqlalchemy import select

    from lab import cli
    from lab.core.measure import history
    from lab.core.registry import Registry
    from lab.db.models import ResearchTrialRow
    from lab.ops import measure as ops_measure

    @contextmanager
    def scope():
        yield session

    strategy = RoundTrip()
    record = Registry(session).add(strategy.manifest)
    monkeypatch.setattr(cli, "_scope", lambda: scope)
    monkeypatch.setattr(ops_measure, "_build_strategy", lambda record: RoundTrip())
    path = write_tape(tmp_path)
    result = cli.main(["measure", "run", record.id, "--tape", str(path), "--root", str(tmp_path)])
    assert result == 0
    assert "задержка 250 мс" in capsys.readouterr().out
    (m,) = history(session, record.id)
    assert m.window_from == WINDOW[0] and m.window_to == WINDOW[1]
    assert m.status == "ok" and m.metrics.research["tracked"]
    trial = session.scalar(
        select(ResearchTrialRow).where(ResearchTrialRow.strategy_id == record.id)
    )
    assert trial.status == "ok" and trial.result["data_hash"] == m.data_hash
