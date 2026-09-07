"""Пустая база — понятные пустые состояния с действием (R01.1, R13.4): без «undefined»
и пустых таблиц."""

from decimal import Decimal

from lab.contracts import StopSpec, StrategyManifest
from lab.core.registry import Registry


def test_empty_registry_offers_two_actions(client):
    for path in ("/", "/strategies"):
        body = client.get(path).text
        assert "Реестр пуст" in body, path
        assert "добавь кандидата или запусти поиск" in body.lower(), path
        assert "lab strategy add" in body and 'href="/queue"' in body, path
        assert "<tbody>" not in body, path
        assert "undefined" not in body and "None" not in body, path


def test_empty_queue_and_graveyard_have_action(client):
    queue = client.get("/queue").text
    assert "Очередь пуста" in queue and "lab candidate add" in queue
    grave = client.get("/graveyard").text
    assert "Кладбище пусто" in grave and 'href="/strategies"' in grave
    assert "<tbody>" not in queue and "<tbody>" not in grave


def test_card_without_trades_or_measurements_shows_text_not_empty_tables(client, session):
    s = Registry(session).add(
        StrategyManifest(
            slug="fresh",
            branch="cex-spot",
            venue="bybit",
            source_kind="test",
            instruments=["BTC/USDT"],
            timeframe="1h",
            stop=StopSpec(daily_pct=Decimal(5)),
        )
    )
    body = client.get(f"/strategies/{s.id}").text
    assert "Сделок пока нет" in body
    assert "Замеров ещё не было" in body
    assert "Переходов ещё не было" in body
    assert "кривой не из чего построить" in body and "<svg" not in body
    assert "<tbody>" not in body
