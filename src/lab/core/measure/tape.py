"""Streaming, sequence-checked spot trade replay. A print is not an order book."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from lab.contracts import Branch, Costs, Event, Signal
from lab.core.costs import CostModel, is_spot
from lab.core.measure.simulator import PaperEngine, SimResult, _PeakStop, _StopTracker
from lab.core.measure.types import IncompleteData

METHOD = "sequenced-spot-aggressor-prints/v1"
ZERO = Decimal(0)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TapeHeader(_Model):
    format: Literal["lab-trades/v1"]
    venue: str
    instrument: str
    covered_from: AwareDatetime
    covered_to: AwareDatetime
    first_sequence: int = Field(ge=0, strict=True)
    last_sequence: int = Field(ge=0, strict=True)


class TapePrint(_Model):
    sequence: int = Field(ge=0, strict=True)
    ts: AwareDatetime
    price: Decimal = Field(gt=0)
    qty: Decimal = Field(gt=0)
    side: Literal["buy", "sell"]  # aggressor side, NOT maker side


class TapeConfig(_Model):
    latency_ms: int = Field(gt=0, strict=True, default=250)
    max_participation: Decimal = Field(gt=0, le=1, default=Decimal("0.1"))


@dataclass
class _Order:
    signal: Signal
    remaining: Decimal


def read_header(path: str | Path) -> TapeHeader:
    with Path(path).open(encoding="utf-8-sig") as stream:
        return TapeHeader.model_validate_json(stream.readline())


def replay_tape(
    strategy,
    path,
    window: tuple[datetime, datetime],
    *,
    costs: CostModel,
    capital: Decimal,
    config: TapeConfig,
    digest,
) -> tuple[SimResult, dict]:
    manifest = strategy.manifest
    if (
        manifest.branch != Branch.CEX_SPOT
        or len(manifest.instruments) != 1
        or not is_spot(manifest.instruments[0])
        or manifest.venue not in costs.config.cex
        or manifest.instruments[0].split("/")[-1] not in {"USD", "USDT", "USDC"}
    ):
        raise IncompleteData("лента: поддерживается только один инструмент долларового CEX-спота")
    if not callable(getattr(strategy, "on_event", None)):
        raise IncompleteData("лента: стратегия должна обрабатывать on_event(kind=trade)")
    if capital <= 0:
        raise IncompleteData("лента: капитал должен быть положительным")
    engine = PaperEngine(
        venue=manifest.venue,
        instrument=manifest.instruments[0],
        tf="1m",
        costs=costs,
        keep_fills=False,
    )
    fee_rate = costs.config.cex[manifest.venue].taker_bps / 10_000
    cash = capital
    pending: list[_Order] = []
    latency = timedelta(milliseconds=config.latency_ms)
    tracker = _StopTracker(manifest.stop, capital, closed_dd=False)
    peak_stop = _PeakStop(manifest.stop, capital)
    peak, max_dd, unrealized = capital, ZERO, ZERO
    stopped_at, stop_rule, blocked, expired = None, "", 0, 0
    fills_count, prints, previous, last_ts = 0, 0, None, None
    reasons: dict[str, int] = {}
    with Path(path).open(encoding="utf-8-sig") as stream:
        raw = stream.readline()
        digest.update(raw.encode())
        header = TapeHeader.model_validate_json(raw)
        if (
            header.venue != manifest.venue
            or header.instrument != engine.instrument
            or (header.covered_from, header.covered_to) != window
            or header.covered_to <= header.covered_from
            or header.last_sequence < header.first_sequence
        ):
            raise IncompleteData(
                "лента: площадка, инструмент, окно или границы номеров не совпадают"
            )
        for raw in stream:
            digest.update(raw.encode())
            tick = TapePrint.model_validate_json(raw)
            expected = header.first_sequence if previous is None else previous + 1
            if tick.sequence != expected or tick.sequence > header.last_sequence:
                raise IncompleteData("лента: пропуск, повтор или нарушение порядка номеров")
            if not window[0] <= tick.ts < window[1] or (last_ts is not None and tick.ts < last_ts):
                raise IncompleteData("лента: время вне окна или нарушен порядок времени")
            previous, last_ts = tick.sequence, tick.ts
            prints += 1
            budget = tick.qty * config.max_participation
            before = len(engine.closed)
            still = []
            for order in pending:
                signal = order.signal
                if tick.ts >= signal.decided_at + timedelta(seconds=signal.ttl_s):
                    expired += 1
                    continue
                if (
                    signal.decided_at + latency <= tick.ts
                    and signal.side == tick.side
                    and budget > 0
                ):
                    available = (
                        cash / (tick.price * (1 + fee_rate))
                        if signal.side == "buy"
                        else engine.position
                    )
                    qty = min(order.remaining, budget, max(ZERO, available))
                    if qty > 0:
                        fee = qty * tick.price * fee_rate
                        cash += (
                            -qty * tick.price - fee
                            if signal.side == "buy"
                            else qty * tick.price - fee
                        )
                        # Actual print price already includes its side of the spread;
                        # add fees only, never another modelled half-spread.
                        engine._apply(signal.side, qty, tick.price, tick.ts, Costs(fee=fee))
                        budget -= qty
                        order.remaining -= qty
                        fills_count += 1
                        reason = str(signal.meta.get("reason") or "без причины")
                        reasons[reason] = reasons.get(reason, 0) + 1
                if order.remaining > 0:
                    still.append(order)
            pending = still
            unrealized = sum(
                ((tick.price - lot.ref_price) * lot.qty - lot.costs.total for lot in engine.lots),
                ZERO,
            )
            equity = cash + engine.position * tick.price
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) * 100 / peak)
            if stopped_at is None:
                stop_rule = tracker.breach(engine.closed[before:], tick.ts) or peak_stop.breach(
                    equity
                )
                if stop_rule:
                    stopped_at = tick.ts
                    blocked += sum(o.signal.side == "buy" for o in pending)
                    pending = [o for o in pending if o.signal.side != "buy"]
            # Decisions see this print AFTER fills. Positive latency prevents using
            # another print with the same timestamp as a free immediate execution.
            for signal in strategy.on_event(
                Event(
                    kind="trade",
                    ts=tick.ts,
                    payload={
                        **tick.model_dump(),
                        "venue": header.venue,
                        "instrument": header.instrument,
                    },
                )
            ):
                if (
                    signal.decided_at != tick.ts
                    or signal.instrument != header.instrument
                    or signal.side not in {"buy", "sell"}
                    or signal.size <= 0
                ):
                    raise IncompleteData(
                        "лента: сигнал имеет неверные время, инструмент, сторону или размер"
                    )
                if signal.meta.get("order_type", "market") != "market":
                    raise IncompleteData("лента: лимитки требуют отдельной модели очереди")
                if stopped_at is not None and signal.side == "buy":
                    blocked += 1
                else:
                    pending.append(_Order(signal, signal.size))
        if previous != header.last_sequence:
            raise IncompleteData("лента: пустой или оборванный хвост")
    expired += sum(
        o.signal.decided_at + timedelta(seconds=o.signal.ttl_s) <= window[1] for o in pending
    )
    pending = [
        o for o in pending if o.signal.decided_at + timedelta(seconds=o.signal.ttl_s) > window[1]
    ]
    return SimResult(
        trades=engine.closed,
        fills=[],
        open_position=engine.position,
        expired_signals=expired,
        stopped_at=stopped_at,
        stop_rule=stop_rule,
        blocked_signals=blocked,
        positions={engine.instrument: engine.position},
        reasons=reasons,
        mtm_max_dd_pct=max_dd,
        unrealized_end=unrealized,
    ), {
        "method": METHOD,
        "prints": prints,
        "fills": fills_count,
        "expired": expired,
        "pending": len(pending),
        "open_position": str(engine.position),
        "latency_ms": config.latency_ms,
        "max_participation": str(config.max_participation),
        "coverage": "declared_header_and_contiguous_sequences; not_independently_verified",
        "mark": "last_trade; not_executable_bid",
        "last_print_at": last_ts.isoformat(),
        "mark_age_seconds": str((window[1] - last_ts).total_seconds()),
        "header": header.model_dump(mode="json"),
    }


def tape_measure_inputs(strategy, path, window, *, costs, capital, benchmark):
    """Hash consumed data, including incomplete prefixes; keep no print list in memory."""
    digest = hashlib.sha256()
    manifest = strategy.manifest
    try:
        config = TapeConfig(
            latency_ms=manifest.params.get("execution_latency_ms", 250),
            max_participation=manifest.params.get("max_participation", Decimal("0.1")),
        )
        result, evidence = replay_tape(
            strategy, path, window, costs=costs, capital=capital, config=config, digest=digest
        )
        digest.update(json.dumps(benchmark, default=str, sort_keys=True).encode())
        return result, evidence, digest.hexdigest(), ""
    except (IncompleteData, ValidationError, OSError, ValueError) as err:
        return None, {}, digest.hexdigest(), f"лента: {err}"
