"""Шов core.risk.check (спецификация, «Швы для тестов»). Портфель и реестр — фейки.

Числа — из таблицы лимитов веток (В9а): банк 10 000 USD → cex 40 % = 4 000,
сделка ≤ 2 % банка = 200, плечо ≤ 3; meme 20 % = 2 000, сделка ≤ 10 % ветки, без плеча,
стоп ветки −20 %/день; nft стоп −30 %/неделя. Потолок реального капитала — 1 000 USD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from lab.contracts import Branch, OrderIntent, Rung, Status, StopSpec
from lab.core.risk import (
    Allow,
    BranchState,
    Deny,
    MemoryHaltSwitch,
    RiskEngine,
    StrategyInfo,
    StrategyStats,
)

D = Decimal
ROOT = Path(__file__).resolve().parents[2]


@dataclass
class FakePortfolio:
    bank: Decimal = D(10_000)
    branches: dict[str, BranchState] = field(default_factory=dict)
    venues_down: set[str] = field(default_factory=set)
    stats: dict[str, StrategyStats] = field(default_factory=dict)
    live_deployed: Decimal = D(0)
    allocation_exposure: Decimal = D(0)
    marks: dict[str, Decimal] = field(default_factory=lambda: {"BTC-USDT": D(100)})
    liq: dict[str, Decimal] = field(default_factory=dict)

    def bank_usd(self) -> Decimal:
        return self.bank

    def branch(self, branch: Branch | str) -> BranchState:
        return self.branches.get(str(branch), BranchState(current_usd=D(0)))

    def venue_available(self, venue: str) -> bool:
        return venue not in self.venues_down

    def strategy_stats(self, strategy_id: str) -> StrategyStats:
        return self.stats.get(strategy_id, StrategyStats())

    def allocation_exposure_usd(self) -> Decimal:
        return self.allocation_exposure

    def live_deployed_usd(self) -> Decimal:
        return self.live_deployed

    def mark_price(self, venue: str, instrument: str) -> Decimal | None:
        return self.marks.get(instrument)

    def liquidation_price(self, intent: OrderIntent) -> Decimal | None:
        return self.liq.get(intent.instrument)


STRATEGIES: dict[str, StrategyInfo] = {
    "cex-perp-hl-trend": StrategyInfo(
        id="cex-perp-hl-trend",
        branch=Branch.CEX_PERP,
        venue="hyperliquid",
        rung=Rung.MICRO,
        status=Status.PASSED,
        stop=StopSpec(daily_pct=D(3), max_dd_pct=D(10), max_position_pct=D(10)),
    ),
    "meme-sol-early": StrategyInfo(
        id="meme-sol-early",
        branch=Branch.MEME,
        venue="jupiter",
        rung=Rung.MICRO,
        status=Status.PASSED,
        stop=StopSpec(daily_pct=D(5)),
    ),
    "cex-spot-rotation": StrategyInfo(
        id="cex-spot-rotation",
        branch=Branch.CEX_SPOT,
        venue="binance",
        rung=Rung.MICRO,
        status=Status.PASSED,
        stop=StopSpec(max_dd_pct=D(35)),
        allocation=True,
    ),
    "nft-me-mint": StrategyInfo(
        id="nft-me-mint",
        branch=Branch.NFT,
        venue="magiceden",
        rung=Rung.PAPER,
        status=Status.MEASURING,
        stop=StopSpec(daily_pct=D(5)),
    ),
}


def intent(
    strategy_id: str = "cex-perp-hl-trend",
    *,
    qty: str = "1",
    price: str | None = "100",
    leverage: str = "1",
    mode: str = "paper",
    reduce_only: bool = False,
    venue: str | None = None,
) -> OrderIntent:
    s = STRATEGIES[strategy_id]
    return OrderIntent(
        strategy_id=strategy_id,
        venue=venue or s.venue,
        instrument="BTC-USDT",
        side="buy",
        qty=D(qty),
        price=None if price is None else D(price),
        order_type="market" if price is None else "limit",
        leverage=D(leverage),
        reduce_only=reduce_only,
        mode=mode,  # type: ignore[arg-type]
        signal_id="sig-1",
        client_order_id="coid-1",
    )


@pytest.fixture
def portfolio() -> FakePortfolio:
    return FakePortfolio(
        branches={
            "cex-perp": BranchState(current_usd=D(4_000)),
            "meme": BranchState(current_usd=D(2_000)),
            "nft": BranchState(current_usd=D(1_000)),
        }
    )


@pytest.fixture
def engine(portfolio: FakePortfolio) -> RiskEngine:
    return RiskEngine(STRATEGIES.get, portfolio, halt=MemoryHaltSwitch())


def deny(verdict: Allow | Deny, rule: str) -> Deny:
    assert isinstance(verdict, Deny), verdict
    assert verdict.rule == rule
    assert verdict.reason and verdict.reason[0].isupper() or verdict.reason[0].isalpha()
    return verdict


# --- доля ветки и размер --------------------------------------------------------------


def test_branch_share_is_capped_at_40_percent_of_bank(engine, portfolio):
    portfolio.branches["cex-perp"] = BranchState(current_usd=D(4_000), exposure_usd=D(3_900))
    # 3 900 + 200 = 4 100 > 4 000 (40 % от 10 000)
    deny(engine.check(intent(qty="2")), "branch_share")

    portfolio.branches["cex-perp"] = BranchState(current_usd=D(4_000), exposure_usd=D(3_700))
    assert engine.check(intent(qty="2")) == Allow()


def test_max_trade_is_2_percent_of_bank_for_cex(engine):
    deny(engine.check(intent(qty="2.5")), "max_trade")  # 250 > 200
    assert engine.check(intent(qty="2")) == Allow()


def test_deny_carries_russian_reason_and_rule_code(engine):
    verdict = engine.check(intent(qty="2.5"))
    assert isinstance(verdict, Deny)
    assert verdict.rule == "max_trade"
    assert "250" in verdict.reason and "200" in verdict.reason
    assert any("а" <= ch <= "я" for ch in verdict.reason.lower())


def test_size_is_from_current_branch_capital_not_base(engine, portfolio):
    # meme: 10 % от текущего капитала ветки. База 2 000 → 200, но ветка просела до 1 500 → 150.
    portfolio.branches["meme"] = BranchState(current_usd=D(1_500))
    deny(engine.check(intent("meme-sol-early", qty="1.8")), "max_trade")  # 180 > 150
    assert engine.check(intent("meme-sol-early", qty="1.5")) == Allow()


def test_strategy_max_position_is_stricter_than_branch_limit(engine, portfolio):
    # У стратегии max_position_pct=10 → 10 % от 4 000 = 400; лимит ветки 200 строже — действует 200
    portfolio.branches["cex-perp"] = BranchState(current_usd=D(1_000))  # 10 % → 100 строже 200
    deny(engine.check(intent(qty="1.5")), "max_trade")  # 150 > 100
    assert engine.check(intent(qty="1")) == Allow()


# --- плечо и ликвидация ---------------------------------------------------------------


def test_leverage_above_branch_limit_is_denied(engine):
    deny(engine.check(intent(leverage="4")), "leverage")
    deny(engine.check(intent("meme-sol-early", leverage="2")), "leverage")  # meme — без плеча
    assert engine.check(intent(leverage="3")) == Allow()


def test_liquidation_closer_than_strategy_stop_is_denied(engine, portfolio):
    # 3× → до ликвидации 100/3 − 0.5 = 32.8 %; стоп стратегии на позицию 10 % → допустимо
    assert engine.check(intent(leverage="3")) == Allow()
    # площадка сообщает цену ликвидации 92 при входе 100 → 8 % < стопа 10 %
    portfolio.liq["BTC-USDT"] = D(92)
    deny(engine.check(intent(leverage="3")), "liquidation")
    # без плеча ликвидации нет — правило не применяется
    assert engine.check(intent(leverage="1")) == Allow()


# --- стопы ----------------------------------------------------------------------------


def test_strategy_daily_stop_and_drawdown_stop(engine, portfolio):
    portfolio.stats["cex-perp-hl-trend"] = StrategyStats(pnl_day_pct=D("-3.2"))
    deny(engine.check(intent()), "strategy_stop_daily")
    portfolio.stats["cex-perp-hl-trend"] = StrategyStats(pnl_day_pct=D("-2.9"), dd_pct=D("10"))
    deny(engine.check(intent()), "strategy_stop_dd")
    portfolio.stats["cex-perp-hl-trend"] = StrategyStats(pnl_day_pct=D("-2.9"), dd_pct=D("9.9"))
    assert engine.check(intent()) == Allow()


def test_branch_stop_day_for_cex_and_week_for_nft(engine, portfolio):
    portfolio.branches["cex-perp"] = BranchState(current_usd=D(3_800), pnl_day_pct=D("-5"))
    deny(engine.check(intent()), "branch_stop")
    portfolio.branches["nft"] = BranchState(
        current_usd=D(700), pnl_day_pct=D("-2"), pnl_week_pct=D("-30")
    )
    deny(engine.check(intent("nft-me-mint", qty="1")), "branch_stop")
    portfolio.branches["nft"] = BranchState(current_usd=D(800), pnl_week_pct=D("-20"))
    assert engine.check(intent("nft-me-mint", qty="1")) == Allow()


def test_reduce_only_passes_size_rules_but_not_halt_or_venue(engine, portfolio):
    portfolio.branches["cex-perp"] = BranchState(current_usd=D(4_000), exposure_usd=D(4_000))
    assert engine.check(intent(qty="5", reduce_only=True)) == Allow()
    portfolio.venues_down.add("hyperliquid")
    deny(engine.check(intent(qty="5", reduce_only=True)), "venue_unavailable")


# --- потолок реального капитала, площадка, halt, стоп в манифесте ------------------------


def test_real_capital_cap_applies_to_live_only(engine, portfolio, monkeypatch):
    monkeypatch.delenv("REAL_CAPITAL_CAP", raising=False)
    portfolio.live_deployed = D(950)
    deny(engine.check(intent(qty="1", mode="live")), "real_capital_cap")  # 950 + 100 > 1 000
    assert engine.check(intent(qty="1", mode="paper")) == Allow()
    monkeypatch.setenv("REAL_CAPITAL_CAP", "1200")
    assert engine.check(intent(qty="1", mode="live")) == Allow()


def test_live_orders_need_micro_or_higher(engine):
    deny(engine.check(intent("nft-me-mint", mode="live")), "rung_mode")  # paper-ступень


def test_unavailable_venue_denies_but_keeps_last_known_capital(engine, portfolio):
    portfolio.venues_down.add("hyperliquid")
    portfolio.branches["cex-perp"] = BranchState(
        current_usd=D(3_500), exposure_usd=D(500), stale=True
    )
    deny(engine.check(intent()), "venue_unavailable")
    alloc = engine.allocation(Branch.CEX_PERP)
    assert alloc.current_usd == D(3_500) and alloc.stale is True
    assert alloc.base_usd == D(4_000) and alloc.available_usd == D(3_500)
    assert alloc.max_trade_usd == D(200)


def test_halt_denies_everything_until_resume(engine):
    engine.halt_switch.halt(by="operator")
    deny(engine.check(intent()), "halted")
    deny(engine.check(intent("meme-sol-early", qty="0.1", reduce_only=True)), "halted")
    engine.halt_switch.resume(by="operator")
    assert engine.check(intent()) == Allow()


def test_strategy_without_stop_on_micro_is_denied(portfolio):
    strategies = dict(STRATEGIES)
    strategies["cex-perp-hl-trend"] = STRATEGIES["cex-perp-hl-trend"].model_copy(
        update={"stop": None}
    )
    strategies["nft-me-mint"] = STRATEGIES["nft-me-mint"].model_copy(update={"stop": None})
    engine = RiskEngine(strategies.get, portfolio)
    deny(engine.check(intent()), "stop_missing")
    assert engine.check(intent("nft-me-mint")) == Allow()  # на paper стоп ещё не обязателен


def test_degraded_strategy_cannot_trade(portfolio):
    strategies = dict(STRATEGIES)
    strategies["cex-perp-hl-trend"] = STRATEGIES["cex-perp-hl-trend"].model_copy(
        update={"status": Status.DEGRADED}
    )
    deny(RiskEngine(strategies.get, portfolio).check(intent()), "strategy_inactive")


# --- reload --------------------------------------------------------------------------


def test_reload_applies_valid_config_and_records_change(portfolio, tmp_path):
    from lab.core.risk import MemoryConfigLog

    path = tmp_path / "limits.yaml"
    path.write_text((ROOT / "config" / "limits.yaml").read_text())
    log = MemoryConfigLog()
    engine = RiskEngine(STRATEGIES.get, portfolio, limits_path=path, change_log=log)
    assert engine.check(intent(qty="2")) == Allow()

    text = path.read_text().replace("max_trade_pct: 2\n", "max_trade_pct: 1\n")
    path.write_text(text)
    result = engine.reload(by="alex")
    assert result.applied and result.change is not None
    assert result.change.who == "alex"
    assert result.change.diff == {"groups.cex.max_trade_pct": {"old": "2", "new": "1"}}
    assert log.changes == [result.change]
    deny(engine.check(intent(qty="2")), "max_trade")  # теперь максимум 100

    path.write_text(text.replace("share_pct: 40", "share_pct: 50"))  # сумма долей ≠ 100
    bad = engine.reload(by="alex")
    assert bad.applied is False and bad.error and "100" in bad.error
    assert engine.limits.for_branch("cex-perp").share_pct == D(40)  # старый конфиг остался
    assert len(log.changes) == 1


# --- общее состояние между процессами (миграция 0003) ----------------------------------


def test_db_halt_switch_and_config_log_are_shared_through_postgres(session, portfolio):
    from datetime import UTC, datetime

    from lab.core.risk import ConfigChange, DbConfigLog, DbHaltSwitch

    bot_side, worker_side = DbHaltSwitch(session), DbHaltSwitch(session)
    engine = RiskEngine(STRATEGIES.get, portfolio, halt=worker_side)
    assert engine.check(intent()) == Allow()
    bot_side.halt(by="operator")
    deny(engine.check(intent()), "halted")
    bot_side.resume(by="operator")
    assert engine.check(intent()) == Allow()

    log = DbConfigLog(session)
    when = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    change = ConfigChange(
        who="alex", when=when, path="config/limits.yaml", diff={"x": {"old": 1, "new": 2}}
    )
    log.record(change)
    assert log.changes("config/limits.yaml") == [change]


def test_maintenance_margin_from_limits_yaml_sets_liquidation_distance(engine, portfolio):
    assert engine.limits.for_branch("cex-perp").maintenance_margin_pct == D("0.5")
    # 3× → 32.83 % до ликвидации; стоп стратегии на позицию 33 % → ликвидация ближе стопа
    wide = STRATEGIES["cex-perp-hl-trend"].model_copy(
        update={"stop": StopSpec(daily_pct=D(3), max_position_pct=D(33))}
    )
    e = RiskEngine({"cex-perp-hl-trend": wide}.get, portfolio)
    deny(e.check(intent(leverage="3")), "liquidation")  # 32.83 % < 33 %
    assert e.check(intent(leverage="2")) == Allow()  # 49.5 % > 33 %


# --- reduce_only: закрыть позицию после пробоя можно всегда (История 10) --------------------


def test_reduce_only_is_allowed_after_stop_breach_but_opening_is_not(engine, portfolio):
    portfolio.stats["cex-perp-hl-trend"] = StrategyStats(pnl_day_pct=D("-3.5"), dd_pct=D(12))
    deny(engine.check(intent()), "strategy_stop_daily")
    assert engine.check(intent(qty="5", reduce_only=True)) == Allow()
    portfolio.branches["cex-perp"] = BranchState(current_usd=D(3_500), pnl_day_pct=D("-6"))
    assert engine.check(intent(qty="5", reduce_only=True)) == Allow()


def test_reduce_only_is_allowed_for_degraded_but_not_for_retired(portfolio):
    strategies = dict(STRATEGIES)
    strategies["cex-perp-hl-trend"] = STRATEGIES["cex-perp-hl-trend"].model_copy(
        update={"status": Status.DEGRADED}
    )
    engine = RiskEngine(strategies.get, portfolio)
    deny(engine.check(intent()), "strategy_inactive")
    assert engine.check(intent(reduce_only=True)) == Allow()

    strategies["cex-perp-hl-trend"] = STRATEGIES["cex-perp-hl-trend"].model_copy(
        update={"status": Status.RETIRED}
    )
    deny(engine.check(intent(reduce_only=True)), "strategy_inactive")


def test_reduce_only_bypasses_exactly_these_rules(portfolio):
    """Обходит: rung_mode, stop_missing, strategy_stop_daily, strategy_stop_dd, branch_stop,
    no_price, leverage, max_trade, branch_share, real_capital_cap, liquidation, degraded.
    Не обходит: halted, unknown_strategy, retired, venue_unavailable."""
    strategies = dict(STRATEGIES)
    strategies["cex-perp-hl-trend"] = STRATEGIES["cex-perp-hl-trend"].model_copy(
        update={"stop": None, "status": Status.DEGRADED}
    )
    strategies["nft-me-mint"] = STRATEGIES["nft-me-mint"].model_copy(update={"stop": None})
    portfolio.stats["cex-perp-hl-trend"] = StrategyStats(pnl_day_pct=D(-9), dd_pct=D(50))
    portfolio.branches["cex-perp"] = BranchState(
        current_usd=D(100), exposure_usd=D(4_000), pnl_day_pct=D(-9)
    )
    portfolio.live_deployed = D(5_000)
    portfolio.marks = {}
    portfolio.liq["BTC-USDT"] = D("99.9")
    engine = RiskEngine(strategies.get, portfolio)
    closing = intent(qty="1000", price=None, leverage="50", mode="live", reduce_only=True)
    assert engine.check(closing) == Allow()
    assert engine.check(intent("nft-me-mint", mode="live", reduce_only=True)) == Allow()

    engine.halt_switch.halt(by="operator")
    deny(engine.check(closing), "halted")
    engine.halt_switch.resume(by="operator")
    portfolio.venues_down.add("hyperliquid")
    deny(engine.check(closing), "venue_unavailable")
    portfolio.venues_down.clear()
    unknown = closing.model_copy(update={"strategy_id": "nope"})
    deny(engine.check(unknown), "unknown_strategy")


# --- ярус размещения (решение владельца 4, 27.09.2026) ------------------------------------
# Банк 10 000 → ярус 20 % = 2 000; сделка — до всей доли яруса; без плеча; стопа ветки нет.


def test_allocation_trade_may_take_the_whole_tier(engine):
    assert engine.check(intent("cex-spot-rotation", qty="20")) == Allow()  # 2 000 = доля яруса
    verdict = deny(engine.check(intent("cex-spot-rotation", qty="20.01")), "max_trade")
    assert "яруса" in verdict.reason
    # обычная стратегия той же группы по-прежнему упирается в 2 % банка
    deny(engine.check(intent(qty="2.01")), "max_trade")


def test_allocation_ignores_branch_stop_but_neighbours_do_not(engine, portfolio):
    portfolio.branches["cex-spot"] = BranchState(current_usd=D(4_000), pnl_day_pct=D(-6))
    assert engine.check(intent("cex-spot-rotation", qty="10")) == Allow()
    plain = STRATEGIES["cex-spot-rotation"].model_copy(update={"allocation": False})
    e = RiskEngine({"cex-spot-rotation": plain}.get, portfolio)
    deny(e.check(intent("cex-spot-rotation", qty="1")), "branch_stop")


def test_allocation_share_counts_the_whole_tier_not_the_branch(engine, portfolio):
    # ветка cex-spot занята под завязку — ярусу это не мешает, у него своя доля
    portfolio.branches["cex-spot"] = BranchState(current_usd=D(4_000), exposure_usd=D(4_000))
    portfolio.allocation_exposure = D(1_500)
    verdict = deny(engine.check(intent("cex-spot-rotation", qty="6")), "tier_share")
    assert "1500" in verdict.reason and "2000" in verdict.reason
    assert engine.check(intent("cex-spot-rotation", qty="5")) == Allow()


def test_allocation_has_no_leverage(engine):
    verdict = deny(engine.check(intent("cex-spot-rotation", leverage="2")), "leverage")
    assert "яруса размещения" in verdict.reason


def test_allocation_drawdown_stop_uses_portfolio_figure(engine, portfolio):
    # Как считать просадку (от вершины, с открытой позицией) — забота портфеля;
    # ядро сравнивает её с 35 % стопа карточки.
    portfolio.stats["cex-spot-rotation"] = StrategyStats(dd_pct=D("34.99"))
    assert engine.check(intent("cex-spot-rotation")) == Allow()
    portfolio.stats["cex-spot-rotation"] = StrategyStats(dd_pct=D(35))
    deny(engine.check(intent("cex-spot-rotation")), "strategy_stop_dd")


def test_allocation_without_tier_in_config_falls_back_to_group_limits(portfolio, tmp_path):
    text = (ROOT / "config" / "limits.yaml").read_text(encoding="utf-8")
    # старая раскладка: яруса нет, доля `copy` — прежние 25 %
    text = text.split("\nallocation_tier:")[0].replace(
        "share_pct: 5\n    max_trade_pct: 3", "share_pct: 25\n    max_trade_pct: 3"
    )
    path = tmp_path / "limits.yaml"
    path.write_text(text, encoding="utf-8")
    e = RiskEngine(STRATEGIES.get, portfolio, limits_path=path)
    assert e.limits.allocation_tier is None
    deny(e.check(intent("cex-spot-rotation", qty="2.01")), "max_trade")  # 2 % банка
