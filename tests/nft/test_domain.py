"""Трекер, создатели, лента минтов, allowlist, лестница продаж, неликвид, издержки."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import NftMint
from lab.feeds.nft import CollectionHistory, FakeNftMarket, LaunchpadSlot, NftSale
from lab.nft import (
    AllowlistOpportunity,
    AllowlistQueue,
    CollectionStore,
    CollectionTracker,
    NftPosition,
    UpcomingFeed,
    breakdown,
    creator_score,
    illiquid_flag,
    nft_costs,
    rank_creators,
    round_trip,
    sell_plan,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _market(name="fake_nft"):
    return FakeNftMarket(market=name)


# -- трекер (История 72) ----------------------------------------------------------------


def test_tracker_writes_floor_history_and_write_is_idempotent(tmp_path):
    market = _market()
    market.seed_floor("degods", Decimal("12.5"), volume_24h=Decimal(1000), holders=900)
    store = CollectionStore(tmp_path)
    tracker = CollectionTracker([market], store=store)

    first = tracker.track("degods", now=NOW)
    assert first.floor == Decimal("12.5")

    tracker.track("degods", now=NOW)  # тот же момент — повтор опроса
    history = tracker.history("degods")
    assert len(history) == 1, "повторный снимок того же момента удвоил историю"

    tracker.track("degods", now=NOW + timedelta(hours=1))
    assert [s.ts for s in tracker.history("degods")] == [NOW, NOW + timedelta(hours=1)]
    assert tracker.history("degods")[0].floor == Decimal("12.5")
    assert tracker.tracked() == ["degods"]


def test_tracker_takes_lowest_floor_across_markets(tmp_path):
    cheap, rich = _market("magiceden"), _market("opensea")
    cheap.seed_floor("degods", Decimal("9"))
    rich.seed_floor("degods", Decimal("11"))
    tracker = CollectionTracker([cheap, rich], store=CollectionStore(tmp_path))
    assert tracker.floor("degods") == Decimal("9")
    result = tracker.track("degods", now=NOW)
    assert {s.market for s in result.snapshots} == {"magiceden", "opensea"}


def test_tracker_survives_a_broken_market(tmp_path):
    class Broken(FakeNftMarket):
        def collection(self, collection):
            raise RuntimeError("площадка легла")

    ok = _market("magiceden")
    ok.seed_floor("degods", Decimal("9"))
    tracker = CollectionTracker([Broken(market="opensea"), ok], store=CollectionStore(tmp_path))
    result = tracker.track("degods", now=NOW)
    assert result.ok and "opensea" in result.errors


def test_tracker_counts_sales_rate_in_window(tmp_path):
    market = _market()
    market.seed_sales(
        "degods",
        [
            NftSale(collection="degods", token_id=str(i), price=Decimal(1),
                    ts=NOW - timedelta(minutes=i))
            for i in range(10)
        ],
    )
    tracker = CollectionTracker([market], store=CollectionStore(tmp_path))
    # 5 продаж за 5 минут окна → 1 продажа в минуту
    assert tracker.sales_rate("degods", window_min=5, now=NOW) == Decimal(1)


# -- создатели (История 73) -------------------------------------------------------------


def _history(slug, creator, *, mint, f1=None, f7=None, f30=None, days_ago=0):
    return CollectionHistory(
        collection=slug,
        creator=creator,
        minted_at=NOW - timedelta(days=days_ago),
        mint_price=Decimal(mint),
        floor_1d=None if f1 is None else Decimal(f1),
        floor_7d=None if f7 is None else Decimal(f7),
        floor_30d=None if f30 is None else Decimal(f30),
    )


def test_creator_score_counts_successful_collections():
    histories = [
        _history("a", "alice", mint=1, f1="1.5", f7="3", f30="4"),
        _history("b", "alice", mint=2, f1="1", f7="6", f30="1"),
        _history("c", "alice", mint=1, f1="0.2", f7="0.5", f30="0.1"),
        _history("d", "bob", mint=1, f1="0.1", f7="0.2", f30="0.1"),
    ]
    alice = creator_score("alice", histories, now=NOW)
    assert alice.collections == 3
    assert alice.successful == 2  # флор через 7 дней ≥ цены минта у двух из трёх
    assert alice.success_rate_pct == Decimal(200) / Decimal(3)
    assert alice.ratio(7) == Decimal(3)  # медиана 0.5, 3, 6
    # score = 0.5*доля (66.67%) + 0.5*медиана/3 (=1) → 0.3333… + 0.5
    assert alice.score == pytest.approx(Decimal("0.8333"), abs=Decimal("0.001"))

    bob = creator_score("bob", histories, now=NOW)
    assert bob.successful == 0 and bob.score < alice.score
    assert [s.creator for s in rank_creators(histories, now=NOW)] == ["alice", "bob"]


def test_creator_without_history_gets_no_score():
    score = creator_score("nobody", [], now=NOW)
    assert score.collections == 0 and score.score == Decimal(0)
    assert "истории" in score.detail


def test_old_success_weighs_less_than_fresh_one():
    fresh = creator_score("f", [_history("x", "f", mint=1, f7="2"),
                                _history("y", "f", mint=1, f7="0.5")], now=NOW)
    stale = creator_score("s", [_history("x", "s", mint=1, f7="2", days_ago=720),
                                _history("y", "s", mint=1, f7="0.5")], now=NOW)
    assert fresh.success_rate_pct == stale.success_rate_pct == Decimal(50)
    assert stale.weighted_success_rate_pct < fresh.weighted_success_rate_pct


# -- лента минтов (Истории 74, 74a) ------------------------------------------------------


class _Calendar:
    def __init__(self, id, mints):
        self.id = id
        self._mints = mints

    def mints(self):
        return self._mints

    def health(self):  # pragma: no cover — в этом тесте не спрашивается
        raise NotImplementedError


class _Mentions:
    def mentions(self, collection, *, window_h):
        return {"hype": (10, 40)}.get(collection, (0, 0))


def test_upcoming_merges_sources_and_ranks_by_attention():
    starts = NOW + timedelta(days=2)
    launchpad = _Calendar(
        "magiceden_launchpad",
        [NftMint(collection="hype", chain="solana", market="magiceden", starts_at=starts,
                 price=Decimal(1), supply=1000, creator="alice")],
    )
    calendar = _Calendar(
        "nftcalendar",
        [
            NftMint(collection="hype", chain="solana", market="nftcalendar"),
            NftMint(collection="quiet", chain="solana", market="nftcalendar", starts_at=starts),
        ],
    )
    histories = [_history("a", "alice", mint=1, f7="3")]
    feed = UpcomingFeed(
        [launchpad, calendar],
        histories=histories,
        mentions=_Mentions(),
        slots={"hype": LaunchpadSlot("hype", minted=800, supply=1000,
                                     allowlist_seats=100, allowlist_demand=1000)},
    )
    lenta = feed.upcoming(now=NOW, min_score=Decimal(0))
    assert [c.collection for c in lenta] == ["hype", "quiet"]

    hype = lenta[0]
    assert set(hype.sources) == {"magiceden_launchpad", "nftcalendar"}
    assert hype.mint.price == Decimal(1) and hype.mint.supply == 1000
    assert hype.attention.value > lenta[1].attention.value
    card = hype.card()
    assert card["attention_note"], "в карточке должно быть видно, что веса — гипотеза"
    assert card["attention_components"]["launchpad_fill"] == "0.8"


def test_upcoming_drops_past_mints_and_low_attention():
    past = _Calendar("c", [NftMint(collection="old", chain="solana", market="c",
                                   starts_at=NOW - timedelta(hours=1))])
    quiet = _Calendar("d", [NftMint(collection="quiet", chain="solana", market="d",
                                    starts_at=NOW + timedelta(days=1))])
    feed = UpcomingFeed([past, quiet])
    assert feed.upcoming(now=NOW) == []  # 0 внимания — ниже min_score_to_watch


# -- allowlist (История 74b) --------------------------------------------------------------


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_card(self, kind, payload):
        self.sent.append((kind, payload))
        return f"msg-{len(self.sent)}"


@pytest.mark.asyncio
async def test_allowlist_queue_sends_cards_marks_seat_and_schedules_mint():
    bot = _Bot()
    queue = AllowlistQueue(bot=bot, clock=lambda: NOW)
    item = queue.add(
        AllowlistOpportunity(
            collection="hype",
            chain="solana",
            market="magiceden",
            kind="raffle",
            deadline=NOW + timedelta(days=1),
            mint_at=NOW + timedelta(days=2),
            url="https://example/raffle",
        )
    )
    sent = await queue.announce(now=NOW)
    assert sent and bot.sent[0][0] == "allowlist"
    payload = bot.sent[0][1]
    assert payload["request_id"] == item.id and payload["collection"] == "hype"
    assert "дедлайн" in payload["reason"]

    assert await queue.announce(now=NOW) == [], "карточка не должна уходить дважды"

    assert queue.due(now=NOW + timedelta(days=2)) == []  # места ещё нет
    queue.decide(item.id, "allow", now=NOW)
    assert queue.get(item.id).has_seat
    assert queue.due(now=NOW + timedelta(days=2)) == [item]
    assert queue.due(now=NOW) == []  # минт ещё не открылся
    assert queue.schedule(now=NOW)[0][0] == NOW + timedelta(days=2)


def test_allowlist_expires_by_deadline():
    queue = AllowlistQueue(clock=lambda: NOW)
    item = queue.add(AllowlistOpportunity(collection="hype", deadline=NOW - timedelta(hours=1)))
    assert queue.open(now=NOW) == []
    assert queue.get(item.id).status == "expired"


# -- лестница продаж и удержание (История 78, G06) ----------------------------------------


def _position(entry="10", qty="4", opened_days_ago=0, sold="0"):
    return NftPosition(
        collection="hype",
        token_id="1",
        qty=Decimal(qty),
        entry_price=Decimal(entry),
        sold_qty=Decimal(sold),
        opened_at=NOW - timedelta(days=opened_days_ago),
    )


def test_sell_plan_sells_part_on_target_and_keeps_hold_share():
    position = _position()
    assert sell_plan(position, Decimal("12")) == []  # +20% — до первой цели далеко

    orders = sell_plan(position, Decimal("15"))  # +50% — первая цель
    assert len(orders) == 1 and orders[0].qty == Decimal(2)  # 50% от 4 предметов
    assert "держим" in orders[0].reason

    position.sold_qty = Decimal(2)
    orders = sell_plan(position, Decimal("25"))  # +150% — вторая цель, ещё 25%
    assert orders[0].qty == Decimal(1)

    position.sold_qty = Decimal(3)
    assert sell_plan(position, Decimal("100")) == [], "остаток на удержании не продаётся"


def test_stop_loss_sells_only_the_sellable_part():
    position = _position()
    orders = sell_plan(position, Decimal("5"))  # −50%
    assert orders[0].kind == "stop" and orders[0].qty == Decimal(3)  # 75%, 25% на удержании


# -- неликвид (История 80) -----------------------------------------------------------------


def test_illiquid_flag_appears_with_age_and_markdown_rule():
    fresh = _position(opened_days_ago=3)
    assert illiquid_flag(fresh, now=NOW, last_sale_at=NOW - timedelta(days=30)) is None

    old = _position(opened_days_ago=15)
    assert illiquid_flag(old, now=NOW, last_sale_at=NOW - timedelta(days=1)) is None

    flag = illiquid_flag(old, now=NOW, last_sale_at=NOW - timedelta(days=10))
    assert flag is not None and int(flag.age_days) == 15
    assert flag.steps == 5  # 15 дней / 3 дня на шаг
    assert flag.suggested_price < Decimal(10) and flag.suggested_price >= Decimal(5)
    assert flag.floor_price == Decimal(5) and "снижаем" in flag.reason


def test_markdown_never_goes_below_floor():
    ancient = _position(opened_days_ago=400)
    flag = illiquid_flag(ancient, now=NOW, last_sale_at=NOW - timedelta(days=100))
    assert flag.suggested_price == flag.floor_price and flag.at_floor


# -- издержки по компонентам (История 79) ---------------------------------------------------


def test_costs_are_broken_into_royalty_fee_and_gas():
    sell = breakdown(Decimal(100), market="opensea", side="sell")
    assert sell.royalty == Decimal(5)  # 5% роялти
    assert sell.marketplace_fee == Decimal("2.5")  # тариф OpenSea
    assert sell.gas == Decimal(4)
    assert sell.total == Decimal("11.5")

    buy = breakdown(Decimal(100), market="opensea", side="buy")
    assert buy.royalty == Decimal(0) and buy.marketplace_fee == Decimal(0)
    assert buy.gas == Decimal(4), "покупатель платит газ"

    costs = nft_costs(Decimal(100), market="opensea", side="sell")
    assert costs.royalty == Decimal(5) and costs.fee == Decimal("2.5")

    circle = round_trip(Decimal(100), Decimal(200), market="opensea")
    assert circle.royalty == Decimal(10) and circle.gas == Decimal(8)
