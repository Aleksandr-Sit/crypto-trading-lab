"""Кандидат от источника и общий интерфейс источников (R15).

Отпечаток считается по «существенным» полям (`facts`), а не по всему, что пришло:
иначе любое дрожание PnL на лидерборде воскрешало бы отклонённого кандидата (R15.2).
Что именно существенно — решает источник: он кладёт в `facts` огрублённые величины.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from lab.contracts import Health

KINDS = ("trader", "wallet", "channel", "creator", "strategy", "mint")


@dataclass(frozen=True)
class CandidateSpec:
    """Кандидат, каким его увидел источник. В базу едет через `Discovery.scan`."""

    kind: str
    ref: str
    venue: str = ""
    chain: str | None = None
    branch: str = ""
    source: str = ""
    source_url: str = ""
    note: str = ""
    priority: str = "auto"
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        """Дубликат — это (venue, ref): так же считает `candidates/seed.md`."""
        return (self.venue, self.ref)

    def summary(self) -> str:
        head = " · ".join(p for p in (self.venue, self.branch) if p)
        facts = ", ".join(f"{k}={_plain(v)}" for k, v in sorted(self.facts.items()))
        tail = " · ".join(p for p in (facts, self.note, self.source_url) if p)
        return f"{head}\n{tail}".strip() if tail else head


@runtime_checkable
class CandidateSource(Protocol):
    """Общий интерфейс источника: только чтение, без сети в конструкторе."""

    id: str

    def fetch(self) -> Sequence[CandidateSpec]: ...

    def health(self) -> Health: ...


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def fingerprint(spec: CandidateSpec) -> str:
    """sha256 по (kind, venue, ref, branch, chain, facts) — «существенное» в кандидате."""
    payload = json.dumps(
        _plain(
            {
                "kind": spec.kind,
                "venue": spec.venue,
                "ref": spec.ref,
                "branch": spec.branch,
                "chain": spec.chain,
                "facts": spec.facts,
            }
        ),
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bucket(value: Any, step: int) -> int | None:
    """Огрубление факта до шага: 63.4% при шаге 10 → 60. Пустое → None."""
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001 — нечисловой факт огрублять нечем
        return None
    return int(number // step) * step


__all__ = ["KINDS", "CandidateSource", "CandidateSpec", "bucket", "fingerprint"]
