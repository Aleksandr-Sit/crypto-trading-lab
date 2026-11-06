"""Global search ledger: no family reset by renaming, retiring or deleting a strategy.

Only attempts through the common wrapper are covered. Historical scripts cannot be
retroactively reconstructed; the recorded count is explicitly a lower bound.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import distinct, func, select

from lab.db.base import utcnow
from lab.db.models import ResearchTrialRow


def variant_count(session) -> int:
    return session.scalar(select(func.count(distinct(ResearchTrialRow.variant_hash)))) or 0


def begin(session, strategy_id: str, spec: dict) -> dict:
    normalized = json.loads(json.dumps(spec, sort_keys=True, default=str))
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    trial = ResearchTrialRow(strategy_id=strategy_id, variant_hash=digest, spec=normalized)
    session.add(trial)
    session.flush()
    return {
        "tracked": True, "trial_id": trial.id, "variant_hash": digest,
        "variants": variant_count(session), "scope": "global_recorded_search",
        "coverage": "common_wrapper_only; historical_and_external_trials_unknown",
    }


def finish(
    session, trial_id: int, status: str, reason: str = "", result: dict | None = None,
) -> None:
    trial = session.get(ResearchTrialRow, trial_id)
    trial.status, trial.reason, trial.finished_at = status, reason, utcnow()
    trial.result = json.loads(json.dumps(result or {}, default=str))
    session.flush()
