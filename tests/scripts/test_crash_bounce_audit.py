"""Regressions for promotion, data coverage and calendar PnL correctness."""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


audit = load("crash_bounce_audit")
paper = load("crash_bounce_paper")
portfolio = load("crash_bounce_portfolio")
forward = load("crash_bounce_forward")
T = audit.ms("2026-10-10T00:00:00Z")


def trade(t, net=0.01):
    return {"acct": "x1", "sym": "A", "t": t, "t_in": t + 1,
            "t_out": t + 1000, "entry": 1.0, "low": 0.99, "net": net,
            "amt": 0.01, "pnl": 0.01 * net, "recv": t + 1001,
            "day": paper._day(t), "how": "tp"}


@pytest.mark.parametrize("net", [-0.01, 0.01])
def test_full_sample_never_approves_execution(net, capsys):
    rows = [trade(T + d * 86_400_000 + j * 3_600_000, net * (1 + (d % 3) / 10))
            for d in range(84) for j in range(4)]
    start = date(2026, 10, 10)
    end = date.fromordinal(start.toordinal() + 83)
    result = portfolio._forward_cell(rows, (start, end), start, 0.01, 3, 0.05)
    assert result["sample_ready"]
    assert result["ready"] is False
    assert result["archive_passed"] == (net > 0)
    assert "можно выносить" not in forward.report(result, end, "")
    assert "можно выносить" not in capsys.readouterr().out


def test_legacy_ready_does_not_authorize():
    result = {"ready": True, "days": 84, "trades": 500, "size": .01, "cap": 3,
              "max_drop": .05, "last_day": None, "total": -.1, "t": -2,
              "drop": .2, "k2": False, "worst_day": -.1}
    assert "можно выносить" not in forward.report(result, date(2026, 11, 9), "")


def test_receipt_cutoff_excludes_future_outcomes_and_reports_corruption(tmp_path):
    (tmp_path / "journal").mkdir()
    path = tmp_path / "journal/2026-10-10.jsonl"
    path.write_text(json.dumps({"k": "exit", "recv": T + 100}) + "\n{bad\n"
                    + json.dumps({"k": "exit", "recv": T + 200}) + "\n")
    rows, errors = audit.read_rows(tmp_path, T + 200)
    assert len(rows) == len(errors) == 1


def test_calendar_pnl_counts_cross_midnight_exit_and_rebases_drawdown():
    previous = {"k": "exit", **trade(T - 2000, 0.02)}
    cross = {"k": "exit", **trade(T - 100, -0.03)}
    later = {"k": "exit", **trade(T + 2000, 0.01)}
    result = audit.account_stats([previous, cross, later], T, T + 10_000)["x1"]
    assert result["closed_trades"] == 2
    assert result["opening_cumulative_pnl_d0"] == pytest.approx(.0002)
    assert result["realized_pnl_d0"] == pytest.approx(-.0002)
    assert result["max_realized_drawdown_d0"] == pytest.approx(.0003)
    assert result["mark_to_market_drawdown"] is None
    assert result["stress_extra_round_trip_bps"]["10"] < result["realized_pnl_d0"]


def test_quality_unions_overlap_and_clips_period():
    rows = [{"k": "gap", "T0": T - 100, "T1": T + 100},
            {"k": "gap", "T0": T + 50, "T1": T + 150},
            {"k": "conn", "event": "close", "conn": 0, "recv": T + 100},
            {"k": "conn", "event": "open", "conn": 0, "recv": T + 200}]
    result = audit.quality(rows, T, T + 1000)
    assert result["gap_union_ms"] == 150
    assert result["affected_union_ms"] == 200
    assert result["coverage_complete"] is False


def test_quality_coverage_requires_each_second_and_counts_degradation():
    row = {"k": "lat", "t": T, "p50": None, "quality_seconds": (1 << 60) - 1,
           "degraded_seconds": 3}
    result = audit.quality([row], T, T + 60_000)
    assert result["coverage_complete"]
    assert result["observed_seconds"] == 60
    assert result["affected_union_ms"] == 2000
    row["quality_seconds"] ^= 1 << 30
    assert not audit.quality([row], T, T + 60_000)["coverage_complete"]


def test_account_cutoff_keeps_future_exit_pending():
    row = trade(T)
    filled = {**row, "k": "fill"}
    exited = {**row, "k": "exit"}
    result = audit.account_stats([filled, exited], T, T + 500)["x1"]
    assert result["closed_trades"] == 0
    assert result["pending_positions"] == 1


def test_reconcile_requires_symbol_day_and_excludes_future_exit(tmp_path):
    a = {**trade(T), "var": "limit5|side"}
    b = {**a, "sym": "B"}
    c = {**a, "sym": "C", "t_out": T + 10_000}
    (tmp_path / "sim_done.txt").write_text("A:2026-10-10 C:2026-10-10")
    (tmp_path / "sim.jsonl").write_text("\n".join(map(json.dumps, [a, b, c])))
    result = audit.reconcile([], tmp_path, T, T + 5000)
    assert result["counts"] == {"missing": 1, "archive_incomplete": 1,
                                 "outcome_after_cutoff": 1, "comparable": 1}
    assert result["missing"][0]["symbol"] == "A"


def position(updated, closed=None, pnl=1):
    return {"positionId": "1", "symbol": "A", "opened": T,
            "closed": closed, "updateTime": updated, "closingPnl": pnl,
            "leverage": "1", "side": "Long"}


