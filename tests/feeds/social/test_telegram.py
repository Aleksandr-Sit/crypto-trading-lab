"""Журнал публичных сигналов авторов (R06.2, G08.1): Telethon-читалка без ключей — «недоступен»
и не падает; парсер сигналов на образцах; `signals_public` пишется с `published_at` до оценки;
оценка форвардом через `core.measure.run(mode="forward")` без исполнения."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.feeds.social import parser, telegram
from lab.feeds.social.journal import PublicSignalsJournal

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_reader_without_keys_reports_unavailable_and_does_not_raise():
    reader = telegram.TelegramReader.from_env({})
    assert reader.available() is False
    health = reader.health()
    assert health.status == "down" and "недоступен" in health.detail
    assert reader.read("pifagortrade", since=T0) == []


def test_channels_from_authors_skip_placeholders():
    chans = telegram.channels_from_authors()
    assert ("pifagor", "pifagortrade") in chans
    assert ("coinmetrika", "CoinMetrika") in chans
    assert all("впиши" not in c for _, c in chans)


@pytest.mark.parametrize(
    "text, instrument, side, entry, targets, stop",
    [
        (
            "BTC/USDT LONG\nВход: 61 200\nЦели: 62 500, 64 000\nСтоп: 59 800",
            "BTC/USDT",
            "long",
            "61200",
            ["62500", "64000"],
            "59800",
        ),
        ("#ETH шорт от 3450, тейк 3300, стоп 3520", "ETH/USDT", "short", "3450", ["3300"], "3520"),
        ("SOL покупка по рынку, цель 180", "SOL/USDT", "long", None, ["180"], None),
        ("Продаю LINK 14.2 → 13", "LINK/USDT", "short", "14.2", ["13"], None),
    ],
)
def test_parser_extracts_instrument_side_targets(text, instrument, side, entry, targets, stop):
    p = parser.parse_signal(text)
    assert p is not None
    assert p.instrument == instrument and p.side == side
    assert p.entry == (Decimal(entry) if entry else None)
    assert [str(t) for t in p.targets] == targets
    assert p.stop == (Decimal(stop) if stop else None)


def test_parser_returns_none_for_non_signal_text():
    assert parser.parse_signal("Всем привет! Сегодня разбираем токеномику нового проекта.") is None
    assert parser.parse_signal("Медвежий рынок ещё активен, ждём.") is None


def test_journal_records_before_outcome_and_evaluates_forward(session):
    j = PublicSignalsJournal(session)
    text = "BTC/USDT LONG\nВход: 100\nЦели: 110\nСтоп: 95"
    row = j.record(
        author="pifagor", channel="pifagortrade", message_ref="42", published_at=T0, text=text
    )
    assert row.published_at == T0 and row.instrument == "BTC/USDT" and row.side == "long"
    assert row.outcome == {}  # исход не известен в момент записи
    assert (
        j.record(
            author="pifagor", channel="pifagortrade", message_ref="42", published_at=T0, text=text
        ).id
        == row.id
    )  # повтор — та же запись

    def price_at(instrument, ts):  # цена дошла до цели через 3 дня
        return Decimal(100) if ts < T0 + timedelta(days=3) else Decimal(112)

    outcome = j.evaluate(
        row.id, price_at=price_at, horizon=timedelta(days=7), now=T0 + timedelta(days=8)
    )
    assert outcome["result"] == "target" and Decimal(outcome["pnl_pct"]) > 0
    m = j.forward_measure(
        "pifagor",
        window=(T0, T0 + timedelta(days=30)),
        price_at=price_at,
        now=T0 + timedelta(days=8),
    )
    assert m.mode == "forward" and m.metrics.n_trades == 1 and m.metrics.net_pnl > 0
    assert m.strategy_id == "cex-spot-public-pifagor"
