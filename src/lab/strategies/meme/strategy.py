"""Стратегии `meme-*` — ранние стадии, только форвард (Истории 65–69, R24).

`can_backtest=False`: бэктеста у ранней стадии не бывает — выжившие токены попадают в
любую историческую выборку, а мёртвые из неё исчезают. Поэтому каждое решение пишется
в журнал моментом события (`decided_at`, `inputs_hash`) и до того, как известен исход.

Покупка невозможна без пройденного чек-листа честности: `honesty_check` вызывается на
каждом событии, результат остаётся в `last_checklist(token)` — и провал виден в журнале
так же, как вход.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from lab.contracts import Branch, Event, Signal, StopSpec, StrategyManifest
from lab.feeds.dex import (
    MIGRATION_EVENT,
    NEW_PAIR_EVENT,
    NEW_TOKEN_EVENT,
    Checklist,
    TokenInfo,
    honesty_check,
    load_meme,
)
from lab.strategies.base import Strategy, inputs_hash
from lab.strategies.meme.ladder import (
    LadderStep,
    MemePosition,
    SellLadder,
    steps_from_params,
)

DEFAULT_TTL_S = 60
ENTRY_EVENTS = (MIGRATION_EVENT, NEW_TOKEN_EVENT, NEW_PAIR_EVENT)


def _num(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _dec(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value is not None else default))


class MemeStrategy(Strategy):
    """Общее для ветки: честность до покупки, размер от капитала, лестница продаж."""

    entry_reason = "meme_entry"

    def __init__(self, manifest: StrategyManifest) -> None:
        config = load_meme()
        self.honesty_config = config.honesty
        self._default_steps = [
            LadderStep(t.gain_pct, t.sell_pct) for t in config.ladder.targets
        ]
        self._config_ladder = config.ladder
        super().__init__(manifest)
        self.ladder = SellLadder(
            steps_from_params(self.param("ladder_targets"), self._default_steps),
            trailing_pct=_dec(self.param("trailing_pct"), str(self._config_ladder.trailing_pct)),
            stop_loss_pct=_dec(self.param("stop_loss_pct"), str(self._config_ladder.stop_loss_pct)),
        )

    def reset(self) -> None:
        self.positions: dict[str, MemePosition] = {}
        self.checklists: dict[str, Checklist] = {}

    # -- вход ---------------------------------------------------------------------------

    def last_checklist(self, token: str) -> Checklist | None:
        return self.checklists.get(token)

    def token_of(self, event: Event) -> TokenInfo | None:
        raw = event.payload.get("token")
        if not isinstance(raw, dict):
            return None
        try:
            return TokenInfo(**raw)
        except (TypeError, ValueError):
            return None

    def wants(self, token: TokenInfo, event: Event) -> bool:
        """Правило конкретной стратегии (переопределяется)."""
        return True

    def on_event(self, event: Event) -> list[Signal]:
        if event.kind not in ENTRY_EVENTS:
            return []
        token = self.token_of(event)
        if token is None or token.address in self.positions:
            return []
        if not self.wants(token, event):
            return []
        checklist = honesty_check(
            token,
            config=self.honesty_config,
            blocklist=list(self.param("blocklist", []) or []),
            now=event.ts,
        )
        self.checklists[token.address] = checklist
        if not checklist.passed:  # провал чек-листа запрещает покупку (История 67)
            return []
        price = token.price_usd or Decimal(0)
        size_usd = self.size_usd()
        if price <= 0 or size_usd <= 0:
            return []
        qty = size_usd / price
        return [
            self.event_signal(
                event,
                token.address,
                "buy",
                qty,
                price_ref=price,
                inputs={"liquidity": str(token.liquidity_usd), "volume": str(token.volume_usd)},
                meta={
                    "reason": self.entry_reason,
                    "chain": token.chain,
                    "venue": token.venue or self.manifest.venue,
                    "symbol": token.symbol,
                    "size_usd": _num(size_usd),
                    "price": _num(price),
                    "honesty": "passed",
                    "honesty_checks": [c.name for c in checklist.checks],
                    "source": token.source,
                },
            )
        ]

    def size_usd(self) -> Decimal:
        capital = _dec(self.param("capital_usd"), "1000")
        share = _dec(self.param("position_pct"), "10")
        size = capital * share / 100
        cap = _dec(self.param("max_trade_usd"), "0")
        if cap > 0:
            size = min(size, cap)
        return size

    def ttl_s(self) -> int:
        return int(self.param("ttl_s", DEFAULT_TTL_S))

    # -- выход по лестнице ----------------------------------------------------------------

    def note_fill(
        self, token: str, side: str, qty: Decimal, price: Decimal, ts: datetime | None = None
    ) -> None:
        """Факт исполнения от worker: без него лестница не знает, чем распоряжаться."""
        position = self.positions.setdefault(token, MemePosition(token=token))
        if side == "buy":
            position.add(qty, price, ts)
        else:
            position.reduce(qty)

    def on_price(self, token: str, price: Decimal, ts: datetime) -> list[Signal]:
        """Тик цены по удерживаемому токену — шаг лестницы продаж, если он настал."""
        position = self.positions.get(token)
        if position is None:
            return []
        sell = self.ladder.next_sell(position, price)
        if sell is None:
            return []
        meta = {
            "reason": sell.reason,
            "entry_price": _num(position.entry_price),
            "gain_pct": _num(sell.gain_pct),
            "peak_price": _num(sell.peak_price),
            "position_qty": _num(position.initial_qty),
            "price": _num(price),
        }
        return [
            Signal(
                strategy_id=self.strategy_id,
                decided_at=ts,
                instrument=token,
                side="sell",
                size=sell.qty,
                price_ref=price,
                inputs_hash=inputs_hash(
                    {"token": token, "price": price, "ts": ts, "reason": sell.reason},
                    self.manifest.params,
                ),
                ttl_s=self.ttl_s(),
                meta=meta,
            )
        ]


class MemeEarlyStrategy(MemeStrategy):
    """Ранний вход сразу после миграции с бондинг-кривой на DEX (История 65 + 66)."""

    entry_reason = "migration_entry"

    def wants(self, token: TokenInfo, event: Event) -> bool:
        # Только состоявшаяся миграция: «create» на кривой ещё не пул, и покупать там нечего.
        return bool(token.migrated)


class MemeHonestVolumeStrategy(MemeStrategy):
    """Вход по чек-листу честности плюс порог объёма и числа покупок (Истории 67, 71)."""

    entry_reason = "honest_volume_entry"

    def wants(self, token: TokenInfo, event: Event) -> bool:
        volume = token.volume_usd or Decimal(0)
        buys = token.buys or 0
        return volume >= _dec(self.param("min_volume_usd"), "0") and buys >= int(
            self.param("min_buys", 0)
        )


def _stop() -> StopSpec:
    return StopSpec(daily_pct=Decimal(10), max_dd_pct=Decimal(25), max_position_pct=Decimal(5))


def meme_manifest(
    *,
    slug: str,
    source_kind: str,
    venue: str,
    instruments: list[str] | None = None,
    description: str = "",
    **params: Any,
) -> StrategyManifest:
    execution = load_meme().execution
    defaults: dict[str, Any] = {
        "capital_usd": "1000",
        "position_pct": "10",
        "max_trade_usd": "100",
        "ttl_s": DEFAULT_TTL_S,
        "max_slippage_pct": str(execution.max_slippage_pct),
        "slippage_pct": str(execution.slippage_pct),
        "priority_fee_usd": str(execution.priority_fee_usd),
        "max_priority_fee_usd": str(execution.max_priority_fee_usd),
        "retry_priority_multiplier": str(execution.retry_priority_multiplier),
        "max_attempts": execution.max_attempts,
        "mode": "paper",
    }
    defaults.update({k: v for k, v in params.items() if v is not None})
    return StrategyManifest(
        slug=slug,
        branch=Branch.MEME if hasattr(Branch, "MEME") else "meme",
        venue=venue,
        source_kind=source_kind,
        instruments=instruments or ["*"],
        params=defaults,
        can_backtest=False,  # ранние стадии меряются только форвардом (История 69)
        stop=_stop(),
        description=description,
    )


def make_meme_early_strategy(
    *, venue: str = "jupiter", slug: str = "early-v1", **params: Any
) -> MemeEarlyStrategy:
    return MemeEarlyStrategy(
        meme_manifest(
            slug=slug,
            source_kind="sol-pumpfun",
            venue=venue,
            description="ранний вход после миграции pump.fun → DEX, выход лестницей",
            **params,
        )
    )


def make_meme_volume_strategy(
    *, venue: str = "jupiter", slug: str = "honest-volume-v1", **params: Any
) -> MemeHonestVolumeStrategy:
    params.setdefault("min_volume_usd", "10000")
    params.setdefault("min_buys", 50)
    return MemeHonestVolumeStrategy(
        meme_manifest(
            slug=slug,
            source_kind="dex",
            venue=venue,
            description="вход по чек-листу честности и объёму, выход лестницей",
            **params,
        )
    )


def meme_strategies() -> list[MemeStrategy]:
    """Обе стратегии ветки — то, что worker ставит на форвард."""
    return [make_meme_early_strategy(), make_meme_volume_strategy()]


__all__ = [
    "DEFAULT_TTL_S",
    "ENTRY_EVENTS",
    "MemeEarlyStrategy",
    "MemeHonestVolumeStrategy",
    "MemeStrategy",
    "make_meme_early_strategy",
    "make_meme_volume_strategy",
    "meme_manifest",
    "meme_strategies",
]
