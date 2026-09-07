"""Шов R15.1: кнопки карточки меняют candidates.decision, «в замер» — первый режим."""

from __future__ import annotations

from lab.contracts import CandidateDecision, MeasureMode
from lab.core.registry import Registry
from lab.discovery import CandidateSpec, Discovery, candidate_hook, decide


class FakeSource:
    id = "okx_lead"

    def __init__(self, specs):
        self._specs = specs

    def fetch(self):
        return list(self._specs)


def _queue(session, spec: CandidateSpec) -> int:
    return Discovery(session, sources=[FakeSource([spec])]).scan().new[0].id


TRADER = CandidateSpec(
    kind="trader", ref="1AB2C3", venue="okx", branch="copy", source="okx_lead", facts={"days": 300}
)
REPO = CandidateSpec(
    kind="strategy", ref="acme/grid-bot", venue="github", branch="cex-spot", source="github"
)


def test_accept_creates_strategy_and_starts_first_mode(session):
    cid = _queue(session, TRADER)
    runs: list[dict] = []

    result = decide(
        session, cid, "accept", measure=lambda **kw: runs.append(kw) or "measurement"
    )

    assert result.decision == CandidateDecision.ACCEPTED
    assert result.strategy_id == "copy-exchange-okx-1ab2c3"  # id реестра — в нижнем регистре
    # копитрейд не бэктестится (measure_plan) → первый режим бумажный
    assert result.mode == MeasureMode.PAPER
    assert runs and runs[0]["strategy_id"] == result.strategy_id
    assert runs[0]["mode"] == "paper"
    assert Registry(session).get(result.strategy_id) is not None
    assert Registry(session).candidates(CandidateDecision.ACCEPTED)[0].id == cid


def test_reject_and_later(session):
    cid = _queue(session, TRADER)

    decide(session, cid, "later")
    assert Registry(session).candidates()[0].decision == CandidateDecision.PENDING

    decide(session, cid, "reject", reason="мало сделок")
    assert Registry(session).candidates()[0].decision == CandidateDecision.REJECTED


def test_repo_candidate_is_accepted_without_manifest(session):
    cid = _queue(session, REPO)

    result = decide(session, cid, "accept")

    assert result.decision == CandidateDecision.ACCEPTED
    assert result.strategy_id is None
    assert "ручной манифест" in result.reason
    assert Registry(session).list() == []


def test_candidate_hook_matches_bot_callback(session):
    """`TraderBot(on_candidate=...)` зовёт хук как (ref_id, decision) строками."""
    cid = _queue(session, TRADER)

    class Scope:
        def __enter__(self):
            return session

        def __exit__(self, *a):
            return False

    hook = candidate_hook(Scope)
    hook(str(cid), "reject")

    assert Registry(session).candidates()[0].decision == CandidateDecision.REJECTED
