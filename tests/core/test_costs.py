"""Модель издержек (решение §4, §17): тарифы из config/costs.yaml, проскальзывание по стакану
и по пулу, фактические издержки по филлу. Ожидаемые числа посчитаны вручную."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lab.contracts import Book, BookLevel, Fill, OrderIntent
from lab.core.costs import (
    CostModel,
    Pool,
    UnknownFeeAsset,
    base_moved,
    fee_in_quote,
    is_spot,
    load_costs,
)


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


def test_bybit_perp_has_own_tariff_spot_unchanged(model: CostModel):
    # v2: у Bybit перпы 5.5/2 б.п., спот 10/10 (сверено по странице биржи 27.09.2026).
    # Тот же стакан: VWAP 102 при покупке 2 → тейкер перпа 2 * 102 * 0.00055 = 0.1122.
    perp = model.estimate("bybit", _intent("2"), book=_book(), perp=True)
    assert perp.fee == Decimal("0.1122")
    assert perp.slippage == Decimal("4")  # проскальзывание от рынка не зависит
    spot = model.estimate("bybit", _intent("2"), book=_book())
    assert spot.fee == Decimal("0.204")  # по умолчанию — спот, как до v2
    # мейкер перпа: лимитка внутри спреда, 1 * 99 * 0.0002
    maker = model.estimate(
        "bybit", _intent("1", order_type="limit", price="99"), book=_book(), perp=True
    )
    assert maker.fee == Decimal("0.0198")


def test_perp_without_own_block_falls_back_to_spot_row(model: CostModel):
    # У Binance перпы не сверялись и блока `perp` нет: перп считается по строке спота
    # (консервативно), а не падает и не берёт чужой тариф.
    perp = model.estimate("binance", _intent("2"), book=_book(), perp=True)
    assert perp.fee == model.estimate("binance", _intent("2"), book=_book()).fee


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
    costs = model.actual(fill, side="buy", instrument="BTC/USDT", ref_price=Decimal("100"))
    assert costs.fee == Decimal("0.204")
    assert costs.slippage == Decimal("4")
    # Продажа по 98 при референсе 100 — тоже потеря 4 (не отрицательная).
    costs = model.actual(
        fill.model_copy(update={"price": Decimal("98")}),
        side="sell",
        instrument="BTC/USDT",
        ref_price=Decimal("100"),
    )
    assert costs.slippage == Decimal("4")
    # Комиссия в BNB — не котировка и не база: прежде умножалась на цену BTC, теперь ошибка.
    with pytest.raises(UnknownFeeAsset):
        bnb = fill.model_copy(update={"fee_asset": "BNB"})
        model.actual(bnb, side="buy", instrument="BTC/USDT")


def _fill(fee: str, asset: str, qty: str = "0.01", price: str = "60000") -> Fill:
    return Fill(
        id="f",
        order_id="o",
        price=Decimal(price),
        qty=Decimal(qty),
        fee=Decimal(fee),
        fee_asset=asset,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("fill", "instrument", "fee"),
    [
        (_fill("0.6", "USDT"), "BTC/USDT", "0.6"),  # котировка — как есть
        (_fill("0.00001", "BTC"), "BTC/USDT", "0.60000"),  # покупка на споте: база × цена
        (_fill("0.6", "usdt"), "BTC/USDT:USDT", "0.6"),  # перп, регистр не важен
        (_fill("0.6", "USDC"), "BTC/USDT", "0.6"),  # доллары в любой обёртке
        (_fill("0.001", "BNB"), "BTC/USDT", None),  # третья монета: цены нет — не угадываем
        (_fill("0.001", "SOL"), "So11111111111111111111111111111111111111112", "0.001"),  # не ccxt
        (_fill("0.5", "ETH"), "0xabc:42", "0.5"),  # NFT `коллекция:токен` — котировка исполнителя
    ],
)
def test_fee_in_quote(fill: Fill, instrument: str, fee: str | None):
    assert fee_in_quote(fill, instrument) == (None if fee is None else Decimal(fee))


@pytest.mark.parametrize(
    ("fill", "instrument", "side", "moved"),
    [
        (_fill("0.00001", "BTC"), "BTC/USDT", "buy", "0.00999"),  # пришло меньше купленного
        (_fill("0.00001", "BTC"), "BTC/USDT", "sell", "0.01001"),  # ушло больше проданного
        (_fill("0.6", "USDT"), "BTC/USDT", "buy", "0.01"),  # комиссия котировкой — объём целый
        (_fill("0.001", "BNB"), "BTC/USDT", "buy", "0.01"),
        (_fill("0.00001", "BTC"), "BTC/USD:BTC", "buy", "0.01"),  # дериватив: объём — контракты
        (_fill("0.5", "ETH"), "0xabc:42", "buy", "0.01"),  # NFT — не спот по ccxt
    ],
)
def test_base_moved(fill: Fill, instrument: str, side: str, moved: str):
    assert base_moved(fill, instrument, side) == Decimal(moved)


def test_is_spot_only_by_ccxt_name():
    assert is_spot("BTC/USDT") and is_spot("PAXG/USDT")
    assert not is_spot("BTC/USDT:USDT") and not is_spot("BTC/USDT:USDT-260925")
    assert not is_spot("0xabc:42")  # NFT: двоеточие есть, но это не перп — и не спот ccxt
    assert not is_spot("So11111111111111111111111111111111111111112")


def test_model_version_is_stable_string(model: CostModel):
    assert model.version.startswith("costs-v2@")
    assert model.version == CostModel(load_costs()).version
