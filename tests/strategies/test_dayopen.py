"""Гашение первого часа суток UTC (26.09.2026).

Правило живёт на границах суток, поэтому проверяется в первую очередь ВРЕМЯ: решение
ровно на закрытии первого часа, исполнение по открытию 01:00, выход на границе следующих
суток, сутки без первого бара не торгуются. Последний тест прогоняет правило через движок
замера — сигналы могут быть верными, а сделка всё равно открыться не на том баре.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Branch, Candle
from lab.core.measure import PaperEngine
from lab.core.measure.simulator import simulate
from lab.strategies import registry as code_registry

Q = timedelta(minutes=15)
DAY0 = datetime(2026, 3, 2, tzinfo=UTC)
SID = "cex-perp-paper-day-open-fade"
BTC, ETH = "BTC/USDT:USDT", "ETH/USDT:USDT"


def _bar(instrument: str, ts: datetime, open_: str, close: str | None = None) -> Candle:
    o = Decimal(open_)
    c = Decimal(close) if close is not None else o
    return Candle(
        instrument=instrument,
        tf="15m",
        ts=ts,
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=Decimal(1000),
    )


def _day(instrument: str, day: datetime, first_hour_to: str, rest: str = "100") -> list[Candle]:
    """Сутки 15-минуток: первый час идёт от 100 к `first_hour_to`, дальше цена `rest`."""
    bars = [_bar(instrument, day, "100", "100")]
    bars += [_bar(instrument, day + Q * i, "100", "100") for i in (1, 2)]
    bars.append(_bar(instrument, day + Q * 3, "100", first_hour_to))
    bars += [_bar(instrument, day + Q * i, rest) for i in range(4, 96)]
    return bars


def _strategy(**params):
    base = {"capital_usd": 10_000, "gross_pct": 20}
    s = code_registry.build(SID, params={**base, **params})
    s.manifest = s.manifest.model_copy(update={"instruments": [BTC, ETH]})
    return s


def _run(s, bars: list[Candle]) -> list:
    out = []
    for bar in bars:
        out.extend(s.on_bar(bar))
    return out


def _opens(signals: list) -> list:
    """Только входы: сутки из `_day` кончаются баром 23:45, и выход в них тоже есть."""
    return [x for x in signals if x.meta.get("kind") == "day_fade_open"]


def test_card_is_registered_with_the_measured_universe():
    m = code_registry.manifest(SID)
    assert m.venue == "bybit" and m.timeframe == "15m"
    assert len(m.instruments) == 8, "те же восемь перпов, на которых эффект найден"
    assert m.params["lead_minutes"] == 60, "окно — час, а не 15 минут (hold-horizon 21.09)"


def test_first_hour_up_opens_short_at_one_and_closes_at_next_midnight():
    s = _strategy()
    signals = _run(s, _day(BTC, DAY0, "102") + _day(BTC, DAY0 + timedelta(days=1), "100"))

    opened = [x for x in signals if x.meta.get("kind") == "day_fade_open"]
    closed = [x for x in signals if x.meta.get("kind") == "day_fade_close"]
    assert opened[0].side == "sell", "вырос — шорт"
    assert opened[0].decided_at == DAY0 + timedelta(hours=1), "решение на закрытии первого часа"
    assert closed[0].side == "buy" and closed[0].meta["reason"] == "day_end"
    assert closed[0].decided_at == DAY0 + timedelta(days=1), "выход ровно на границе суток"


def test_first_hour_down_opens_long():
    s = _strategy()
    signals = _run(s, _day(BTC, DAY0, "98"))
    assert [x.side for x in signals] == ["buy", "sell"], "лонг и его выход в полночь"


def test_size_is_gross_share_split_across_instruments():
    s = _strategy(gross_pct=20)
    (opened,) = _opens(_run(s, _day(BTC, DAY0, "102")))
    # 10 000 × 20% / 2 инструмента = 1 000 номинала по цене решения 102.
    assert opened.size == Decimal(1000) / Decimal(102)


def test_day_without_its_first_bar_is_not_traded():
    """Ряд начался в 00:15: открытия суток нет — нет и раннего хода."""
    s = _strategy()
    assert _run(s, _day(BTC, DAY0, "102")[1:]) == []


def test_small_move_below_threshold_is_skipped():
    s = _strategy(min_move_pct=3)
    assert _run(s, _day(BTC, DAY0, "102")) == []
    s = _strategy(min_move_pct=1)
    assert len(_opens(_run(s, _day(BTC, DAY0, "102")))) == 1


def test_gap_at_midnight_still_closes_on_first_bar_after_it():
    """Нет баров 23:45 и 00:00 — позиция не должна пережить сутки."""
    day1 = DAY0 + timedelta(days=1)
    bars = _day(BTC, DAY0, "102")[:-1] + _day(BTC, day1, "100")[1:]
    signals = _run(_strategy(), bars)
    closed = [x for x in signals if x.meta.get("kind") == "day_fade_close"]
    assert closed and closed[0].decided_at == day1 + Q * 2, "закрытие бара 00:15"
    assert not [
        x for x in signals if x.meta.get("kind") == "day_fade_open" and x.decided_at > day1
    ], "у следующих суток нет первого бара — они не торгуются"


def test_instruments_keep_separate_state():
    s = _strategy()
    days = zip(_day(BTC, DAY0, "102"), _day(ETH, DAY0, "98"), strict=True)
    merged = [b for pair in days for b in pair]
    sides = {x.instrument: x.side for x in _opens(_run(s, merged))}
    assert sides == {BTC: "sell", ETH: "buy"}


def test_engine_fills_at_one_oclock_open_and_exits_at_midnight_open():
    """Через движок: вход по открытию 01:00, выход по открытию 00:00 следующих суток."""
    s = _strategy()
    s.manifest = s.manifest.model_copy(update={"instruments": [BTC]})
    engine = PaperEngine(venue="bybit", instrument=BTC, tf="15m", branch=Branch.CEX_PERP)
    day1 = DAY0 + timedelta(days=1)
    # После первого часа цена стоит на 102, в последний бар суток сходит к 100.
    bars = _day(BTC, DAY0, "102", rest="102")
    bars[-1] = _bar(BTC, bars[-1].ts, "102", "100")
    bars += _day(BTC, day1, "100")

    result = simulate(s, bars, engine=engine, capital=Decimal(10_000))

    (trade,) = result.trades
    assert trade.side == "short"
    assert trade.opened_at == DAY0 + timedelta(hours=1)
    assert trade.closed_at == day1
    assert trade.pnl_gross > 0, "шорт от 102 к 100"
