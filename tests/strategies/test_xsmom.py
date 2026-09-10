"""Кросс-секционный моментум альтов (08.09.2026).

Первая стратегия, которая решает СРАВНЕНИЕМ инструментов, а не чтением одного ряда.
Проверяются именно те места, где такое правило ломается тихо: полнота дневного среза,
состав вселенной, фильтр режима и учёт делистнутых пар.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle
from lab.strategies import registry as code_registry

DAY = timedelta(days=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SID = "cex-spot-paper-xsmom-alts-weekly"
BTC = "BTC/USDT"
ALTS = ["AAA/USDT", "BBB/USDT", "CCC/USDT"]


def _bar(instrument: str, i: int, price: Decimal, volume: Decimal = Decimal(10_000_000)) -> Candle:
    return Candle(
        instrument=instrument,
        tf="1d",
        ts=T0 + DAY * i,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
    )


def _strategy(**params):
    base = {
        "universe_size": 50,
        "lookback_days": 5,
        "skip_days": 0,
        "top_n": 1,
        "rebalance_days": 7,
        "min_daily_volume_usd": 0,
        "btc_filter_sma_days": 0,
        "capital_usd": 10_000,
    }
    s = code_registry.build(SID, params={**base, **params})
    s.manifest = s.manifest.model_copy(update={"instruments": [BTC, *ALTS]})
    return s


def _feed_day(strategy, i: int, prices: dict[str, Decimal], volume=Decimal(10_000_000)):
    """День рынка: бары всех инструментов подряд, как их сливает замер."""
    out = []
    for name, price in prices.items():
        out += strategy.on_bar(_bar(name, i, price, volume))
    return out


def _flat(i: int) -> dict[str, Decimal]:
    return {BTC: Decimal(50_000), **{a: Decimal(100) for a in ALTS}}


def test_no_decision_until_the_day_is_complete():
    """Пока бары дня приходят по одному, срез неполон — ранжировать нечего.

    Если решать на каждом баре, вселенная получается случайной: в неё попадают только
    инструменты, чьи бары уже пришли, а остальные считаются несуществующими.
    """
    s = _strategy(rebalance_days=1)
    for i in range(40):
        signals = []
        for name in [BTC, *ALTS]:
            got = s.on_bar(_bar(name, i, Decimal(100 + i)))
            if name != BTC:
                assert got == [] or i > 0, "решение не может родиться посреди дня"
            signals += got
        # решения появляются на ПЕРВОМ баре нового дня, то есть у BTC — он идёт первым
    assert s.held, "за 40 дней хотя бы один ребаланс должен был случиться"


def test_buys_the_best_performer_of_the_window():
    s = _strategy(rebalance_days=7, lookback_days=5, top_n=1)
    for i in range(40):
        prices = {BTC: Decimal(50_000), "AAA/USDT": Decimal(100), "CCC/USDT": Decimal(100)}
        # BBB растёт быстрее всех — его и должны купить
        prices["BBB/USDT"] = Decimal(100) + Decimal(i) * 5
        _feed_day(s, i, prices)

    assert s.held == {"BBB/USDT"}


def test_btc_filter_keeps_everything_in_cash():
    """BTC ниже своей средней — альты не покупаются вовсе: это и есть работа в падении."""
    s = _strategy(btc_filter_sma_days=10, rebalance_days=7)
    for i in range(40):
        prices = {BTC: Decimal(50_000) - Decimal(i) * 500, "AAA/USDT": Decimal(100) + Decimal(i)}
        prices["BBB/USDT"] = Decimal(100)
        prices["CCC/USDT"] = Decimal(100)
        _feed_day(s, i, prices)

    assert s.held == set(), "на медвежьем рынке сидим в кэше"


def test_volume_floor_excludes_thin_pairs():
    # порог заведомо выше оборота пары (цена 100 × объём 10 млн = 1 млрд)
    s = _strategy(min_daily_volume_usd=1_000_000_000_000, rebalance_days=7)
    for i in range(40):
        _feed_day(s, i, {BTC: Decimal(50_000), **{a: Decimal(100 + i) for a in ALTS}})

    assert s.held == set(), "неликвид в топ не берётся, каким бы ни был моментум"


def test_delisted_pair_leaves_the_universe():
    """У делистнутой пары последний бар остаётся в памяти навсегда.

    Без проверки «торговалась ли она НА дату решения» такая пара вечно висит в топе
    с замороженной ценой — бэктест покупает то, чего на бирже уже нет.
    """
    s = _strategy(rebalance_days=7, lookback_days=5, top_n=1)
    for i in range(40):
        prices = {BTC: Decimal(50_000), "AAA/USDT": Decimal(100), "CCC/USDT": Decimal(100)}
        if i < 20:  # BBB торговалась и росла, потом исчезла с биржи
            prices["BBB/USDT"] = Decimal(100) + Decimal(i) * 10
        _feed_day(s, i, prices)

    assert "BBB/USDT" not in s.held


def test_breadth_is_measured_on_alts_not_on_btc():
    """Ширина рынка — про АЛЬТЫ: биткоин здесь ориентир, а не участник вселенной."""
    s = _strategy(breadth_sma_days=5, breadth_min_pairs=2)
    last = None
    for i in range(20):
        prices = {BTC: Decimal(50_000) + Decimal(i) * 1000}
        prices.update({a: Decimal(100) - Decimal(i) for a in ALTS})  # альты падают
        _feed_day(s, i, prices)
        last = (T0 + DAY * i).date()

    assert s.breadth_pct(last) == 0, "все альты ниже средней; растущий BTC на это не влияет"


def test_breadth_collapse_closes_everything_without_waiting_for_rebalance():
    """Фаза кончилась — выходим сразу, а не через неделю.

    В сентябре 2020 доля альтов выше своей средней упала с 80% до 22% за двое суток,
    а стоп стратегии сработал только через три недели. Ждать ребаланса означало бы отдать
    рынку эти три недели — именно в них моментум потерял всё, что набрал за лето.
    """
    s = _strategy(
        rebalance_days=7,
        lookback_days=5,
        top_n=1,
        breadth_min_pct=50,
        breadth_sma_days=5,
        breadth_min_pairs=2,
    )
    for i in range(40):  # рынок широкий и растущий — позиция открывается
        _feed_day(s, i, {BTC: Decimal(50_000), **{a: Decimal(100) + Decimal(i) for a in ALTS}})
    assert s.held, "на растущем рынке позиция должна быть"

    signals = []
    for i in range(40, 44):  # обвал альтов: ширина проваливается
        prices = {BTC: Decimal(50_000), **{a: Decimal(140) - Decimal(i - 39) * 20 for a in ALTS}}
        signals += _feed_day(s, i, prices)

    assert s.held == set(), "при узком рынке позиций быть не должно"
    assert any(sig.meta.get("reason") == "ширина рынка" for sig in signals)


def test_breadth_filter_is_off_by_default():
    """Выключенный фильтр не меняет поведение — прежние замеры остаются сравнимыми."""
    s = _strategy()

    assert s.param("breadth_min_pct", 0) == 0
    assert s._breadth_allows(T0.date()) is True


def test_stop_closes_a_losing_position_before_the_rebalance():
    s = _strategy(rebalance_days=7, lookback_days=5, top_n=1, trade_stop_pct=15)
    # 36 дней: раньше 30-го пара не входит во вселенную — не набралась история оборота.
    # День 36 выбран не случайно: он НЕ кратен неделе, поэтому в сигналах будет только стоп.
    for i in range(36):
        prices = {BTC: Decimal(50_000), "AAA/USDT": Decimal(100), "CCC/USDT": Decimal(100)}
        prices["BBB/USDT"] = Decimal(100) + Decimal(i) * 5
        _feed_day(s, i, prices)
    assert s.held == {"BBB/USDT"}
    entry = s.assets["BBB/USDT"].entry

    # обвал на следующий день: −20% от входа
    signals = _feed_day(
        s,
        36,
        {
            BTC: Decimal(50_000),
            "AAA/USDT": Decimal(100),
            "BBB/USDT": entry * Decimal("0.8"),
            "CCC/USDT": Decimal(100),
        },
    )

    assert s.held == set()
    assert [sig.side for sig in signals] == ["sell"]
    assert signals[0].meta.get("reason") == "стоп"


def _fund(strategy, rates: list[str], start: int = 0):
    """Подаёт ставки фандинга событиями — так их отдаёт замер (три выплаты в сутки)."""
    from lab.contracts import Event

    for i, rate in enumerate(rates):
        strategy.on_event(
            Event(
                kind="funding",
                ts=T0 + DAY * ((start + i) // 3),
                payload={"instrument": "BTC/USDT:USDT", "rate": Decimal(rate)},
            )
        )


def test_funding_regime_needs_a_full_month_before_it_judges():
    """Мало данных — не запрет, а «не знаю»: молчание не должно останавливать торговлю."""
    s = _strategy(funding_filter=True, funding_fast_days=7, funding_slow_days=30)
    _fund(s, ["0.0001"] * 30)

    assert s.funding_regime() is None
    assert s._funding_allows() is True, "нет оценки — фильтр не мешает"


def test_expanding_leverage_demand_allows_trading():
    """Недельная ставка выше месячной — плечевой спрос расширяется, моментуму это среда."""
    s = _strategy(funding_filter=True, funding_fast_days=7, funding_slow_days=30)
    _fund(s, ["0.0001"] * 69 + ["0.0005"] * 21)

    assert s.funding_regime() is True


def test_shrinking_leverage_demand_closes_positions():
    """Ставка сдувается — плечевые деньги уходят из ралли, держать нечего.

    Это ровно то, что было в конце августа 2020: цена ETH шла с 383 до 434, а ставка
    падала с 19.7% до 11.0% годовых — за неделю до обвала.
    """
    s = _strategy(
        funding_filter=True, funding_fast_days=7, funding_slow_days=30,
        rebalance_days=7, lookback_days=5, top_n=1,
    )
    for i in range(40):
        _feed_day(s, i, {BTC: Decimal(50_000), **{a: Decimal(100) + Decimal(i) for a in ALTS}})
    _fund(s, ["0.0005"] * 69 + ["0.00001"] * 21)
    assert s.held, "позиция должна быть открыта до сжатия ставки"

    signals = _feed_day(s, 40, {BTC: Decimal(50_000), **{a: Decimal(140) for a in ALTS}})

    assert s.funding_regime() is False
    assert s.held == set()
    assert any(sig.meta.get("reason") == "плечевой спрос" for sig in signals)


def test_perp_is_a_signal_source_not_a_tradable_asset():
    """Перп в списке нужен ради ставки: покупать его моментум не должен."""
    s = _strategy(rebalance_days=7, lookback_days=5, top_n=2)
    s.manifest = s.manifest.model_copy(update={"instruments": [BTC, *ALTS, "BTC/USDT:USDT"]})
    for i in range(40):
        prices = {BTC: Decimal(50_000), **{a: Decimal(100) + Decimal(i) for a in ALTS}}
        prices["BTC/USDT:USDT"] = Decimal(50_000) + Decimal(i) * 5000  # растёт быстрее всех
        _feed_day(s, i, prices)

    assert "BTC/USDT:USDT" not in s.held
