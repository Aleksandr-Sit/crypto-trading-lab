"""Переизмерение лидера раз в неделю (История 63): цифры упали ниже порога — копирование снято.

Порог — те же числа, что и при отборе (`config/chains.yaml`, раздел `wallets`), плюс флаги
накрутки: кошелёк, у которого появился wash-trading или «свой токен», копировать нельзя.
Снятие идёт через `core.ladder.demote` — стратегия сама себе ступень не меняет (решение §7).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lab.feeds.chains.config import WalletThresholds, wallet_thresholds
from lab.ops.scheduler import Job
from lab.wallets import Flag, WalletStats, flags, recalc

JOB_ID = "leader_remeasure"
WEEKLY_CRON = "0 22 * * sun"  # рядом с еженедельным переизмерением стратегий (решение §11)
BLOCKING_FLAGS = ("wash_trading", "own_token")


@dataclass(frozen=True)
class LeaderVerdict:
    address: str
    chain: str
    passed: bool
    reason: str
    stats: WalletStats
    marks: Sequence[Flag] = field(default_factory=tuple)
    strategy_id: str = ""


class LeaderWatch:
    """Слежение за лидерами: пересчёт по их сделкам и снятие копирования при провале."""

    def __init__(
        self,
        *,
        source: Callable[[str, str], Sequence[Any]] | None = None,
        feeds: dict[str, Any] | None = None,
        ladder: Any | None = None,
        leaders: Sequence[tuple[str, str, str]] = (),
        thresholds: WalletThresholds | None = None,
        session: Any | None = None,
        notify: Callable[[LeaderVerdict], None] | None = None,
    ) -> None:
        self.source = source
        self.feeds = feeds or {}
        self.ladder = ladder
        self.leaders = list(leaders)
        self.thresholds = thresholds or wallet_thresholds()
        self.session = session
        self.notify = notify

    def remeasure(
        self,
        address: str,
        chain: str,
        *,
        strategy_id: str = "",
        now: datetime | None = None,
    ) -> LeaderVerdict:
        now = now or datetime.now(UTC)
        trades = list(self._trades(address, chain))
        stats = recalc(address, chain, trades=trades, now=now)
        marks = flags(address, chain, trades=trades, stats=stats, thresholds=self.thresholds)
        blocking = [f for f in marks if f.code in BLOCKING_FLAGS]
        reasons: list[str] = []
        if not stats.passes(self.thresholds):
            reasons.append(
                "порог не пройден: сделок "
                f"{stats.n_trades}/{self.thresholds.min_trades}, "
                f"win rate {stats.win_rate_pct}%/{self.thresholds.min_win_rate_pct}%, "
                f"просадка {stats.max_dd_pct}%/{self.thresholds.max_dd_pct}%"
            )
        reasons.extend(f"флаг {f.code}: {f.detail}" for f in blocking)
        verdict = LeaderVerdict(
            address=address,
            chain=chain,
            passed=not reasons,
            reason="; ".join(reasons) or "порог пройден",
            stats=stats,
            marks=tuple(marks),
            strategy_id=strategy_id,
        )
        if self.session is not None:
            from lab.wallets import save_stats

            save_stats(self.session, stats, marks)
        return verdict

    def run_all(self, now: datetime | None = None) -> list[LeaderVerdict]:
        out: list[LeaderVerdict] = []
        for address, chain, strategy_id in self.leaders:
            verdict = self.remeasure(address, chain, strategy_id=strategy_id, now=now)
            out.append(verdict)
            if not verdict.passed and self.ladder is not None and strategy_id:
                self.ladder.demote(strategy_id, f"лидер деградировал — {verdict.reason}")
            if self.notify is not None:
                self.notify(verdict)
        return out

    def job(self, cron: str = WEEKLY_CRON) -> Job:
        return Job(
            id=JOB_ID,
            func=self.run_all,
            cron=cron,
            description="Еженедельное переизмерение лидеров копитрейда",
        )

    def _trades(self, address: str, chain: str) -> Sequence[Any]:
        if self.source is not None:
            return self.source(address, chain)
        feed = self.feeds.get(chain)
        if feed is None:
            from lab.feeds.chains import make_chain_feed

            feed = make_chain_feed(chain)
            self.feeds[chain] = feed
        return feed.wallet_trades(address)


__all__ = ["BLOCKING_FLAGS", "JOB_ID", "WEEKLY_CRON", "LeaderVerdict", "LeaderWatch"]
