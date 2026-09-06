"""Модель издержек (решение §4, §17): тарифы из config/costs.yaml, проскальзывание по стакану
и по пулу, фактические издержки по филлу. Ожидаемые числа посчитаны вручную."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.contracts import Book, BookLevel, Fill, OrderIntent
from lab.core.costs import CostModel, Pool, load_costs


def _intent(qty: str, side: str = "buy", order_type: str = "market", price: str | None = None):
    return OrderIntent(
        strategy_id="cex-spot-test-x",
        venue="bybit",
        instrument="BTC/USDT",
        side=side,
        qty=Decimal(qty),
        price=None if price is None else Decimal(price),
        order_type=order_type,
        mode="paper",
        signal_id="s1",
        client_order_id="c1",
    )


def _book() -> Book:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    return Book(
        instrument="BTC/USDT",
        ts=ts,
        bids=[BookLevel(price=Decimal("99"), qty=Decimal("1"))],
        asks=[
            BookLevel(price=Decimal("101"), qty=Decimal("1")),
            BookLevel(price=Decimal("103"), qty=Decimal("2")),
        ],
    )


@pytest.fixture(scope="module")
def model() -> CostModel:
    return CostModel(load_costs())


def test_taker_fee_and_book_slippage_bybit(model: CostModel):
    # Мид = 100. Покупка 2 BTC съедает 1 по 101 и 1 по 103 → VWAP 102 → проскальзывание 2*2 = 4.
    # Bybit taker 0.1% от оборота по VWAP: 2 * 102 * 0.001 = 0.204.
    costs = model.estimate("bybit", _intent("2"), book=_book())
    assert costs.slippage == Decimal("4")
    assert costs.fee == Decimal("0.204")
    assert costs.funding == 0 and costs.gas == 0 and costs.royalty == 0
    assert costs.total == Decimal("4.204")


def test_maker_fee_for_limit_order(model: CostModel):
    # Лимитка не пересекает стакан → maker-тариф Binance 0.1%... в конфиге maker бинанс 0.1%.
    # Берём OKX: maker 0.08%: 1 * 99 * 0.0008 = 0.0792, проскальзывания нет.
    costs = model.estimate("okx", _intent("1", order_type="limit", price="99"), book=_book())
    assert costs.slippage == 0
    assert costs.fee == Decimal("0.0792")


def test_pool_constant_product_slippage(model: CostModel):
    # Пул x*y=k с ликвидностью 10 000 USD на стороне котировки, своп на 100 USD:
    # эффективная цена хуже спота на dx/(x+dx) = 100/10100 → 0.990099...% от 100 = ~0.990099 USD.
    # fee пула 0.3% = 0.3; приоритет + газ по конфигу solana-jupiter: 0.02 + 0.01.
    intent = _intent("1", side="buy").model_copy(
        update={"venue": "jupiter", "instrument": "X/USDC"}
    )
    pool = Pool(liquidity_quote=Decimal("10000"), price=Decimal("100"))
    costs = model.estimate("jupiter", intent, pool=pool)
    assert costs.slippage.quantize(Decimal("0.000001")) == Decimal("0.990099")
    assert costs.fee == Decimal("0.3")
    assert costs.priority_fee == Decimal("0.02")
    assert costs.gas == Decimal("0.01")


def test_nft_royalty_and_marketplace_fee(model: CostModel):
    # Magic Eden: комиссия площадки 2%, роялти 5% (по умолчанию) от цены 10 USD.
    intent = _intent("1").model_copy(update={"venue": "magiceden", "price": Decimal("10")})
    costs = model.estimate("magiceden", intent)
    assert costs.fee == Decimal("0.2")
    assert costs.royalty == Decimal("0.5")
    assert costs.gas == Decimal("0.01")


def test_unknown_venue_is_error(model: CostModel):
    with pytest.raises(KeyError):
        model.estimate("nyse", _intent("1"))


def test_actual_from_fill(model: CostModel):
    # Купили 2 по 102 при референсе 100: проскальзывание 4, комиссия из филла в котируемом активе.
    fill = Fill(
        id="f1",
        order_id="o1",
        price=Decimal("102"),
        qty=Decimal("2"),
        fee=Decimal("0.204"),
        fee_asset="USDT",
        ts=datetime(2026, 1, 1, tzinfo=UTC),
    )
    costs = model.actual(fill, side="buy", ref_price=Decimal("100"))
    assert costs.fee == Decimal("0.204")
    assert costs.slippage == Decimal("4")
    # Продажа по 98 при референсе 100 — тоже потеря 4 (не отрицательная).
    costs = model.actual(
        fill.model_copy(update={"price": Decimal("98")}), side="sell", ref_price=Decimal("100")
    )
    assert costs.slippage == Decimal("4")


def test_model_version_is_stable_string(model: CostModel):
    assert model.version.startswith("costs-v1@")
    assert model.version == CostModel(load_costs()).version