def test_merge_updates_partial_and_rejects_stale_overwrite():
    partial = position(T + 1)
    closed = position(T + 10, T + 10, 2)
    merged = audit.merge_positions([partial], [closed, partial])
    assert merged == [closed]
    assert audit.merge_positions(merged, [closed]) == merged
    summary = audit.trader_summary(merged, "2026-11-09T12:00:00Z")
    assert summary["closed"]["closing_pnl_usdt"] == 2
    assert summary["rule_identified"] is False
    assert summary["complete_history"] is False


def test_merge_refuses_missing_identity():
    with pytest.raises(ValueError, match="identity"):
        audit.merge_positions([], [{"opened": T, "symbol": "A"}])


def test_merge_identity_survives_open_time_correction_and_no_equal_time_downgrade():
    old = position(T + 10, T + 10, 2)
    corrected = {**old, "opened": T - 1, "updateTime": T + 11}
    assert audit.merge_positions([old], [corrected]) == [corrected]
    partial = {**corrected, "closed": None}
    assert audit.merge_positions([corrected], [partial]) == [corrected]


@pytest.mark.parametrize("offset", [0, 249, 250, 549, 59_700, 59_999])
def test_seam_account_never_uses_stale_order_during_gap(offset):
    engine = paper.Engine(lambda row: None)
    state = paper.SymState(snap={T - 60_000: 95.0, T: 90.0})
    assert engine.x1_level(state, T + offset, paper.PULL_MS) is None


def test_seam_account_accepts_only_current_snapshot_after_switch():
    engine = paper.Engine(lambda row: None)
    state = paper.SymState(snap={T - 60_000: 95.0, T: 90.0})
    assert engine.x1_level(state, T + 550, paper.PULL_MS) == 90.0
    state.snap.pop(T)
    assert engine.x1_level(state, T + 1000, paper.PULL_MS) is None


def test_asof_requires_timezone_and_rejects_reversed_period():
    with pytest.raises(ValueError, match="timezone"):
        audit.ms("2026-11-09T00:00:00")
    with pytest.raises(ValueError, match="interval"):
        audit.bounds("2026-11-10", "2026-11-09", T)


def test_healthy_process_with_stale_stream_is_not_healthy():
    state = {"checked_at": T, "expected_connections": [0, 1],
             "latest_event_ms": {"0": T, "1": T}, "consumer_at": T, "queue_delay_ms": 1}
    assert paper.quality_health(state, T)
    state["latest_event_ms"]["1"] -= 6000
    assert not paper.quality_health(state, T)
    state["latest_event_ms"]["1"] = T
    state["queue_delay_ms"] = 6000
    assert not paper.quality_health(state, T)
    assert not paper.quality_health({}, T)


def test_unknown_quality_or_failed_cell_blocks_review():
    summary = {"days": 84, "trades": 400, "k1": True, "k2": True}
    accounts = {"x1": {"closed_trades": 10, "realized_pnl_d0": .01}}
    reconciliation = {"observed_criterion_passed": True}
    quality = {"coverage_complete": False, "window_ms": 1000, "affected_union_ms": 0}
    cell = audit.evaluate_cells(summary, accounts, reconciliation, quality)[0]
    assert cell["checks"]["k4"] is None
    assert not cell["ready_for_owner_review"]
    quality["coverage_complete"] = True
    assert audit.evaluate_cells(summary, accounts, reconciliation, quality)[0][
        "ready_for_owner_review"]
    summary["k2"] = False
    assert not audit.evaluate_cells(summary, accounts, reconciliation, quality)[0][
        "ready_for_owner_review"]


def test_history_consolidation_preserves_sources_and_rejects_future(tmp_path):
    history = load("cryptosmx_history")
    source = tmp_path / "source.json"
    raw = json.dumps({"fetched_at": "2026-11-09T00:00:00Z",
                      "positions": [position(T + 1)]})
    source.write_text(raw)
    result = history.consolidate([source], "2026-11-09T01:00:00Z")
    assert result["summary"]["open_or_partial"] == 1
    assert len(result["sources"][0]["sha256"]) == 64
    assert source.read_text() == raw
    with pytest.raises(ValueError, match="newer"):
        history.consolidate([source], "2026-11-08T00:00:00Z")


def test_snapshot_pipeline_keeps_raw_history_and_updates_partial(tmp_path, monkeypatch):
    old = position(T + 1)
    new = position(T + 10, T + 10, 2)
    (tmp_path / "cryptosmx-positions.json").write_text(json.dumps([old]))
    snapshot = {"fetched_at": "2026-11-09T08:26:07+00:00", "positions": [new]}
    (tmp_path / "cryptosmx-snapshot.json").write_text(json.dumps(snapshot))
    monkeypatch.setattr(forward, "_run", lambda args: 0)
    forward.snapshot_trader(tmp_path)
    forward.snapshot_trader(tmp_path)
    assert json.loads((tmp_path / "cryptosmx-positions.json").read_text()) == [new]
    assert len(list((tmp_path / "cryptosmx-snapshots").glob("*.json"))) == 1
    coverage = json.loads((tmp_path / "cryptosmx-coverage.json").read_text())
    assert coverage["closed"]["count"] == 1
    assert coverage["complete_history"] is False
