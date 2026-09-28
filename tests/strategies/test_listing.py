"""Шорт листингов: вход на закрытии ПЕРВОГО полного дня от даты листинга, стоп, срок.

До 28.09.2026 у стратегии не было ни одного теста. Ошибка состава — в `listing_dates`
лежал первый полный день вместо дня листинга, и вход шёл на сутки позже карточки —
прожила две недели и прошла порог: смысл даты нигде не был записан проверкой.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.contracts import Candle
from lab.strategies.registry import build

LISTING_ID = "cex-perp-paper-listing-fade-short"
COIN = "NEW/USDT:USDT"
LISTED = datetime(2026, 9, 24, tzinfo=UTC)  # день листинга: первая дневная свеча спота


def bar(day: int, close: str = "10", high: str | None = None) -> Candle:
    price = Decimal(close)
    return Candle(
        instrument=COIN,
        tf="1d",
        ts=LISTED + timedelta(days=day),
        open=price,
        high=Decimal(high) if high is not None else price,
        low=price,
        close=price,
        volume=Decimal(1),
    )


def run(bars: list[Candle]):
    strategy = build(
        LISTING_ID,
        params={"listing_dates": json.dumps({COIN: LISTED.date().isoformat()})},
        overrides={"instruments": [COIN]},
    )
    strategy.reset()
    out = []
    for b in bars:
        out += strategy.on_bar(b)
    return out


def test_short_opens_on_the_close_of_the_first_full_day():
    """Перп торгуется раньше спота; отсчёт — от дня листинга, а не от первого бара перпа."""
    signals = run([bar(day) for day in range(-5, 4)])

    opens = [s for s in signals if s.side == "sell"]
    assert len(opens) == 1
    # Бар первого полного дня (+1) закрывается в полночь дня +2 — это и есть момент решения.
    assert opens[0].decided_at == LISTED + timedelta(days=2)
    assert opens[0].instrument == COIN
    assert opens[0].meta["kind"] == "listing_short_open"


def test_perp_launched_on_the_first_full_day_enters_that_same_day():
    """Бара дня листинга у перпа нет (DYM, ID, JTO и ещё четыре из 194 в замере 13.09).

    Счёт баров потока ставил вход на сутки позже карточки; вход по календарю — нет.
    """
    signals = run([bar(day) for day in range(1, 4)])

    opens = [s for s in signals if s.side == "sell"]
    assert len(opens) == 1 and opens[0].decided_at == LISTED + timedelta(days=2)


def test_listing_before_the_stream_starts_does_not_open_a_phantom_short():
    """Прогрева у движка нет: окно замера началось через месяцы после листинга.

    Счёт баров принимал первый бар окна за день листинга, и ALICE, DYDX, GTC, LINA
    «шортились» 10.10.2021 — через 1–7 месяцев после настоящего листинга.
    """
    assert [s for s in run([bar(day) for day in range(40, 45)]) if s.side == "sell"] == []


def test_no_bar_on_the_first_full_day_means_no_entry():
    """Перпа к закрытию первого полного дня не было (EDU) — шортить было нечем."""
    assert [s for s in run([bar(day) for day in range(2, 6)]) if s.side == "sell"] == []


def test_stop_closes_at_its_level_by_the_bar_high():
    """Шорт выносит внутри дня: стоп смотрит на максимум бара, выход — по уровню стопа."""
    signals = run([bar(-1), bar(0), bar(1), bar(2, close="11", high="16")])

    closes = [s for s in signals if s.side == "buy"]
    assert len(closes) == 1
    assert closes[0].price_ref == Decimal("15")  # вход 10, стоп +50% из карточки
    assert closes[0].meta["reason"] == "stop"


def test_position_is_closed_when_the_hold_expires():
    signals = run([bar(day) for day in range(0, 40)])

    closes = [s for s in signals if s.side == "buy"]
    assert len(closes) == 1 and closes[0].meta["reason"] == "hold_expired"
    # Открыта на баре +1, держится 30 дней: закрывается на баре +31.
    assert closes[0].decided_at == LISTED + timedelta(days=32)
