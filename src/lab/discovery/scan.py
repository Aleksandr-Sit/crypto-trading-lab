"""`discovery.scan()` — обход источников, отпечаток, дедупликация, карточки (R15, R15.2).

Строка кандидата одна на (kind, ref) — та же, что заводит `core.registry`. Существенное
содержимое лежит в `payload.fingerprint`: пока оно не изменилось, отклонённый кандидат
в очередь не возвращается; изменилось — строка снова `pending` и уходит карточкой.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.contracts import CandidateDecision
from lab.core.registry import Candidate, Registry
from lab.db.base import utcnow
from lab.db.models import CandidateRow
from lab.discovery.config import DiscoveryConfig, load_discovery
from lab.discovery.types import CandidateSpec, fingerprint

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """Итог одного прогона поиска."""

    new: list[Candidate] = field(default_factory=list)
    updated: list[Candidate] = field(default_factory=list)  # вернулись: отпечаток изменился
    refreshed: int = 0  # уже в очереди, обновили содержимое
    seen: int = 0  # знакомые и неизменившиеся
    errors: dict[str, str] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    cards: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.new) + len(self.updated)

    def report(self) -> str:
        head = (
            f"Поиск кандидатов: новых {len(self.new)}, вернулись {len(self.updated)}, "
            f"без изменений {self.seen}"
        )
        by_source = ", ".join(f"{k}: {v}" for k, v in sorted(self.by_source.items()))
        lines = [head, f"источники — {by_source}" if by_source else "источники молчат"]
        for source_id, err in sorted(self.errors.items()):
            lines.append(f"⚠️ {source_id}: {err}")
        return "\n".join(lines)


def candidate_payload(candidate: Candidate, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Полезная нагрузка карточки `bot.send_card("candidate", ...)` (виды карточек — таск 05)."""
    return {
        "candidate_id": candidate.id,
        "kind": candidate.kind,
        "ref": candidate.ref,
        "summary": str(payload.get("summary") or "Взять в замер?"),
    }


class Discovery:
    """Поиск кандидатов. `sources` — что угодно с `fetch()`; сеть живёт внутри них."""

    def __init__(
        self,
        session: Session,
        *,
        sources: Sequence[Any] | None = None,
        bot: Any = None,
        config: DiscoveryConfig | None = None,
        clock: Any = None,
    ) -> None:
        self.s = session
        self.config = config or load_discovery()
        if sources is None:
            from lab.discovery.sources import default_sources

            sources = default_sources(config=self.config)
        self.sources = list(sources)
        self.bot = bot
        self._clock = clock or utcnow
        self.registry = Registry(session)

    # -- прогон ---------------------------------------------------------------------

    def scan(self, *, now: datetime | None = None) -> ScanResult:
        at = now or self._clock()
        result = ScanResult()
        for spec in self._collect(result):
            self._absorb(spec, at, result)
        self.s.flush()
        self._send_cards(result)
        log.info("%s", result.report())
        return result

    def _collect(self, result: ScanResult) -> list[CandidateSpec]:
        specs: list[CandidateSpec] = []
        keys: set[tuple[str, str]] = set()
        for source in self.sources:
            source_id = getattr(source, "id", source.__class__.__name__)
            try:
                fetched = list(source.fetch())
            except Exception as err:  # noqa: BLE001 — отказ ленты не роняет прогон (R25.2)
                result.errors[source_id] = f"{type(err).__name__}: {err}"
                log.warning("Источник %s не ответил: %s", source_id, err)
                continue
            result.by_source[source_id] = len(fetched)
            for spec in fetched:
                if spec.key in keys:  # дубликат = (venue, ref)
                    continue
                keys.add(spec.key)
                specs.append(spec if spec.source else _with_source(spec, source_id))
        return specs

    def _absorb(self, spec: CandidateSpec, at: datetime, result: ScanResult) -> None:
        fp = fingerprint(spec)
        row = self.s.scalar(
            select(CandidateRow)
            .where(CandidateRow.kind == spec.kind, CandidateRow.ref == spec.ref)
            .order_by(CandidateRow.id)
            .limit(1)
        )
        if row is None:
            payload = _payload(spec, fp, first_seen=at, last_seen=at)
            candidate = self.registry.enqueue_candidate(spec.kind, spec.ref, payload)
            result.new.append(candidate)
            return
        old = dict(row.payload or {})
        if old.get("fingerprint") == fp:
            row.payload = {**old, "last_seen": at.isoformat()}
            result.seen += 1
            return
        first_seen = old.get("first_seen") or at.isoformat()
        row.payload = _payload(spec, fp, first_seen=first_seen, last_seen=at)
        if row.decision == CandidateDecision.REJECTED.value:
            # R15.2: вернулся, потому что изменился существенно.
            row.decision = CandidateDecision.PENDING.value
            row.decided_at = None
            row.payload = {**row.payload, "returned_at": at.isoformat()}
            self.s.flush()
            result.updated.append(Candidate.from_row(row))
            return
        result.refreshed += 1

    def _send_cards(self, result: ScanResult) -> None:
        limit = self.config.max_cards_per_scan
        for candidate in (result.new + result.updated)[:limit]:
            row = self.s.get(CandidateRow, candidate.id)
            payload = candidate_payload(candidate, dict(row.payload or {}) if row else {})
            result.cards.append(payload)
            if self.bot is not None:
                self.bot.send_card_sync("candidate", payload)

    # -- решения оператора ----------------------------------------------------------

    def pending(self) -> list[Candidate]:
        return self.registry.candidates(CandidateDecision.PENDING)

    def accept(self, candidate_id: int, **kw: Any):
        from lab.discovery.decisions import decide

        return decide(self.s, candidate_id, "accept", **kw)

    def reject(self, candidate_id: int, *, reason: str = "", now: datetime | None = None):
        from lab.discovery.decisions import decide

        return decide(self.s, candidate_id, "reject", reason=reason, now=now)

    def postpone(self, candidate_id: int, *, now: datetime | None = None):
        from lab.discovery.decisions import decide

        return decide(self.s, candidate_id, "later", now=now)


def _with_source(spec: CandidateSpec, source_id: str) -> CandidateSpec:
    return CandidateSpec(**{**spec.__dict__, "source": source_id})


def _payload(
    spec: CandidateSpec, fp: str, *, first_seen: datetime | str, last_seen: datetime
) -> dict[str, Any]:
    raw = {
        "source": spec.source,
        "venue": spec.venue,
        "chain": spec.chain,
        "branch": spec.branch,
        "source_url": spec.source_url,
        "note": spec.note,
        "priority": spec.priority,
        "facts": spec.facts,
        "fingerprint": fp,
        "summary": spec.summary(),
        "first_seen": first_seen if isinstance(first_seen, str) else first_seen.isoformat(),
        "last_seen": last_seen.isoformat(),
    }
    return json.loads(json.dumps(raw, ensure_ascii=False, default=str))


def scan(session: Session, sources: Iterable[Any] | None = None, **kw: Any) -> ScanResult:
    """Модульный шов из спецификации: `discovery.scan() -> [Candidate]`."""
    return Discovery(session, sources=list(sources) if sources else None, **kw).scan()


__all__ = ["Discovery", "ScanResult", "candidate_payload", "scan"]
