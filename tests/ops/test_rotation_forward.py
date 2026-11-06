import pytest
from sqlalchemy import select

from lab.core.forward import current_observation
from lab.core.registry import Registry
from lab.db.models import ForwardObservationRow, RungTransitionRow, StrategyRow, SystemFlagRow
from lab.ops.rotation_forward import STRATEGY_ID, prepare_rotation
from lab.strategies.registry import build


@pytest.fixture
def rotation(session):
    record = Registry(session).add(build(STRATEGY_ID).manifest)
    row = session.get(StrategyRow, record.id)
    row.rung = "semi"
    row.status = "measuring"
    session.flush()
    return row


def test_preview_preserves_rung_and_does_not_start_observation(session, rotation):
    assert not prepare_rotation(session)["applied"]
    assert rotation.rung == "semi"
    assert list(session.scalars(select(ForwardObservationRow))) == []
    assert list(session.scalars(select(RungTransitionRow))) == []


def test_repair_keeps_audit_trail_and_starts_only_one_contract(session, rotation):
    assert prepare_rotation(session, apply=True)["applied"]
    assert rotation.rung == "paper"
    transitions = list(session.scalars(select(RungTransitionRow).order_by(RungTransitionRow.id)))
    assert [(r.from_rung, r.to_rung) for r in transitions] == [
        ("semi", "signal"), ("signal", "micro"), ("micro", "paper"),
    ]
    assert all(r.by == "operator" and r.metrics_snapshot["audit"] for r in transitions)
    assert len(list(session.scalars(select(ForwardObservationRow)))) == 1
    with pytest.raises(ValueError, match="differs"):
        prepare_rotation(session, apply=True)
    assert len(list(session.scalars(select(RungTransitionRow)))) == 3


@pytest.mark.parametrize("field,value", [
    ("params_json", {}), ("rung", "auto"), ("status", "degraded"),
    ("instruments", ["BTC/USDT"]),
])
def test_refuses_changed_contract(session, rotation, field, value):
    setattr(rotation, field, value)
    with pytest.raises(ValueError, match="differs"):
        prepare_rotation(session, apply=True)
    assert list(session.scalars(select(RungTransitionRow))) == []


@pytest.mark.parametrize("existing", ["observation", "collector"])
def test_refuses_existing_forward_state(session, rotation, existing):
    if existing == "observation":
        current_observation(session, STRATEGY_ID, start=True)
    else:
        session.add(SystemFlagRow(key=f"virtual_forward:{STRATEGY_ID}",
                                  value={}, updated_by="worker"))
        session.flush()
    with pytest.raises(ValueError, match="existing|already exists"):
        prepare_rotation(session, apply=True)
    assert rotation.rung == "semi"
    assert list(session.scalars(select(RungTransitionRow))) == []
