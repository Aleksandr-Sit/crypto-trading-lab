"""Копитрейдинг (Истории 60–63): масштаб по капиталу, ссылка на сделку лидера и лаг,
правило «висящей» позиции, переизмерение лидера, контрольная группа на бирже.

Сеть не вызывается: события лидера собираются из `wallet_trade_payload`, биржевые
лидеры — из готовых ответов фейкового HTTP-транспорта.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Event
from lab.executors.cex.copy_exchange import (
    BybitLeaderboard,
    ManualEntryRequired,
    OkxLeadTraders,
    executor_for,
    make_copy_exchange_strategy,
)
from lab.feeds.chains import WalletTrade, wallet_trade_payload
from lab.ops.scheduler import Scheduler
from lab.strategies.copy import (
    CopyRecord,
    CopyStrategy,
    LeaderWatch,
    copy_metrics,
    make_copy_strategy,
    measure_extra,
)

LEADER = "So1Leader111111111111111111111111111111111"
OTHER = "So1Other1111111111111111111111111111111111"
TOKEN = "BonkMint11111111111111111111111111111111111"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _leader_trade(side: str, qty: str, value: str, ts: datetime, address=LEADER) -> WalletTrade:
    qty_d = Decimal(qty)
    return WalletTrade(
        chain="solana",
        address=address,
        tx=f"tx-{side}-{ts.isoformat()}",
        ts=ts,
        token=TOKEN,
        symbol="BONK",
        side=side,
        qty=qty_d,
        quote_asset="USDC",
        quote_qty=Decimal(value),
        value_usd=Decimal(value),
    )


def _event(trade: WalletTrade) -> Event:
    return Event(kind="wallet_trade", ts=trade.ts, payload=wallet_trade_payload(trade))


def _strategy(**kw) -> CopyStrategy:
    params = dict(
        capital_usd="1000",
        leader_capital_usd="100000",
        max_trade_usd="500",
        hanging={"timeout_s": 600, "drop_pct": "10"},
        clock=lambda: T0 + timedelta(milliseconds=1500),
    )
    params.update(kw)
    clock = params.pop("clock")
    return make_copy_strategy("solana", LEADER, venue="solana", clock=clock, **params)


def test_strategy_id_and_branch_follow_the_leader():
    strategy = _strategy()
    assert strategy.strategy_id == f"copy-solana-{LEADER.lower()}"
    assert strategy.manifest.branch == "copy"
    assert strategy.manifest.source_ref == LEADER
    assert strategy.manifest.can_backtest is False  # сделки лидера в прошлом не смоделировать


def test_copy_scales_by_capital_and_links_to_leader_trade():
    strategy = _strategy()

    (signal,) = strategy.on_event(_event(_leader_trade("buy", "50000", "10000", T0)))

    # капитал 1000 против 100000 у лидера — доля 1%: 10000 USDC → 100 USDC
    assert signal.meta["size_usd"] == "100"
    assert signal.size == Decimal("500")  # 100 USDC по цене 0.2 USDC за токен
    assert signal.side == "buy"
    assert signal.meta["leader_tx"] == f"tx-buy-{T0.isoformat()}"
    assert signal.meta["leader"] == LEADER and signal.meta["chain"] == "solana"
    assert signal.meta["lag_ms"] == 1500
    assert signal.meta["mode"] == "paper"  # DEX-исполнителя ещё нет (тикет 09)


def test_copy_size_is_capped_by_max_trade():
    strategy = _strategy(max_trade_usd="50")
    (signal,) = strategy.on_event(_event(_leader_trade("buy", "50000", "10000", T0)))
    assert signal.meta["size_usd"] == "50"


def test_other_wallets_are_ignored():
    strategy = _strategy()
    assert strategy.on_event(_event(_leader_trade("buy", "1", "1", T0, address=OTHER))) == []


def test_leader_exit_produces_closing_signal_for_held_qty():
    strategy = _strategy()
    (entry,) = strategy.on_event(_event(_leader_trade("buy", "50000", "10000", T0)))
    strategy.note_fill(TOKEN, "buy", entry.size, Decimal("0.2"), T0)

    (exit_signal,) = strategy.on_event(
        _event(_leader_trade("sell", "50000", "12000", T0 + timedelta(minutes=5)))
    )

    assert exit_signal.side == "sell" and exit_signal.size == entry.size
    assert exit_signal.meta["reason"] == "leader_exit"


def test_hanging_position_is_closed_by_time_and_by_price():
    strategy = _strategy()
    (entry,) = strategy.on_event(_event(_leader_trade("buy", "50000", "10000", T0)))
    strategy.note_fill(TOKEN, "buy", entry.size, Decimal("0.2"), T0)
    exit_ts = T0 + timedelta(minutes=5)
    strategy.on_event(_event(_leader_trade("sell", "50000", "12000", exit_ts)))  # не исполнилось

    assert strategy.hanging(exit_ts + timedelta(seconds=60)) == []
    (by_time,) = strategy.hanging(exit_ts + timedelta(seconds=601))
    assert by_time.meta["reason"] == "hanging_timeout" and by_time.side == "sell"

    fresh = _strategy()
    (entry,) = fresh.on_event(_event(_leader_trade("buy", "50000", "10000", T0)))
    fresh.note_fill(TOKEN, "buy", entry.size, Decimal("0.2"), T0)
    fresh.on_event(_event(_leader_trade("sell", "50000", "12000", exit_ts)))
    (by_price,) = fresh.hanging(exit_ts + timedelta(seconds=10), prices={TOKEN: Decimal("0.2")})
    assert by_price.meta["reason"] == "hanging_price"  # цена ушла на 16% от выхода лидера


def test_copy_in_chains_runs_on_paper_until_dex_executor_appears():
    executor = executor_for("solana", mode="paper")
    assert executor.health().status in ("ok", "degraded", "down")
    with pytest.raises(NotImplementedError):
        executor_for("solana", mode="live")
    assert executor_for("bybit", mode="paper").venue == "bybit"


def test_copy_report_gives_lag_cost_and_missed_share():
    copies = [
        CopyRecord(leader_tx="a", side="buy", leader_price=Decimal("100"),
                   copy_price=Decimal("101"), lag_ms=1200, status="copied"),
        CopyRecord(leader_tx="b", side="buy", leader_price=Decimal("100"),
                   copy_price=Decimal("102"), lag_ms=2400, status="copied"),
        CopyRecord(leader_tx="c", side="sell", leader_price=Decimal("100"),
                   copy_price=Decimal("99"), lag_ms=800, status="copied"),
        CopyRecord(leader_tx="d", side="buy", leader_price=Decimal("100"),
                   copy_price=None, lag_ms=None, status="missed", reason="нет ликвидности"),
    ]

    report = copy_metrics(copies)

    assert report["copy_lag_cost"] == Decimal("133.33")  # (100 + 200 + 100) б.п. / 3
    assert report["missed_share"] == Decimal("25")
    assert report["copy_lag_ms"] == Decimal("1466.67")


def test_leader_remeasure_is_weekly_and_drops_failing_leader():
    history = [_leader_trade("buy", "1", "10", T0), _leader_trade("sell", "1", "9", T0)]
    demoted: list[tuple[str, str]] = []

    class _Ladder:
        def demote(self, strategy_id, reason, **kw):
            demoted.append((strategy_id, reason))

    watch = LeaderWatch(
        source=lambda address, chain: history,
        ladder=_Ladder(),
        leaders=[(LEADER, "solana", f"copy-solana-{LEADER.lower()}")],
    )
    verdict = watch.remeasure(LEADER, "solana", now=T0 + timedelta(days=1))
    assert verdict.passed is False and "порог" in verdict.reason

    watch.run_all(now=T0 + timedelta(days=1))
    assert demoted and demoted[0][0] == f"copy-solana-{LEADER.lower()}"

    scheduler = Scheduler()
    scheduler.register(watch.job())
    assert "leader_remeasure" in scheduler.jobs()


OKX_LEADERS = {
    "code": "0",
    "data": [
        {
            "uniqueCode": "6DAC2D1B4B4C3F92",
            "nickName": "steady",
            "pnlRatio": "0.42",
            "winRatio": "0.61",
            "aum": "125000",
            "copyTraderNum": "180",
            "leadDays": "300",
        }
    ],
}


def test_okx_public_lead_traders_need_no_key():
    from lab.feeds.chains import FakeHttpTransport

    transport = FakeHttpTransport()
    transport.route("GET", "public-lead-traders", OKX_LEADERS)

    (leader,) = OkxLeadTraders(transport).leaders()

    assert leader.leader_id == "6DAC2D1B4B4C3F92" and leader.win_rate_pct == Decimal("61")
    assert leader.pnl_pct == Decimal("42") and leader.days == 300
    assert transport.calls[0].params["instType"] == "SWAP"


def test_bybit_leaderboard_is_web_only_and_takes_manual_id():
    board = BybitLeaderboard()
    with pytest.raises(ManualEntryRequired) as err:
        board.leaders()
    assert "веб" in str(err.value)

    leader = board.manual("bybit-leader-42", nickname="сосед")
    assert leader.leader_id == "bybit-leader-42" and leader.venue == "bybit"


def test_copy_exchange_strategy_is_a_control_group_on_small_size():
    strategy = make_copy_exchange_strategy(
        "okx", "6DAC2D1B4B4C3F92", capital_usd="200", instruments=["BTC/USDT:USDT"]
    )
    assert strategy.strategy_id == "copy-exchange-okx-6dac2d1b4b4c3f92"
    assert strategy.manifest.venue == "okx"
    assert strategy.manifest.params["control_group"] is True


def test_copy_trade_row_keeps_link_to_leader_trade(session):
    from lab.strategies.copy import CopyTradeRow

    row = CopyTradeRow(
        strategy_id=f"copy-solana-{LEADER.lower()}",
        chain="solana",
        leader_address=LEADER,
        leader_tx="tx-buy-1",
        leader_ts=T0,
        leader_side="buy",
        leader_qty=Decimal("50000"),
        leader_price=Decimal("0.2"),
        signal_id="sig-1",
        copy_price=Decimal("0.21"),
        lag_ms=1500,
        status="copied",
    )
    session.add(row)
    session.flush()

    loaded = session.get(CopyTradeRow, row.id)
    assert loaded.leader_tx == "tx-buy-1" and loaded.lag_ms == 1500
    assert loaded.copy_price == Decimal("0.21")


def test_copy_lag_cost_reaches_the_measurement():
    """Метрика ветки `copy` доезжает до замера через `extra` (таск 02)."""
    from lab.contracts import Costs
    from lab.core.measure import ClosedTrade, metrics

    records = [
        CopyRecord(leader_tx="a", side="buy", leader_price=Decimal("100"),
                   copy_price=Decimal("101"), lag_ms=1000, status="copied"),
    ]
    trade = ClosedTrade(
        instrument="BONK",
        side="long",
        qty=Decimal("1"),
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        opened_at=T0,
        closed_at=T0 + timedelta(hours=1),
        pnl_gross=Decimal("10"),
        costs=Costs(fee=Decimal("0.1")),
    )

    result = metrics(
        [trade],
        Decimal("0"),
        capital=Decimal("1000"),
        window=(T0, T0 + timedelta(days=1)),
        extra=measure_extra(records),
    )

    assert result.copy_lag_cost["lag_cost_bps"] == Decimal("100")
    assert result.copy_lag_cost["missed_share_pct"] == Decimal("0")
