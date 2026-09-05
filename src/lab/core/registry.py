"""core.registry — реестр стратегий и очередь кандидатов.

Выставляет: add(spec) -> Strategy, get(id), list(filter), retire(id, reason),
enqueue_candidate(kind, ref). Прячет генерацию id и отпечатки дубликатов.
"""

from __future__ import annotations  # метод Registry.list затеняет builtins.list в аннотациях

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.contracts import Branch, CandidateDecision, Rung, Status, StopSpec, StrategyManifest
from lab.db.base import utcnow
from lab.db.models import CandidateRow, StrategyRow


class RegistryError(Exception):
    pass


class DuplicateStrategy(RegistryError):
    def __init__(self, existing_id: str) -> None:
        self.existing_id = existing_id
        super().__init__(f"такая стратегия уже есть: {existing_id}")


class StrategyNotFound(RegistryError):
    pass


class IncompleteManifest(RegistryError):
    """Манифест не прошёл валидацию; черновик сохранён в candidates (kind='draft')."""

    def __init__(self, fields: dict[str, str], draft_id: int | None) -> None:
        self.fields = fields
        self.draft_id = draft_id
        detail = "; ".join(f"{name}: {msg}" for name, msg in fields.items())
        super().__init__(f"неполный манифест — {detail} (черновик #{draft_id})")


class Strategy(BaseModel):
    """Запись реестра, как её видят другие модули."""

    model_config = ConfigDict(frozen=True)

    id: str
    slug: str
    branch: Branch
    venue: str
    source_kind: str
    source_ref: str | None
    instruments: list[str]
    timeframe: str | None
    rung: Rung
    status: Status
    params: dict[str, Any]
    can_backtest: bool
    stop: StopSpec
    valid_until: datetime | None
    description: str
    created_at: datetime
    retired_reason: str | None

    @classmethod
    def from_row(cls, row: StrategyRow) -> Strategy:
        return cls(
            id=row.id,
            slug=row.slug,
            branch=Branch(row.branch),
            venue=row.venue,
            source_kind=row.source_kind,
            source_ref=row.source_ref,
            instruments=list(row.instruments),
            timeframe=row.timeframe,
            rung=Rung(row.rung),
            status=Status(row.status),
            params=dict(row.params_json),
            can_backtest=row.can_backtest,
            stop=StopSpec.model_validate(row.stop_json),
            valid_until=row.valid_until,
            description=row.description,
            created_at=row.created_at,
            retired_reason=row.retired_reason,
        )


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    kind: str
    ref: str
    decision: CandidateDecision
    discovered_at: datetime
    decided_at: datetime | None
    error: str | None = None

    @classmethod
    def from_row(cls, row: CandidateRow) -> Candidate:
        return cls(
            id=row.id,
            kind=row.kind,
            ref=row.ref,
            decision=CandidateDecision(row.decision),
            discovered_at=row.discovered_at,
            decided_at=row.decided_at,
            error=row.error,
        )


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return value


