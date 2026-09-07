"""Швы R14/R14.1/G05/G10: переизмерение, срок годности, перелив, напоминание G10."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Status
from lab.core.registry import Registry
from lab.db.models import StrategyRow
from lab.executors.cex.copy_exchange import make_copy_exchange_strategy
from lab.ops.jobs import (
    SeedReminder,
    confirm_rebalance,
    expiry,
    proposals,
    rebalance_proposal,
    weekly_remeasure,
)

NOW = datetime(2026, 9, 6, 22, 0, tzinfo=UTC)


class Scope:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *a):
        return False


class FakeBot:
    def __init__(self) -> None:
        self.cards: list[tuple[str, dict]] = []

    def send_card_sync(self, kind: str, payload: dict) -> int:
        self.cards.append((kind, dict(payload)))
        return len(self.cards)


class FakeLadder:
    def __init__(self, session) -> None:
        self.session = session
        self.evaluated: list[str] = []
        self.demoted: list[tuple[str, str]] = []

    def start(self, strategy_id: str) -> None:
        pass

    def evaluate(self, strategy_id: str, metrics=None) -> None:
        self.evaluated.append(strategy_id)
        return None

    def demote(self, strategy_id: str, reason: str, **kw) -> None:
        self.demoted.append((strategy_id, reason))
        return None


@dataclass
class FakeAllocation:
    branch: str
    base_usd: Decimal
    current_usd: Decimal
    stale: bool = False


class FakeRisk:
    def __init__(self, allocations: dict[str, FakeAllocation]) -> None:
        self._a = allocations

    def allocation(self, branch: str) -> FakeAllocation:
        return self._a[branch]


@pytest.fixture
def strategy(session):
    manifest = make_copy_exchange_strategy("okx", "L1").manifest
    return Registry(session).add(manifest)


def _row(session, strategy_id: str) -> StrategyRow:
    return session.get(StrategyRow, strategy_id)


def test_weekly_remeasure_measures_live_strategies(session, strategy):
    _row(session, strategy.id).status = Status.PASSED.value
    session.flush()
    ladder = FakeLadder(session)
    bot = FakeBot()
    calls: list[dict] = []

    report = weekly_remeasure(
        Scope(session),
        measure=lambda **kw: calls.append(kw) or "ok",
        ladder_factory=lambda s: ladder,
        bot=bot,
        now=NOW,
    )

    assert calls[0]["strategy_id"] == strategy.id
    assert calls[0]["window"][1] == NOW
    assert (NOW - calls[0]["window"][0]).days == 90  # окно из config/discovery.yaml
    assert ladder.evaluated == [strategy.id]
    assert report.measured == [strategy.id]
    # успешное переизмерение продлевает срок годности (R14.1)
    assert _row(session, strategy.id).valid_until == NOW + timedelta(weeks=4)
    assert bot.cards and bot.cards[0][0] == "alert"


def test_weekly_remeasure_survives_broken_measure(session, strategy):
    _row(session, strategy.id).status = Status.MEASURING.value
    session.flush()
    ladder = FakeLadder(session)

    def boom(**kw):
        raise RuntimeError("нет свечей")

    report = weekly_remeasure(
        Scope(session), measure=boom, ladder_factory=lambda s: ladder, now=NOW
    )

    assert report.measured == []
    assert "нет свечей" in report.failed[strategy.id]
    assert _row(session, strategy.id).valid_until is None


def test_expiry_degrades_strategy_past_valid_until(session, strategy):
    row = _row(session, strategy.id)
    row.status = Status.PASSED.value
    row.valid_until = NOW - timedelta(days=1)
    session.flush()
    ladder = FakeLadder(session)
    bot = FakeBot()

    expired = expiry(Scope(session), ladder_factory=lambda s: ladder, bot=bot, now=NOW)

    assert expired == [strategy.id]
    assert _row(session, strategy.id).status == Status.DEGRADED.value
    assert ladder.demoted and "срок годности" in ladder.demoted[0][1]
    assert bot.cards


def test_expiry_leaves_fresh_strategy_alone(session, strategy):
    row = _row(session, strategy.id)
    row.status = Status.PASSED.value
    row.valid_until = NOW + timedelta(days=1)
    session.flush()

    assert expiry(Scope(session), ladder_factory=lambda s: FakeLadder(s), now=NOW) == []
    assert _row(session, strategy.id).status == Status.PASSED.value


def test_rebalance_proposal_offers_surplus_over_base(session):
    risk = FakeRisk(
        {
            "meme": FakeAllocation("meme", Decimal(200), Decimal(500)),
            "nft": FakeAllocation("nft", Decimal(100), Decimal(120)),  # излишек 20 < порога 50
            "prediction": FakeAllocation("prediction", Decimal(50), Decimal(10)),
            "copy": FakeAllocation("copy", Decimal(250), Decimal(250)),
        }
    )
    bot = FakeBot()

    made = rebalance_proposal(Scope(session), risk=risk, bot=bot, now=NOW)

    assert [(p.from_branch, p.amount_usd) for p in made] == [("meme", Decimal(300))]
    assert made[0].to_branch == "cex-spot" and made[0].status == "proposed"
    kind, payload = bot.cards[0]
    assert kind == "rebalance" and payload["proposal_id"] == made[0].id
    assert payload["amount_usd"] == "300"


def test_rebalance_is_recorded_only_after_confirmation(session):
    risk = FakeRisk({"meme": FakeAllocation("meme", Decimal(200), Decimal(500))})
    made = rebalance_proposal(
        Scope(session), risk=risk, branches=["meme"], bot=None, now=NOW
    )
    proposal = made[0]

    confirmed = confirm_rebalance(session, proposal.id, "apply", now=NOW)

    assert confirmed.status == "moved" and confirmed.decided_at == NOW
    assert proposals(session, status="moved")[0].id == proposal.id
    # повторный прогон не плодит предложение по той же ветке
    again = rebalance_proposal(Scope(session), risk=risk, branches=["meme"], now=NOW)
    assert again == []


def test_rebalance_skip_keeps_money_in_place(session):
    risk = FakeRisk({"meme": FakeAllocation("meme", Decimal(200), Decimal(500))})
    proposal = rebalance_proposal(Scope(session), risk=risk, branches=["meme"], now=NOW)[0]

    assert confirm_rebalance(session, proposal.id, "skip", now=NOW).status == "skipped"


def test_jobs_are_scheduled_from_schedule_yaml(session):
    from lab.ops.jobs import jobs
    from lab.ops.scheduler import Scheduler

    scheduler = Scheduler()
    registered = [
        scheduler.register(job)
        for job in jobs(
            Scope(session),
            measure=lambda **kw: None,
            ladder_factory=FakeLadder,
            risk=FakeRisk({}),
            reminder=SeedReminder(Scope(session)),
        )
    ]

    assert {j.id for j in registered} == {
        "discovery",
        "remeasure",
        "expiry",
        "rebalance_proposal",
        "seed_reminder",
    }
    assert scheduler.tz.key == "Europe/Samara"
    assert scheduler.config.jobs["discovery"].cron == "0 6 * * mon"
    assert scheduler.config.jobs["remeasure"].cron == "0 22 * * sun"
    assert scheduler.config.jobs["rebalance_proposal"].cron == "30 22 * * sun"
    assert set(scheduler.jobs()) == {j.id for j in registered}


def test_seed_reminder_fires_once_after_first_status(session):
    bot = FakeBot()
    reminder = SeedReminder(Scope(session), bot=bot)

    reminder.run()  # /status ещё не был — молчим
    assert bot.cards == []

    reminder.arm()
    reminder.run()
    reminder.run()

    assert len(bot.cards) == 1
    kind, payload = bot.cards[0]
    assert kind == "alert"
    assert "кандидат" in payload["detail"].lower() or "кандидат" in payload["title"].lower()
