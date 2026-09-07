"""Фильтр потока ранних токенов (История 71, R17.5).

При 1000+ токенах в час оцениваются все — но полная история копится только по тем, что
прошли первичный фильтр; по остальным остаются агрегаты (сколько видели, сколько отсеяли
и по какой причине). Так поток не выедает базу и при этом остаётся измеримым.

Первичный фильтр — не чек-лист честности: он дешёвый и грубый (ликвидность, объём,
покупки, возраст), а честность (`honesty_check`) считается уже по кандидату перед покупкой.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from lab.feeds.dex.config import StreamConfig, load_meme
from lab.feeds.dex.types import TokenInfo


@dataclass(frozen=True)
class StreamVerdict:
    """Итог оценки одного события потока."""

    token: str
    chain: str
    passed: bool
    reason: str = ""
    detail: str = ""
    at: datetime = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class FlowAggregate:
    """Агрегаты по потоку — то, что остаётся от отсеянных токенов."""

    window_start: datetime
    window_end: datetime
    seen: int = 0
    passed: int = 0
    rejected: int = 0
    tokens: int = 0
    by_reason: Counter[str] = field(default_factory=Counter)
    by_chain: Counter[str] = field(default_factory=Counter)
    by_source: Counter[str] = field(default_factory=Counter)


class TokenStream:
    """Оценивает каждый токен, хранит историю прошедших, считает агрегаты по всем."""

    def __init__(
        self,
        *,
        config: StreamConfig | None = None,
        now: Callable[[], datetime] | None = None,
        max_tracked: int | None = None,
    ) -> None:
        self.config = config or load_meme().stream
        self.now = now or (lambda: datetime.now(UTC))
        self.max_tracked = max_tracked or self.config.max_tracked
        self.tracked: OrderedDict[str, list[TokenInfo]] = OrderedDict()
        self.verdicts: dict[str, StreamVerdict] = {}
        self._agg = FlowAggregate(window_start=self.now(), window_end=self.now())

    # -- оценка ------------------------------------------------------------------------

    def evaluate(self, token: TokenInfo) -> StreamVerdict:
        """Первичный фильтр. Дешёвый и без сети: его проходит каждое событие потока."""
        cfg, now = self.config, self.now()
        checks: list[tuple[str, bool, str]] = []
        liquidity = token.liquidity_usd
        checks.append(
            (
                "liquidity",
                liquidity is not None and liquidity >= cfg.min_liquidity_usd,
                f"ликвидность {liquidity} < {cfg.min_liquidity_usd}",
            )
        )
        volume = token.volume_usd
        checks.append(
            (
                "volume",
                cfg.min_volume_usd <= 0 or (volume is not None and volume >= cfg.min_volume_usd),
                f"объём {volume} < {cfg.min_volume_usd}",
            )
        )
        buys = token.buys
        checks.append(
            (
                "buys",
                cfg.min_buys <= 0 or (buys is not None and buys >= cfg.min_buys),
                f"покупок {buys} < {cfg.min_buys}",
            )
        )
        age = token.age_s(now)
        checks.append(
            (
                "age",
                age is None or age <= Decimal(cfg.max_age_min * 60),
                f"возраст {age} с — не ранняя стадия",
            )
        )
        for name, ok, detail in checks:
            if not ok:
                return StreamVerdict(token.address, token.chain, False, name, detail, now)
        return StreamVerdict(token.address, token.chain, True, "", "", now)

    def observe(self, token: TokenInfo) -> StreamVerdict:
        """Оценить событие: прошедшее уходит в историю, любое — в агрегаты."""
        verdict = self.evaluate(token)
        self._count(token, verdict)
        self.verdicts[token.key] = verdict
        if not verdict.passed:
            return verdict
        history = self.tracked.pop(token.key, [])
        history.append(token)
        if len(history) > self.config.max_history_per_token:
            del history[: len(history) - self.config.max_history_per_token]
        self.tracked[token.key] = history
        while len(self.tracked) > self.max_tracked:
            self.tracked.popitem(last=False)  # вытесняется самый давний кандидат
        return verdict

    def _count(self, token: TokenInfo, verdict: StreamVerdict) -> None:
        agg = self._agg
        agg.seen += 1
        agg.window_end = verdict.at
        agg.by_chain[token.chain] += 1
        if token.source:
            agg.by_source[token.source] += 1
        if verdict.passed:
            agg.passed += 1
        else:
            agg.rejected += 1
            agg.by_reason[verdict.reason] += 1

    # -- что осталось от потока ---------------------------------------------------------

    def history(self, key: str) -> list[TokenInfo]:
        return list(self.tracked.get(key, []))

    def latest(self, key: str) -> TokenInfo | None:
        history = self.tracked.get(key)
        return history[-1] if history else None

    def candidates(self) -> list[TokenInfo]:
        return [h[-1] for h in self.tracked.values() if h]

    def aggregates(self) -> FlowAggregate:
        self._agg.tokens = len(self.verdicts)
        return self._agg

    def reset_window(self) -> FlowAggregate:
        """Закрыть окно агрегатов и начать новое (агрегаты пишутся в базу пачкой)."""
        closed = self.aggregates()
        now = self.now()
        self._agg = FlowAggregate(window_start=now, window_end=now)
        return closed


__all__ = ["FlowAggregate", "StreamVerdict", "TokenStream"]