def _fingerprint(*parts: Any) -> str:
    payload = json.dumps(_canonical(list(parts)), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strategy_fingerprint(m: StrategyManifest) -> str:
    """Отпечаток правил: ветка + площадка + источник + инструменты + параметры. Slug не входит."""
    return _fingerprint(
        m.branch,
        m.venue,
        m.source_kind,
        m.source_ref,
        sorted(m.instruments),
        m.timeframe,
        m.params,
        m.stop.model_dump(),
    )


def _strategy_id(m: StrategyManifest) -> str:
    return f"{m.branch}-{m.source_kind}-{m.slug}"


def _guess_ref(spec: Mapping[str, Any]) -> str:
    parts = [str(spec.get(k) or "?") for k in ("branch", "source_kind", "slug")]
    return "-".join(parts)


class Registry:
    def __init__(self, session: Session) -> None:
        self._s = session

    # -- стратегии ------------------------------------------------------------

    def add(self, spec: Mapping[str, Any] | StrategyManifest) -> Strategy:
        if isinstance(spec, StrategyManifest):
            manifest = spec
        else:
            try:
                manifest = StrategyManifest.model_validate(dict(spec))
            except ValidationError as err:
                fields = {
                    ".".join(str(p) for p in e["loc"]) or "<корень>": e["msg"] for e in err.errors()
                }
                draft_id = self._save_draft(spec, fields)
                raise IncompleteManifest(fields, draft_id) from err

        fingerprint = _strategy_fingerprint(manifest)
        existing = self._s.scalar(select(StrategyRow).where(StrategyRow.fingerprint == fingerprint))
        if existing is not None:
            raise DuplicateStrategy(existing.id)
        strategy_id = _strategy_id(manifest)
        if self._s.get(StrategyRow, strategy_id) is not None:
            raise DuplicateStrategy(strategy_id)

        row = StrategyRow(
            id=strategy_id,
            slug=manifest.slug,
            branch=manifest.branch.value,
            venue=manifest.venue,
            source_kind=manifest.source_kind,
            source_ref=manifest.source_ref,
            instruments=list(manifest.instruments),
            timeframe=manifest.timeframe,
            rung=Rung.BACKTEST.value,
            status=Status.CANDIDATE.value,
            params_json=_canonical(manifest.params),
            can_backtest=manifest.can_backtest,
            stop_json=_canonical(manifest.stop.model_dump()),
            valid_until=manifest.valid_until,
            description=manifest.description,
            fingerprint=fingerprint,
        )
        self._s.add(row)
        self._s.flush()
        return Strategy.from_row(row)

    def get(self, strategy_id: str) -> Strategy:
        row = self._s.get(StrategyRow, strategy_id)
        if row is None:
            raise StrategyNotFound(f"стратегия {strategy_id} не найдена")
        return Strategy.from_row(row)

    def list(  # noqa: A003 — имя из спецификации
        self,
        *,
        branch: Branch | str | None = None,
        status: Status | str | None = None,
        rung: Rung | str | None = None,
        venue: str | None = None,
    ) -> list[Strategy]:
        stmt = select(StrategyRow).order_by(StrategyRow.created_at, StrategyRow.id)
        if branch is not None:
            stmt = stmt.where(StrategyRow.branch == Branch(branch).value)
        if status is not None:
            stmt = stmt.where(StrategyRow.status == Status(status).value)
        if rung is not None:
            stmt = stmt.where(StrategyRow.rung == Rung(rung).value)
        if venue is not None:
            stmt = stmt.where(StrategyRow.venue == venue)
        return [Strategy.from_row(r) for r in self._s.scalars(stmt).all()]

    def retire(self, strategy_id: str, reason: str) -> Strategy:
        row = self._s.get(StrategyRow, strategy_id)
        if row is None:
            raise StrategyNotFound(f"стратегия {strategy_id} не найдена")
        row.status = Status.RETIRED.value
        row.retired_reason = reason
        self._s.flush()
        return Strategy.from_row(row)

    # -- кандидаты ------------------------------------------------------------

    def enqueue_candidate(
        self, kind: str, ref: str, payload: Mapping[str, Any] | None = None
    ) -> Candidate:
        fingerprint = _fingerprint("candidate", kind, ref)
        row = self._s.scalar(select(CandidateRow).where(CandidateRow.fingerprint == fingerprint))
        if row is None:
            row = CandidateRow(
                kind=kind,
                ref=ref,
                fingerprint=fingerprint,
                payload=_canonical(dict(payload or {})),
                decision=CandidateDecision.PENDING.value,
            )
            self._s.add(row)
            self._s.flush()
        return Candidate.from_row(row)

    def candidates(self, decision: CandidateDecision | str | None = None) -> list[Candidate]:
        stmt = select(CandidateRow).order_by(CandidateRow.discovered_at, CandidateRow.id)
        if decision is not None:
            stmt = stmt.where(CandidateRow.decision == CandidateDecision(decision).value)
        return [Candidate.from_row(r) for r in self._s.scalars(stmt).all()]

    def _save_draft(self, spec: Mapping[str, Any], fields: dict[str, str]) -> int:
        ref = _guess_ref(spec)
        payload = _canonical(dict(spec))
        fingerprint = _fingerprint("draft", ref, payload)
        row = self._s.scalar(select(CandidateRow).where(CandidateRow.fingerprint == fingerprint))
        error = "; ".join(f"{k}: {v}" for k, v in fields.items())
        if row is None:
            row = CandidateRow(
                kind="draft",
                ref=ref,
                fingerprint=fingerprint,
                payload=payload,
                error=error,
                decision=CandidateDecision.DRAFT.value,
            )
            self._s.add(row)
        else:
            row.error = error
            row.discovered_at = utcnow()
        self._s.flush()
        return row.id
