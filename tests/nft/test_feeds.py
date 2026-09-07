"""Площадки, квоты, ключ OpenSea и парсер календаря. Сеть не трогаем — фейковый транспорт."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import MintAttemptSpec
from lab.feeds.chains import FakeHttpTransport
from lab.feeds.nft import (
    BlurMarket,
    HtmlCalendar,
    MagicEdenMarket,
    NftDisabled,
    NftReadOnly,
    OpenSeaKey,
    TensorMarket,
    make_market,
    parse_ld_events,
)
from lab.feeds.quota import MemoryFeedsRegistry

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
PAGE = """
<html><head>
<script type="application/ld+json">
{"@type": "Event", "name": "Hype Apes", "startDate": "2026-09-10T18:00:00Z",
 "offers": {"price": "0.05"}, "organizer": {"name": "alice"},
 "url": "https://nftcalendar.io/event/hype-apes/"}
</script>
</head><body>всё остальное — вёрстка</body></html>
"""


def test_magiceden_reads_floor_and_spends_quota():
    quota = MemoryFeedsRegistry()
    transport = FakeHttpTransport()
    transport.route("GET", "/collections/degods/stats", {"floorPrice": 12_000_000_000,
                                                          "volume24hr": 800, "listedCount": 30})
    transport.route("GET", "/collections/degods", {"name": "DeGods", "totalItems": 10000,
                                                    "creator": "alice"})
    market = MagicEdenMarket(transport, quota=quota)
    stats = market.collection("degods")
    assert stats.floor == Decimal(12)  # лампорты приведены к SOL
    assert stats.name == "DeGods" and stats.currency == "SOL"
    assert quota.used["magiceden"] == 2, "каждый вызов площадки идёт через квоту"


def test_magiceden_launchpad_feeds_upcoming():
    transport = FakeHttpTransport()
    transport.route(
        "GET",
        "/launchpad/collections",
        [{"symbol": "hype", "name": "Hype", "launchDatetime": "2026-09-10T18:00:00Z",
          "price": 1.5, "size": 1000, "minted": 400}],
    )
    market = MagicEdenMarket(transport)
    mints = market.upcoming()
    assert [m.collection for m in mints] == ["hype"]
    assert mints[0].price == Decimal("1.5") and mints[0].starts_at.year == 2026
    slot = market.launchpad_slot("hype")
    assert slot.fill == Decimal("0.4")


def test_blur_is_read_only_and_refuses_to_mint():
    market = BlurMarket(FakeHttpTransport())
    assert market.read_only is True
    with pytest.raises(NftReadOnly):
        market.mint(
            MintAttemptSpec(collection="x", chain="ethereum", market="blur", qty=1,
                            max_price=Decimal(1), mode="paper")
        )


def test_tensor_without_a_key_is_disabled():
    with pytest.raises(NftDisabled):
        make_market("tensor", FakeHttpTransport(), env={})
    market = TensorMarket(FakeHttpTransport())
    with pytest.raises(NftDisabled):
        market.collection("madlads")


def test_market_takes_its_key_from_env_by_name_from_config():
    market = make_market("magiceden", FakeHttpTransport(), env={"MAGICEDEN_API_KEY": "k"})
    assert market.headers()["Authorization"].endswith("k")


# -- ключ OpenSea живёт 7 дней (research §6) ---------------------------------------------


def test_opensea_key_is_refreshed_before_it_expires():
    transport = FakeHttpTransport()
    transport.route("POST", "/auth/keys", {"api_key": "fresh-key"})
    key = OpenSeaKey(transport, key="old-key", issued_at=NOW - timedelta(days=6, hours=20))
    assert key.expired(now=NOW), "ключу осталось меньше запаса — считаем истёкшим"
    assert key.value(now=NOW) == "fresh-key"
    assert key.state.refreshed == 1
    # свежий ключ не обновляется повторно
    assert key.value(now=NOW + timedelta(hours=1)) == "fresh-key"
    assert key.state.refreshed == 1


def test_opensea_key_without_refresh_keeps_the_old_one_instead_of_going_blank():
    transport = FakeHttpTransport(offline=True)
    key = OpenSeaKey(transport, key="old-key", issued_at=NOW - timedelta(days=8))
    assert key.value(now=NOW) == "old-key"


def test_opensea_key_from_env_without_a_known_issue_date_is_trusted():
    key = OpenSeaKey(FakeHttpTransport(), env={"OPENSEA_API_KEY": "env-key"})
    assert key.value(now=NOW) == "env-key" and not key.expired(now=NOW)


# -- парсер календаря (История 74) ---------------------------------------------------------


def test_calendar_parser_reads_schema_org_events():
    mints = parse_ld_events(PAGE, source="nftcalendar", chain="ethereum")
    assert len(mints) == 1
    mint = mints[0]
    assert mint.collection == "Hype Apes"
    assert mint.starts_at == datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
    assert mint.price == Decimal("0.05") and mint.creator == "alice"
    assert mint.meta["url"].startswith("https://nftcalendar.io")


def test_calendar_source_reports_degraded_when_markup_did_not_parse():
    transport = FakeHttpTransport()
    transport.route("GET", "nftcalendar.io", "<html>вёрстка поменялась</html>")
    calendar = HtmlCalendar(transport, id="nftcalendar")
    assert calendar.mints() == []
    # пустая лента не должна выглядеть как «минтов нет»
    assert calendar.health().status == "degraded"


def test_calendar_source_survives_a_dead_site():
    calendar = HtmlCalendar(FakeHttpTransport(offline=True), id="nftcalendar")
    assert calendar.mints() == []
    assert calendar.health().status == "degraded"
