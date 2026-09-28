"""Имя инструмента → папка хранилища: иероглифы не склеиваются, ASCII-пути не двигаются.

28.09.2026: у Binance пять перпов с именами из иероглифов, и прежняя замена «всё
не-ASCII → `_`» клала `龙虾/USDT:USDT` и `牛来/USDT:USDT` в одну папку `___USDT_USDT`.
Лента листингов добавляет такие монеты сама, поэтому склейка стала бы тихой порчей рядов.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle
from lab.data import CandleStore
from lab.data.funding import FundingRate, FundingStore
from lab.data.paths import safe_part

T0 = datetime(2026, 9, 1, tzinfo=UTC)
LOBSTER, BULL = "龙虾/USDT:USDT", "牛来/USDT:USDT"


def flat(instrument: str, close: str, days: int = 5) -> list[Candle]:
    price = Decimal(close)
    return [
        Candle(
            instrument=instrument,
            tf="1d",
            ts=T0 + timedelta(days=d),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal(1),
        )
        for d in range(days)
    ]


def test_ascii_names_keep_their_old_folders():
    """Существующие ряды не должны переехать: для ASCII результат прежний до символа."""
    assert safe_part("BTC/USDT:USDT") == "BTC_USDT_USDT"
    assert safe_part("1000PEPE/USDT:USDT") == "1000PEPE_USDT_USDT"
    assert safe_part("kPEPE/USDC:USDC") == "kPEPE_USDC_USDC"
    assert safe_part("BTC/USDT:USDT-260925") == "BTC_USDT_USDT-260925"
    assert safe_part("a b+c") == "a_b_c"


def test_hieroglyph_names_do_not_collide():
    assert safe_part(LOBSTER) != safe_part(BULL)
    assert safe_part("币安人生/USDT") != safe_part("我踏马来了/USDT")
    # Разделитель пути в имя папки не протекает, и знак `%` бывает только у не-ASCII.
    assert "/" not in safe_part(BULL)
    assert "%" not in safe_part("BTC/USDT:USDT")


def test_candle_store_keeps_two_hieroglyph_coins_apart(tmp_path):
    """Сквозь DuckDB: у двух монет два ряда, а не один перемешанный."""
    store = CandleStore(tmp_path)
    store.write("binance", LOBSTER, "1d", flat(LOBSTER, "1"))
    store.write("binance", BULL, "1d", flat(BULL, "2"))

    lobster = store.read("binance", LOBSTER, "1d", T0, T0 + timedelta(days=10))
    bull = store.read("binance", BULL, "1d", T0, T0 + timedelta(days=10))

    assert [c.close for c in lobster] == [Decimal(1)] * 5
    assert [c.close for c in bull] == [Decimal(2)] * 5
    assert store.count("binance", BULL, "1d") == 5


def test_funding_store_keeps_two_hieroglyph_coins_apart(tmp_path):
    store = FundingStore(tmp_path)
    store.write("binance", LOBSTER, [FundingRate(ts=T0, rate=Decimal("0.001"))])
    store.write("binance", BULL, [FundingRate(ts=T0, rate=Decimal("-0.002"))])

    assert [r.rate for r in store.read("binance", BULL, T0, T0 + timedelta(days=1))] == [
        Decimal("-0.002")
    ]
    assert store.count("binance", LOBSTER) == 1
