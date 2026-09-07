"""Журнал публичных сигналов авторов — таблица `signals_public` (R06.2, G08.1).

Порядок жёсткий: `record()` пишет `published_at`, инструмент, направление, цель **до** любой оценки
(`outcome_json` пустой); `evaluate()` дописывает исход по факту (цель/стоп/истёк) по ценам после
`published_at`; `forward_measure()` собирает исходы автора в `ClosedTrade` и считает метрики через
`core.measure.run(mode="forward")` — без исполнения, стратегия `cex-spot-public-<author>`.
`compare()` — отчёт совпадения «мой порт индикатора» vs «их публичные сигналы» (G08.1)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.contracts import Branch, Costs, Signal
from lab.core.measure import ClosedTrade, Measurement, run
from lab.db.models import SignalPublicRow
from lab.feeds.social.parser import parse_signal

PriceAt = Callable[[str, datetime], Decimal | None]


@dataclass(frozen=True)
class PublicSignal:
    id: int
    author: str
    channel: str
    message_ref: str
    published_at: datetime
    instrument: str | None
    side: str | None
    price_ref: Decimal | None
    parsed: dict[str, Any]
    outcome: dict[str, Any]


def _rec(row: SignalPublicRow) -> PublicSignal:
    return PublicSignal(
        row.id,
        row.author,
        row.channel,
        row.message_ref,
        row.published_at,
        row.instrument,
        row.side,
        row.price_ref,
        dict(row.parsed_json or {}),
        dict(row.outcome_json or {}),
    )


class PublicSignalsJournal:
    def __init__(self, session: Session) -> None:
        self.s = session

    # -- запись до исхода -----------------------------------------------------------------

    def record(
        self, *, author: str, channel: str, message_ref: str, published_at: datetime, text: str
    ) -> PublicSignal:
        existing = self.s.scalar(
            select(SignalPublicRow).where(
                SignalPublicRow.channel == channel, SignalPublicRow.message_ref == str(message_ref)
            )
        )
        if existing is not None:
            return _rec(existing)
        parsed = parse_signal(text)
        row = SignalPublicRow(
            author=author,
            channel=channel,
            message_ref=str(message_ref),
            published_at=published_at,
            instrument=parsed.instrument if parsed else None,
            side=parsed.side if parsed else None,
            price_ref=parsed.entry if parsed else None,
            raw_text=text,
            parsed_json=parsed.as_dict() if parsed else {},
            outcome_json={},
        )
        self.s.add(row)
        self.s.flush()
        return _rec(row)

    def get(self, signal_id: int) -> PublicSignal:
        return _rec(self.s.get_one(SignalPublicRow, signal_id))

    def list(self, author: str | None = None, *, parsed_only: bool = False) -> list[PublicSignal]:
        q = select(SignalPublicRow).order_by(SignalPublicRow.published_at)
        if author:
            q = q.where(SignalPublicRow.author == author)
        rows = [_rec(r) for r in self.s.scalars(q)]
        return [r for r in rows if r.instrument and r.side] if parsed_only else rows

    # -- оценка по факту ------------------------------------------------------------------

    def evaluate(
        self,
        signal_id: int,
        *,
        price_at: PriceAt,
        horizon: timedelta = timedelta(days=30),
        step: timedelta = timedelta(days=1),
        now: datetime | None = None,
    ) -> dict[str, Any]:
        row = self.s.get_one(SignalPublicRow, signal_id)
        now = now or datetime.now(UTC)
        if not (row.instrument and row.side):
            outcome = {"result": "unparsed"}
        else:
            outcome = _walk(row, price_at, horizon, step, now)
        row.outcome_json = outcome
        self.s.flush()
        return outcome

    def forward_measure(
        self,
        author: str,
        *,
        window: tuple[datetime, datetime],
        price_at: PriceAt,
        horizon: timedelta = timedelta(days=30),
        now: datetime | None = None,
        branch: Branch | str = Branch.CEX_SPOT,
        **run_kwargs,
    ) -> Measurement:
        trades: list[ClosedTrade] = []
        for sig in self.list(author, parsed_only=True):
            if not (window[0] <= sig.published_at <= window[1]):
                continue
            outcome = sig.outcome or self.evaluate(
                sig.id, price_at=price_at, horizon=horizon, now=now
            )
            if outcome.get("result") in ("target", "stop", "expired"):
                trades.append(_trade(sig, outcome))
        return run(
            f"{Branch(branch)}-public-{author}",
            "forward",
            window,
            trades=trades,
            branch=branch,
            **run_kwargs,
        )

    # -- G08.1: совпадение порта индикатора с публичными сигналами ------------------------------

    def compare(
        self, author: str, ours: Sequence[Signal], *, tolerance: timedelta = timedelta(days=7)
    ) -> dict[str, Any]:
        theirs = self.list(author, parsed_only=True)
        matched = 0
        for t in theirs:
            side = "buy" if t.side == "long" else "sell"
            if any(
                o.instrument == t.instrument
                and o.side == side
                and abs(o.decided_at - t.published_at) <= tolerance
                for o in ours
            ):
                matched += 1
        return {
            "author": author,
            "theirs": len(theirs),
            "ours": len(ours),
            "matched": matched,
            "match_rate": (matched / len(theirs)) if theirs else None,
        }


def _walk(
    row: SignalPublicRow, price_at: PriceAt, horizon: timedelta, step: timedelta, now: datetime
) -> dict[str, Any]:
    entry = row.price_ref or price_at(row.instrument, row.published_at)
    if entry is None:
        return {"result": "no_price"}
    parsed = row.parsed_json or {}
    target = Decimal(parsed["targets"][0]) if parsed.get("targets") else None
    stop = Decimal(parsed["stop"]) if parsed.get("stop") else None
    sign = 1 if row.side == "long" else -1
    end = min(now, row.published_at + horizon)
    ts = row.published_at + step
    last_price = entry
    while ts <= end:
        price = price_at(row.instrument, ts)
        if price is not None:
            last_price = price
            if target is not None and sign * (price - target) >= 0:
                return _closed("target", entry, price, ts, sign)
            if stop is not None and sign * (price - stop) <= 0:
                return _closed("stop", entry, price, ts, sign)
        ts += step
    if now >= row.published_at + horizon:
        return _closed("expired", entry, last_price, end, sign)
    return {
        "result": "open",
        "entry": str(entry),
        "last_price": str(last_price),
        "as_of": end.isoformat(),
    }


def _closed(
    result: str, entry: Decimal, exit_price: Decimal, at: datetime, sign: int
) -> dict[str, Any]:
    pnl_pct = (exit_price - entry) / entry * 100 * sign
    return {
        "result": result,
        "entry": str(entry),
        "exit_price": str(exit_price),
        "closed_at": at.isoformat(),
        "pnl_pct": str(pnl_pct),
    }


def _trade(sig: PublicSignal, outcome: dict[str, Any]) -> ClosedTrade:
    entry, exit_price = Decimal(outcome["entry"]), Decimal(outcome["exit_price"])
    sign = 1 if sig.side == "long" else -1
    return ClosedTrade(
        instrument=sig.instrument or "?",
        side=sig.side or "long",
        qty=Decimal(1),
        entry_price=entry,
        exit_price=exit_price,
        opened_at=sig.published_at,
        closed_at=datetime.fromisoformat(outcome["closed_at"]),
        pnl_gross=(exit_price - entry) * sign,
        costs=Costs(),
    )


__all__ = ["PriceAt", "PublicSignal", "PublicSignalsJournal"]
