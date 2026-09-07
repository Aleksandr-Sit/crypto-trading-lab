"""`external-signal`: сигналы crypto-trader / solana-smart-money → стратегия `<ветка>-ext-<name>`.

Транспорты дают `Event(kind="external-signal", payload=json-v1)`: `webhook_event` (тело + HMAC),
`file_events` (JSON Lines), `telegram_event` (пост канала через парсер `feeds.social`).
`ExternalSignalStrategy.on_event` превращает событие в `Signal` с `decided_at` = `ts` источника и
`inputs_hash` от payload; дубли по `source_id` отбрасываются. Дальше — обычный путь: журнал,
лестница (начальная ступень по `can_backtest=False`), риск — всё вне стратегии."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from lab.config import CONFIG_DIR, load_config
from lab.contracts import Branch, Event, Signal, StopSpec, StrategyManifest
from lab.feeds.social.parser import parse_signal
from lab.strategies.base import Strategy

EVENT_KIND = "external-signal"
PLACEHOLDER = "впиши"


class ExternalSource(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    transport: Literal["webhook", "file", "telegram"]
    branch: Branch
    venue: str
    secret_env: str | None = None
    path: str | None = None
    channel: str | None = None
    repo: str | None = None
    instruments: list[str] = Field(default_factory=lambda: ["*"])
    ttl_s: int = 900

    @property
    def configured(self) -> bool:
        target = {"file": self.path, "telegram": self.channel}.get(self.transport, "ok") or ""
        return PLACEHOLDER not in target


class ExternalSignalsConfig(BaseModel):
    version: int = 1
    format: str = "json-v1"
    example: dict[str, Any] = Field(default_factory=dict)
    webhook: dict[str, Any] = Field(default_factory=dict)
    sources: list[ExternalSource] = Field(default_factory=list)

    def source(self, name: str) -> ExternalSource:
        for s in self.sources:
            if s.name == name:
                return s
        raise KeyError(f"внешний источник {name!r} не описан в external_signals.yaml")


def load_external_signals(path: Path | str | None = None) -> ExternalSignalsConfig:
    return load_config(path or CONFIG_DIR / "external_signals.yaml", ExternalSignalsConfig)


# -- транспорты ---------------------------------------------------------------------------------


def _event(source: ExternalSource, payload: dict[str, Any], *, transport: str) -> Event | None:
    if (
        not isinstance(payload, dict)
        or not payload.get("instrument")
        or payload.get("side") not in ("buy", "sell")
    ):
        return None
    try:
        ts = datetime.fromisoformat(str(payload["ts"]))
    except (KeyError, ValueError):
        return None
    ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    return Event(
        kind=EVENT_KIND, ts=ts, payload={**payload, "source": source.name, "transport": transport}
    )


def webhook_event(
    source: ExternalSource, body: bytes, *, secret: str | None, signature: str | None = None
) -> Event | None:
    """Тело webhook → событие; при заданном secret подпись HMAC-SHA256 обязана совпасть."""
    if secret:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not (signature and hmac.compare_digest(expected, signature.lower())):
            return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    return _event(source, payload, transport="webhook")


def file_events(source: ExternalSource, path: Path | str | None = None) -> list[Event]:
    """JSON Lines: битые строки пропускаются, порядок сохраняется."""
    p = Path(path or source.path or "")
    if not p.is_file():
        return []
    out: list[Event] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        ev = _event(source, payload, transport="file")
        if ev is not None:
            out.append(ev)
    return out


def telegram_event(
    source: ExternalSource,
    text: str,
    *,
    published_at: datetime,
    message_ref: str,
    size: Decimal | str = "1",
) -> Event | None:
    parsed = parse_signal(text)
    if parsed is None:
        return None
    payload = {
        "instrument": parsed.instrument,
        "side": "buy" if parsed.side == "long" else "sell",
        "size": str(size),
        "price": None if parsed.entry is None else str(parsed.entry),
        "ts": published_at.isoformat(),
        "ttl_s": source.ttl_s,
        "source_id": f"tg:{message_ref}",
        "meta": {
            "targets": [str(t) for t in parsed.targets],
            "stop": None if parsed.stop is None else str(parsed.stop),
            "text": text,
        },
    }
    return _event(source, payload, transport="telegram")


# -- стратегия ------------------------------------------------------------------------------------


def external_manifest(source: ExternalSource) -> StrategyManifest:
    return StrategyManifest(
        slug=source.name,
        branch=source.branch,
        venue=source.venue,
        source_kind="ext",
        source_ref=source.repo or source.path or source.channel,
        instruments=source.instruments,
        timeframe=None,
        params={"ttl_s": source.ttl_s, "transport": source.transport},
        can_backtest=False,
        stop=StopSpec(daily_pct=Decimal(5), max_dd_pct=Decimal(15)),
        description=f"внешний источник {source.name} ({source.transport})",
    )


class ExternalSignalStrategy(Strategy):
    """Решения §9: внешние проекты — класс `ExternalSignalStrategy`; id `<ветка>-ext-<name>`."""

    def __init__(self, source: ExternalSource, manifest: StrategyManifest | None = None) -> None:
        self.source = source
        super().__init__(manifest or external_manifest(source))

    def reset(self) -> None:
        self.seen: set[str] = set()

    def on_event(self, event: Event) -> list[Signal]:
        p = event.payload
        if event.kind != EVENT_KIND or p.get("source") != self.source.name:
            return []
        source_id = str(p.get("source_id") or "")
        if source_id and source_id in self.seen:
            return []
        try:
            size = Decimal(str(p.get("size", "1")))
            price = None if p.get("price") in (None, "") else Decimal(str(p["price"]))
        except InvalidOperation:
            return []
        if size <= 0:
            return []
        if source_id:
            self.seen.add(source_id)
        sig = self.event_signal(
            event,
            str(p["instrument"]),
            str(p["side"]),
            size,
            price_ref=price,
            inputs={
                "source_id": source_id,
                "size": size,
                "price": price,
                "meta": p.get("meta") or {},
            },
            meta={
                "source": self.source.name,
                "transport": p.get("transport"),
                "source_id": source_id,
                **(p.get("meta") or {}),
            },
        )
        ttl = p.get("ttl_s")
        return [sig if ttl is None else sig.model_copy(update={"ttl_s": int(ttl)})]


__all__ = [
    "EVENT_KIND",
    "ExternalSignalStrategy",
    "ExternalSignalsConfig",
    "ExternalSource",
    "external_manifest",
    "file_events",
    "load_external_signals",
    "telegram_event",
    "webhook_event",
]
