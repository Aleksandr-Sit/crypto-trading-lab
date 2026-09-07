"""NFT-исполнители: покупка по флору, листинг с роялти, минт и его метрики."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import MintAttemptSpec, OrderIntent, OrderState
from lab.executors import registry
from lab.executors.nft import (
    EvmMintExecutor,
    FakeMintClient,
    MintExecutor,
    NftError,
    NftExecutor,
    SolanaMintExecutor,
    check_trading_access,
    make_executor,
    mint_metrics,
)
from lab.feeds.nft import FakeNftMarket

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _intent(
    side="buy",
    instrument="hype",
    price="10",
    qty="1",
    mode="paper",
    strategy_id="nft-secondary-hype-v1",
):
    return OrderIntent(
        strategy_id=strategy_id,
        venue="magiceden",
        instrument=instrument,
        side=side,
        qty=Decimal(qty),
        price=None if price is None else Decimal(price),
        order_type="limit" if price else "market",
        mode=mode,
        signal_id="sig-1",
        client_order_id="lab-nft-1",
    )


def test_paper_buy_uses_market_floor_when_price_is_not_given():
    market = FakeNftMarket(market="magiceden")
    market.seed_floor("hype", Decimal("7.5"))
    ex = NftExecutor(market, mode="paper")
    order = ex.place(_intent(price=None), mode="paper")
    assert order.state == OrderState.FILLED
    fills = ex.fills(since=NOW - timedelta(days=1))
    assert fills[-1].price == Decimal("7.5")
    assert ex.positions()[0].instrument == "hype"


def test_buy_above_floor_premium_is_refused_before_sending():
    market = FakeNftMarket(market="magiceden")
    market.seed_floor("hype", Decimal(10))
    ex = NftExecutor(market, mode="paper")
    order = ex.place(_intent(price="20"), mode="paper")  # +100% к флору при потолке 5%
    assert order.state == OrderState.REJECTED and "флор" in order.reason


def test_sell_costs_carry_royalty_marketplace_fee_and_gas():
    ex = NftExecutor(FakeNftMarket(market="opensea"), mode="paper")
    ex.venue = "opensea"
    buy = ex.place(_intent(price="100"), mode="paper")
    sell = ex.place(_intent(side="sell", price="200"), mode="paper")
    assert ex.costs_of(buy.id).royalty == Decimal(0), "роялти платит продавец"
    costs = ex.costs_of(sell.id)
    assert costs.royalty == Decimal(10) and costs.fee == Decimal(5)  # 5% и 2.5% от 200
    assert costs.gas > 0


def test_order_in_a_foreign_mode_is_refused():
    ex = NftExecutor(mode="paper")
    with pytest.raises(NftError):
        ex.place(_intent(mode="live"), mode="live")


def test_read_only_market_has_no_executor():
    with pytest.raises(NftError):
        make_executor("blur", mode="paper")
    assert "blur" not in registry.all()


def test_nft_executors_are_registered():
    names = set(registry.all())
    assert {"magiceden", "opensea", "zora", "nft_mint_solana", "nft_mint_evm"} <= names


# -- минт (Истории 76, 77; G09) -----------------------------------------------------------


def _spec(qty=1, price="1", fee="0.1", mode="paper"):
    return MintAttemptSpec(
        collection="hype",
        chain="solana",
        market="magiceden",
        qty=qty,
        max_price=Decimal(price),
        priority_fee=Decimal(fee),
        mode=mode,
    )


def test_paper_mint_records_attempt_and_costs():
    ex = SolanaMintExecutor(mode="paper")
    result = ex.mint(_spec(), strategy_id="nft-mint-priority-fee-v1", variant="priority-fee")
    assert result.ok and result.minted == 1
    attempt = ex.attempts[-1]
    assert attempt.variant == "priority-fee" and attempt.ok
    assert attempt.confirm_latency_ms is not None


def test_failed_mint_costs_money_and_shows_up_in_metrics():
    client = FakeMintClient()
    client.script("failed", "confirmed")
    ex = SolanaMintExecutor(mode="live", client=client, private_key="key")
    result = ex.mint(
        _spec(mode="live"), strategy_id="nft-mint-priority-fee-v1", variant="priority-fee"
    )
    assert result.ok, "вторая попытка прошла"
    metrics = mint_metrics(ex.attempts)
    assert metrics["mint_attempts"] == 2
    assert metrics["mint_success_rate"] == Decimal("0.5")
    assert metrics["mint_cost_failed"] > 0, "неудачная попытка стоила газа"


def test_mint_over_max_price_is_refused_without_sending():
    client = FakeMintClient()
    ex = EvmMintExecutor(mode="live", client=client, private_key="key")
    result = ex.mint(_spec(price="0.0001", mode="live"), price=Decimal(5))
    assert not result.ok and "цена" in result.detail
    assert client.sent == [], "до сети дело дойти не должно"


def test_multi_wallet_variant_sends_from_each_wallet():
    client = FakeMintClient()
    ex = SolanaMintExecutor(mode="live", client=client, private_key="key")
    ex.mint(_spec(mode="live"), wallets=["w1", "w2", "w3"], variant="multi-wallet")
    assert [a.wallet for a in ex.attempts] == ["w1", "w2", "w3"]
    assert mint_metrics(ex.attempts)["mint_attempts"] == 3


def test_live_mint_without_key_is_refused():
    from lab.executors.nft import NotConnected

    with pytest.raises(NotConnected):
        SolanaMintExecutor(mode="live").mint(_spec(mode="live"))


def test_mint_metrics_on_empty_history_are_not_invented():
    metrics = mint_metrics([])
    assert metrics["mint_attempts"] == 0 and metrics["mint_success_rate"] is None


def test_check_trading_access_marks_branch_read_only(session):
    ex = MintExecutor(mode="live")  # ключа нет — торговать нечем
    mode = check_trading_access(ex, session=session)
    assert mode.branch == "nft" and mode.read_only is True
    assert "ключ" in mode.reason.lower() or "нет" in mode.reason.lower()
