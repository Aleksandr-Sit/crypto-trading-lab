"""core.registry: add/get/list/retire/enqueue_candidate через публичный интерфейс."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from lab.contracts import Branch, CandidateDecision, Rung, Status
from lab.core.registry import (
    DuplicateStrategy,
    IncompleteManifest,
    Registry,
    StrategyNotFound,
)
from lab.db.models import CandidateRow

SPEC = {
    "slug": "ema-cross-v1",
    "branch": "cex-spot",
    "venue": "bybit",
    "source_kind": "preset",
    "instruments": ["BTC/USDT"],
    "timeframe": "1h",
    "params": {"fast": 12, "slow": 26},
    "can_backtest": True,
    "stop": {"max_dd_pct": 10},
}


def test_add_puts_candidate_with_readable_id(session):
    reg = Registry(session)
    strategy = reg.add(SPEC)
    assert strategy.id == "cex-spot-preset-ema-cross-v1"
    assert strategy.status == Status.CANDIDATE
    assert strategy.rung == Rung.BACKTEST
    assert strategy.branch == Branch.CEX_SPOT
    assert reg.get(strategy.id).params["fast"] == 12
    assert reg.get(strategy.id).stop.max_dd_pct == Decimal(10)


def test_duplicate_by_fingerprint_is_rejected_even_with_other_slug(session):
    reg = Registry(session)
    reg.add(SPEC)
    same_rules = {**SPEC, "slug": "ema-cross-v2"}
    with pytest.raises(DuplicateStrategy, match="уже есть"):
        reg.add(same_rules)
    # другие параметры — уже другая стратегия
    other = reg.add({**SPEC, "slug": "ema-cross-v2", "params": {"fast": 5, "slow": 20}})
    assert other.id == "cex-spot-preset-ema-cross-v2"


def test_incomplete_manifest_names_field_and_saves_draft(session):
    reg = Registry(session)
    broken = {k: v for k, v in SPEC.items() if k != "stop"}
    with pytest.raises(IncompleteManifest) as err:
        reg.add(broken)
    assert "stop" in err.value.fields
    assert "stop" in str(err.value)

    drafts = session.scalars(
        select(CandidateRow).where(CandidateRow.decision == CandidateDecision.DRAFT)
    ).all()
    assert len(drafts) == 1
    assert drafts[0].kind == "draft"
    assert drafts[0].ref == "cex-spot-preset-ema-cross-v1"
    assert drafts[0].payload["slug"] == "ema-cross-v1"
    assert reg.list() == []


def test_list_filters_by_branch_and_status(session):
    reg = Registry(session)
    reg.add(SPEC)
    reg.add({**SPEC, "slug": "pump-early-v1", "branch": "meme", "venue": "jupiter"})
    assert {s.id for s in reg.list()} == {
        "cex-spot-preset-ema-cross-v1",
        "meme-preset-pump-early-v1",
    }
    assert [s.branch for s in reg.list(branch="meme")] == [Branch.MEME]
    assert reg.list(status=Status.RETIRED) == []


def test_retire_sets_status_and_reason(session):
    reg = Registry(session)
    sid = reg.add(SPEC).id
    retired = reg.retire(sid, reason="EV<0 после издержек")
    assert retired.status == Status.RETIRED
    assert retired.retired_reason == "EV<0 после издержек"
    assert reg.list(status=Status.RETIRED)[0].id == sid
    with pytest.raises(StrategyNotFound):
        reg.retire("nope", reason="x")


def test_enqueue_candidate_dedups_by_fingerprint(session):
    reg = Registry(session)
    first = reg.enqueue_candidate("wallet", "0xabc")
    again = reg.enqueue_candidate("wallet", "0xabc")
    other = reg.enqueue_candidate("wallet", "0xdef")
    assert first.id == again.id
    assert first.id != other.id
    assert first.decision == CandidateDecision.PENDING
    assert len(reg.candidates()) == 2


def test_migration_creates_every_core_table(migrated_engine):
    from tests.conftest import table_names

    expected = {
        "strategies",
        "candidates",
        "measurements",
        "signals",
        "orders",
        "fills",
        "trades",
        "rung_transitions",
        "feeds",
        "wallets_tracked",
        "collections_tracked",
        "creators",
        "mints_upcoming",
        "signals_public",
        "allocations",
        "outbox",
    }
    assert expected <= table_names(migrated_engine)


def test_trades_table_has_column_for_every_cost_component(migrated_engine):
    from sqlalchemy import inspect

    from lab.contracts import Costs

    columns = {c["name"] for c in inspect(migrated_engine).get_columns("trades")}
    assert set(Costs.model_fields) <= columns
    assert {"royalty", "priority_fee"} <= columns
