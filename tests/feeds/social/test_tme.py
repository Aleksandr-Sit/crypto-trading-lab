"""Читалка каналов через веб-превью `t.me/s/<канал>` — путь без ключей Telethon.

Разметка в `fixtures/tme_preview*.html` записана руками по строению страницы t.me/s:
пост — блок `tgme_widget_message` с `data-post="канал/НОМЕР"`, текст — `.tgme_widget_message_text`,
дата — `<time datetime=...>`. Сеть в тестах не трогается: транспорт подставной."""

from datetime import UTC, datetime
from pathlib import Path

from lab.feeds import social
from lab.feeds.quota import MemoryFeedsRegistry
from lab.feeds.social import telegram, tme

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def reader(*routes, offline: bool = False, **kw):
    transport = tme.FakeTmeTransport(offline=offline)
    for match, html in routes:
        transport.route(match, html)
    return tme.TmePreviewReader(transport, **kw), transport


def test_reads_id_date_and_text_of_every_post():
    r, _ = reader(("pifagortrade", fixture("tme_preview")))
    msgs = r.read("pifagortrade", since=datetime(2026, 8, 1, tzinfo=UTC))

    assert [m.message_id for m in msgs] == [1203, 1201]  # свежие первыми, пост без текста пропущен
    assert all(m.channel == "pifagortrade" for m in msgs)
    assert msgs[0].published_at == datetime(2026, 9, 2, 18, 40, tzinfo=UTC)
    assert msgs[1].published_at == datetime(2026, 9, 1, 9, 15, tzinfo=UTC)
    assert msgs[1].text == "BTC/USDT LONG\nВход: 61 200\nЦели: 62 500, 64 000\nСтоп: 59 800"
    assert msgs[1].ref == "1201"


def test_strips_tags_and_unfolds_entities():
    r, _ = reader(("pifagortrade", fixture("tme_preview")))
    latest = r.read("pifagortrade", since=datetime(2026, 8, 1, tzinfo=UTC))[0]
    assert latest.text == (
        '"Уровни" & риск — смотрим внимательно\n'
        "ETH шорт от 3450, тейк 3300, стоп 3520\n"
        "не финсовет"
    )


def test_pagination_walks_back_with_before_and_stops_on_empty_page():
    r, t = reader(
        ("before=1199", fixture("tme_empty")),
        ("before=1201", fixture("tme_preview_before")),
        ("pifagortrade", fixture("tme_preview")),
    )
    msgs = r.read("pifagortrade", since=datetime(2026, 8, 1, tzinfo=UTC), limit=50)

    assert [m.message_id for m in msgs] == [1203, 1201, 1200, 1199]
    assert t.calls == [
        "https://t.me/s/pifagortrade",
        "https://t.me/s/pifagortrade?before=1201",
        "https://t.me/s/pifagortrade?before=1199",
    ]  # пустая страница — конец, четвёртого запроса нет


def test_does_not_ask_for_older_pages_once_since_is_passed():
    r, t = reader(
        ("before=1201", fixture("tme_preview_before")),
        ("pifagortrade", fixture("tme_preview")),
    )
    msgs = r.read("pifagortrade", since=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), limit=50)

    assert [m.message_id for m in msgs] == [1203]
    assert t.calls == ["https://t.me/s/pifagortrade"]


def test_health_is_ok_while_preview_page_gives_posts():
    r, _ = reader(("pifagortrade", fixture("tme_preview")))
    assert r.available() is True
    assert r.health(channel="pifagortrade").status == "ok"


def test_closed_channel_degrades_to_down_and_empty_read():
    r, _ = reader(("closedchannel", fixture("tme_no_preview")))
    health = r.health(channel="closedchannel")
    assert health.status == "down" and "закрыт" in health.detail
    assert r.read("closedchannel", since=datetime(2026, 8, 1, tzinfo=UTC)) == []


def test_transport_failure_degrades_to_down_and_empty_read():
    r, _ = reader(offline=True)
    health = r.health(channel="pifagortrade")
    assert health.status == "down" and "нет связи" in health.detail
    assert r.read("pifagortrade", since=datetime(2026, 8, 1, tzinfo=UTC)) == []


def test_every_page_request_is_counted_by_quota():
    quota = MemoryFeedsRegistry()
    r, _ = reader(
        ("before=1199", fixture("tme_empty")),
        ("before=1201", fixture("tme_preview_before")),
        ("pifagortrade", fixture("tme_preview")),
        quota=quota,
    )
    r.read("pifagortrade", since=datetime(2026, 8, 1, tzinfo=UTC), limit=50)
    assert quota.calls[tme.FEED_ID] == 3


def test_factory_takes_telethon_only_with_both_keys_and_web_preview_otherwise():
    assert isinstance(social.make_reader({}), tme.TmePreviewReader)
    assert isinstance(social.make_reader({"TELEGRAM_API_ID": "1"}), tme.TmePreviewReader)
    keyed = social.make_reader({"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "hash"})
    assert isinstance(keyed, telegram.TelegramReader) and keyed.api_id == "1"
    assert social.make_reader is tme.make_reader
