"""DEX-исполнители (Истории 66, 70): защита от проскальзывания, приоритет, неудачные транзакции."""

from decimal import Decimal

import pytest

from lab.contracts import OrderIntent, OrderState
from lab.executors import registry
from lab.executors.dex import (
    DexError,
    DexExecutor,
    JupiterExecutor,
    NotConnected,
    SwapLimits,
    make_executor,
    swap_limits_from_manifest,
)
from lab.executors.dex.fake import FakeSwapClient

TOKEN = "So11111111111111111111111111111111111111112"


def intent(qty: str = "100", price: str = "1", side: str = "buy") -> OrderIntent:
    return OrderIntent(
        strategy_id="meme-sol-pumpfun-early-v1",
        venue="jupiter",
        instrument=TOKEN,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        order_type="market",
        mode="paper",
        signal_id="sig-1",
        client_order_id="lab-meme-1",
    )


def executor(client: FakeSwapClient, **kw) -> JupiterExecutor:
    return JupiterExecutor(client, mode=kw.pop("mode", "paper"), **kw)


def test_four_dex_executors_are_registered():
    for name in ("jupiter", "uniswap", "pancake", "stonfi"):
        ex = registry.get(name)()
        assert isinstance(ex, DexExecutor)
        assert ex.mode == "paper"


def test_paper_swap_fills_by_quote_and_charges_pool_fee():
    client = FakeSwapClient(price=Decimal("1.02"))
    ex = executor(client)
    order = ex.place(intent(), "paper")
    assert order.state == OrderState.FILLED
    fill = ex.fills(since=order.created_at)[0]
    assert fill.price == Decimal("1.02")
    assert fill.fee > 0
    assert ex.positions()[0].qty == Decimal(100)


def test_swap_above_slippage_limit_is_refused_without_sending():
    client = FakeSwapClient(price=Decimal("1.2"), price_impact_pct=Decimal("9"))
    ex = executor(client, limits=SwapLimits(max_slippage_pct=Decimal(5)))
    order = ex.place(intent(), "paper")
    assert order.state == OrderState.REJECTED
    assert "проскальзывание" in order.reason
    assert client.sent == []


def test_priority_fee_above_limit_is_refused():
    client = FakeSwapClient()
    ex = executor(
        client,
        limits=SwapLimits(priority_fee_usd=Decimal(2), max_priority_fee_usd=Decimal("0.5")),
    )
    order = ex.place(intent(), "paper")
    assert order.state == OrderState.REJECTED
    assert "приоритет" in order.reason


def test_stuck_transaction_is_retried_with_higher_priority():
    client = FakeSwapClient(price=Decimal(1))
    client.fail_next("stuck", gas_usd=Decimal("0.01"))
    ex = executor(
        client,
        mode="live",
        limits=SwapLimits(priority_fee_usd=Decimal("0.05"), retry_priority_multiplier=Decimal(2)),
        private_key="hot-wallet",
    )
    order = ex.place(intent().model_copy(update={"mode": "live"}), "live")
    assert order.state == OrderState.FILLED
    assert [a.status for a in ex.attempts] == ["stuck", "confirmed"]
    assert client.sent[1].priority_fee_usd == Decimal("0.10")
    costs = ex.costs_of(order.id)
    assert costs.gas == Decimal("0.02"), "газ обеих попыток, включая застрявшую"
    assert costs.priority_fee == Decimal("0.15")


def test_failed_transaction_reports_reason_and_cost_of_attempt():
    client = FakeSwapClient()
    client.fail_next("mev", times=5, gas_usd=Decimal("0.01"))
    ex = executor(client, mode="live", private_key="hot-wallet")
    order = ex.place(intent().model_copy(update={"mode": "live"}), "live")
    assert order.state == OrderState.REJECTED
    assert "mev" in order.reason
    costs = ex.costs_of(order.id)
    assert costs.gas > 0, "стоимость неудачной попытки не теряется"
    assert ex.failed_costs().total > 0


def test_mode_is_per_instance():
    ex = executor(FakeSwapClient())
    with pytest.raises(DexError):
        ex.place(intent().model_copy(update={"mode": "live"}), "live")


def test_live_without_key_is_not_connected():
    ex = executor(FakeSwapClient(), mode="live")
    with pytest.raises(NotConnected):
        ex.place(intent().model_copy(update={"mode": "live"}), "live")


def test_limits_come_from_strategy_manifest():
    limits = swap_limits_from_manifest(
        {"max_slippage_pct": "3", "priority_fee_usd": "0.2", "max_priority_fee_usd": "0.4"}
    )
    assert limits.max_slippage_pct == Decimal(3)
    client = FakeSwapClient(price=Decimal(1), price_impact_pct=Decimal("4"))
    ex = executor(client)
    ex.set_limits("meme-sol-pumpfun-early-v1", limits)
    order = ex.place(intent(), "paper")
    assert order.state == OrderState.REJECTED
    assert "3" in order.reason


def test_make_executor_covers_four_venues():
    for venue in ("jupiter", "uniswap", "pancake", "stonfi"):
        ex = make_executor(venue)
        assert ex.venue == venue
        assert ex.rights().withdraw is False


def test_jupiter_client_parses_route_quote_and_refuses_to_send_without_key():
    from lab.executors.dex.clients import JupiterClient, make_client
    from lab.executors.dex.swap import SwapPlan
    from lab.feeds.chains.fake import FakeHttpTransport

    transport = FakeHttpTransport().route(
        "GET",
        "/swap/v1/quote",
        {
            "inAmount": "100000000",
            "outAmount": "50000000",
            "priceImpactPct": "0.004",
            "routePlan": [{"swapInfo": {"label": "Raydium"}}],
        },
    )
    client = JupiterClient(transport)
    quote = client.quote(TOKEN, "buy", Decimal(50), price=Decimal(2))
    assert quote.out_amount == Decimal(50)
    assert quote.price == Decimal(2)
    assert quote.price_impact_pct == Decimal("0.4")
    assert quote.route == "Raydium"

    plan = SwapPlan(TOKEN, "buy", Decimal(50), quote, Decimal(1), Decimal("0.05"))
    with pytest.raises(NotConnected):
        client.send(plan)
    assert make_client("pancake").venue == "pancake"
