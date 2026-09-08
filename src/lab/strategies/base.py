"""Базовый класс стратегии (Решения §9–§10).

Стратегия — правило: декларативный `StrategyManifest` (ветка, площадка, инструменты,
параметры, `can_backtest`, стоп, `ttl_s` в `params`) и методы `on_bar/on_event -> [Signal]`.
Каждый сигнал рождается с `decided_at` (закрытие бара, на котором принято решение — не раньше)
и `inputs_hash` (sha256 от входов решения и параметров) — форвард-журнал пишет его до исхода.

Стратегия ничего не знает о лестнице и риске: она не импортирует `core.ladder`, `core.risk`,
`core.registry`, `executors` (тест `tests/strategies/test_base.py` проверяет импорты).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from lab.contracts import Candle, Event, Signal, StrategyManifest, parse_tf

DEFAULT_TTL_S = 3600


def strategy_id_of(manifest: StrategyManifest) -> str:
    """`<ветка>-<источник>-<слаг>` — та же формула, что у `core.registry`."""
    return f"{manifest.branch}-{manifest.source_kind}-{manifest.slug}"


def _canon(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canon(v) for v in value]
    return value


def inputs_hash(inputs: dict[str, Any], params: dict[str, Any] | None = None) -> str:
    """sha256 канонического JSON входов и параметров — одинаковые входы дают одинаковый хеш."""
    payload = {"inputs": _canon(inputs), "params": _canon(params or {})}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Strategy:
    """База для стратегий с правилами. Подкласс переопределяет `on_bar` и/или `on_event`."""

    manifest: StrategyManifest
    card: str | None = None  # id карточки docs/research/strategies/<id>.md, если есть

    def __init__(self, manifest: StrategyManifest) -> None:
        self.manifest = manifest
        self.step: timedelta = parse_tf(manifest.timeframe) if manifest.timeframe else timedelta(0)
        self.reset()

    # -- контракт --------------------------------------------------------------------

    @property
    def strategy_id(self) -> str:
        return strategy_id_of(self.manifest)

    @property
    def params(self) -> dict[str, Any]:
        return self.manifest.params

    def reset(self) -> None:
        """Сброс внутреннего состояния (walk-forward создаёт свежие экземпляры,
        но и это пригодится)."""

    def on_bar(self, bar: Candle) -> list[Signal]:
        return []

    def on_event(self, event: Event) -> list[Signal]:
        return []

    # -- оформление сигнала -------------------------------------------------------------

    def param(self, name: str, default: Any = None) -> Any:
        return self.manifest.params.get(name, default)

    def ttl_s(self) -> int:
        return int(self.param("ttl_s", DEFAULT_TTL_S))

    def decided_at(self, bar: Candle) -> datetime:
        """Момент решения — закрытие бара: раньше него данные бара не известны."""
        return bar.ts + (self.step or parse_tf(bar.tf))

    def signal(
        self,
        bar: Candle,
        side: str,
        size: Decimal,
        *,
        inputs: dict[str, Any] | None = None,
        price_ref: Decimal | None = None,
        meta: dict[str, Any] | None = None,
        decided_at: datetime | None = None,
    ) -> Signal:
        base_inputs = {"ts": bar.ts, "instrument": bar.instrument, "side": side}
        if inputs:
            base_inputs.update(inputs)
        # `inputs` уходит В ХЕШ, а хеш обратно не прочитаешь: почему стратегия закрылась —
        # по стопу, по каналу или по базису — после этого узнать негде. Поэтому «что это
        # было» и «почему» дублируются в meta, откуда их видит замер и журнал.
        meta_out = dict(meta or {})
        for key in ("kind", "reason"):
            value = (inputs or {}).get(key)
            if value is not None and key not in meta_out:
                meta_out[key] = value
        return Signal(
            strategy_id=self.strategy_id,
            decided_at=decided_at or self.decided_at(bar),
            instrument=bar.instrument,
            side=side,
            size=size,
            price_ref=bar.close if price_ref is None else price_ref,
            inputs_hash=inputs_hash(base_inputs, self.manifest.params),
            ttl_s=self.ttl_s(),
            meta=meta_out,
        )

    def event_signal(
        self,
        event: Event,
        instrument: str,
        side: str,
        size: Decimal,
        *,
        price_ref: Decimal | None = None,
        inputs: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Signal:
        base_inputs = {"event": event.kind, "ts": event.ts, "instrument": instrument, "side": side}
        if inputs:
            base_inputs.update(inputs)
        return Signal(
            strategy_id=self.strategy_id,
            decided_at=event.ts,
            instrument=instrument,
            side=side,
            size=size,
            price_ref=price_ref,
            inputs_hash=inputs_hash(base_inputs, self.manifest.params),
            ttl_s=self.ttl_s(),
            meta=dict(meta or {}),
        )


class BarHistory:
    """Скользящее окно свечей одного инструмента — общая мелочь для правил на барах."""

    def __init__(self, maxlen: int) -> None:
        self.maxlen = maxlen
        self.bars: list[Candle] = []

    def push(self, bar: Candle) -> None:
        self.bars.append(bar)
        if len(self.bars) > self.maxlen:
            del self.bars[: len(self.bars) - self.maxlen]

    def __len__(self) -> int:
        return len(self.bars)

    def closes(self) -> list[Decimal]:
        return [b.close for b in self.bars]


__all__ = ["DEFAULT_TTL_S", "BarHistory", "Strategy", "inputs_hash", "strategy_id_of"]
