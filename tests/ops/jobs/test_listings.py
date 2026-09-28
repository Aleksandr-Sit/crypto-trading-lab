"""Лента листингов Binance → состав шорта листингов (шаг 5 плана трейдеров, 28.09.2026).

Главное, что здесь проверяется, — что лента применяет ТО ЖЕ правило, на котором мерили
эффект: день листинга — первая дневная свеча спотовой пары, торгуемо — если перп запущен
не позже первого полного дня. Отступи лента от него, вперёд проверялось бы другое правило,
чем то, что прошло порог.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta

import pytest

from lab.core.registry import Registry
from lab.db.models import StrategyRow, SystemFlagRow
from lab.feeds.cex.listings import BinanceListings
from lab.feeds.chains.fake import FakeHttpTransport
from lab.ops.jobs.listings import SEEN_FLAG, fill_data, listings_job, sync_listings
from lab.strategies.registry import build

LISTING_ID = "cex-perp-paper-listing-fade-short"
ROTATION_ID = "cex-spot-external-rotation-gold-btc"
FLOOR = date(2026, 7, 17)  # последний листинг состава — как у AERO на 28.09.2026
OLD = "OLDC/USDT:USDT"
DAY = timedelta(days=1)


def ms(d: date) -> int:
    return int(datetime.combine(d, time(), tzinfo=UTC).timestamp() * 1000)


def evening(d: date) -> datetime:
    """Время прогона ленты: 03:30 по Самаре = 23:30 UTC дня `d`."""
    return datetime.combine(d, time(23, 30), tzinfo=UTC)


class Exchange:
    """Биржа для ленты: перпы с датами запуска, спотовые пары и их первые дневные свечи."""

    def __init__(self) -> None:
        self.perps: dict[str, date] = {}
        self.spot: dict[str, date | None] = {}
        self.transport = FakeHttpTransport()
        self.transport.route("GET", "fapi/v1/exchangeInfo", self._perps)
        self.transport.route("GET", "api/v3/exchangeInfo", self._spot)
        self.transport.route("GET", "api/v3/klines", self._klines)

    def _perps(self, url, params, body):
        return {
            "symbols": [
                {
                    "baseAsset": base,
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "onboardDate": ms(day),
                }
                for base, day in self.perps.items()
            ]
        }

    def _spot(self, url, params, body):
        def pair(base: str, quote: str = "USDT", status: str = "TRADING") -> dict:
            return {
                "baseAsset": base,
                "quoteAsset": quote,
                "symbol": f"{base}{quote}",
                "status": status,
            }

        # Снятая пара и пара к другой котировке ленту не интересуют.
        extra = [pair("DEAD", status="BREAK"), pair("HYPE", quote="BTC")]
        return {"symbols": [pair(base) for base in self.spot] + extra}

    def _klines(self, url, params, body):
        day = self.spot.get(params["symbol"].removesuffix("USDT"))
        return [] if day is None else [[ms(day), "1", "1", "1", "1", "1", ms(day) + 86_399_999]]

    def source(self) -> BinanceListings:
        return BinanceListings(self.transport)

    def klines_asked(self) -> int:
        return sum(1 for call in self.transport.calls if "klines" in call.url)


class Scope:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *a):
        return False


def listing_manifest(slug_suffix: str = "", **params):
    """Запись шорта листингов с составом из одной монеты.

    Реестр отсекает дубли по отпечатку ПРАВИЛ, а не по имени: вторую запись с теми же
    параметрами он не примет, — копии нужен свой параметр (`hold_days=14`).
    """
    manifest = build(LISTING_ID).manifest
    return manifest.model_copy(
        update={
            "slug": manifest.slug + slug_suffix,
            "instruments": [OLD],
            "params": {
                **manifest.params,
                "listing_dates": json.dumps({OLD: FLOOR.isoformat()}, indent=1),
                **params,
            },
        }
    )


@pytest.fixture
def record(session):
    return Registry(session).add(listing_manifest())


def composition(session, strategy_id: str) -> tuple[list[str], dict[str, str]]:
    row = session.get(StrategyRow, strategy_id)
    return list(row.instruments), json.loads(row.params_json["listing_dates"])


def test_new_listing_with_perp_goes_in_with_its_listing_day(session, record):
    """В `listing_dates` — день ЛИСТИНГА: стратегия сама отсчитает первый полный день.

    Ровно на этом месте 28.09.2026 нашлась ошибка состава: туда записали первый полный
    день, и стратегия входила на сутки позже карточки.
    """
    ex = Exchange()
    ex.spot = {"OLDC": FLOOR, "HYPE": date(2026, 9, 24)}
    ex.perps = {"OLDC": date(2026, 7, 1), "HYPE": date(2025, 5, 30)}

    report = sync_listings(session, ex.source(), now=evening(date(2026, 9, 24)))

    instruments, dates = composition(session, record.id)
    assert instruments == [OLD, "HYPE/USDT:USDT"]
    assert dates["HYPE/USDT:USDT"] == "2026-09-24"
    assert [a.instrument for a in report.added] == ["HYPE/USDT:USDT"]
    added = report.added[0]
    assert added.entry_day == date(2026, 9, 25)
    assert not added.entry_passed(evening(date(2026, 9, 24)))
    assert added.strategies == (record.id,)


def test_perp_by_the_first_full_day_counts_a_day_later_does_not(session, record):
    listed = date(2026, 10, 5)
    ex = Exchange()
    ex.spot = {"EARLY": listed, "LATE": listed}
    ex.perps = {"EARLY": listed + DAY, "LATE": listed + 2 * DAY}

    report = sync_listings(session, ex.source(), now=evening(listed + 2 * DAY))

    assert [a.base for a in report.added] == ["EARLY"]
    assert report.rejected == 1
    assert "LATE/USDT:USDT" not in composition(session, record.id)[0]


def test_pair_waits_for_its_perp_until_the_first_full_day_is_over(session, record):
    """Binance нередко запускает перп через несколько часов после спота."""
    listed = date(2026, 10, 5)
    ex = Exchange()
    ex.spot = {"COIN": listed}

    first = sync_listings(session, ex.source(), now=evening(listed))
    assert first.pending == ["COIN"] and first.added == []

    ex.perps = {"COIN": listed + DAY}
    second = sync_listings(session, ex.source(), now=evening(listed + DAY))

    assert [a.base for a in second.added] == ["COIN"]
    # День листинга запомнен с первого раза — вторую свечу не спрашиваем.
    assert ex.klines_asked() == 1


def test_tokenized_stock_without_perp_is_dropped_by_the_same_rule(session, record):
    """`COINB` и подобные — 20 из 23 новых пар июля–сентября 2026: перпа у них нет."""
    listed = date(2026, 9, 23)
    ex = Exchange()
    ex.spot = {"COINB": listed}

    for day in range(3):
        sync_listings(session, ex.source(), now=evening(listed + day * DAY))
    report = sync_listings(session, ex.source(), now=evening(listed + 5 * DAY))

    assert report.added == [] and report.pending == []
    assert composition(session, record.id)[0] == [OLD]
    assert ex.klines_asked() == 1


def test_pairs_up_to_the_floor_are_left_to_the_research(session, record):
    """До последней даты состава отбор уже сделало исследование — по архиву, с умершими.

    Второй прогон не спрашивает у биржи ничего: итог по каждой паре запомнен.
    """
    ex = Exchange()
    ex.spot = {"OLDC": FLOOR, "SAMEDAY": FLOOR, "PREV": FLOOR - 30 * DAY}
    ex.perps = {base: FLOOR - 60 * DAY for base in ex.spot}

    first = sync_listings(session, ex.source(), now=evening(date(2026, 9, 28)))
    second = sync_listings(session, ex.source(), now=evening(date(2026, 9, 29)))

    assert first.added == [] and first.rejected == 3
    assert second.asked == 0 and second.rejected == 0
    assert composition(session, record.id)[0] == [OLD]


def test_floor_does_not_move_after_additions(session, record):
    """Пара, увиденная с опозданием, не выпадает из-за того, что состав уже подрос."""
    ex = Exchange()
    ex.spot = {"HYPE": date(2026, 9, 24)}
    ex.perps = {"HYPE": date(2025, 5, 30), "LATE": date(2026, 9, 1)}
    sync_listings(session, ex.source(), now=evening(date(2026, 9, 24)))

    ex.spot["LATE"] = date(2026, 9, 20)  # старше HYPE, но новее границы состава
    report = sync_listings(session, ex.source(), now=evening(date(2026, 9, 25)))

    assert [a.base for a in report.added] == ["LATE"]
    flag = session.get(SystemFlagRow, SEEN_FLAG)
    assert flag.value["floor"] == FLOOR.isoformat()


def test_hieroglyph_base_goes_in_by_its_ccxt_name(session, record):
    """`牛来` — настоящий листинг 09.09.2026; символ ccxt у Binance — `<baseAsset>/USDT:USDT`."""
    ex = Exchange()
    ex.spot = {"牛来": date(2026, 9, 9)}
    ex.perps = {"牛来": date(2026, 8, 30)}

    sync_listings(session, ex.source(), now=evening(date(2026, 9, 28)))

    instruments, dates = composition(session, record.id)
    assert "牛来/USDT:USDT" in instruments
    assert dates["牛来/USDT:USDT"] == "2026-09-09"


def test_other_and_retired_records_are_untouched(session, record):
    rotation = Registry(session).add(build(ROTATION_ID).manifest)
    retired = Registry(session).add(listing_manifest("-old", hold_days=14))
    session.get(StrategyRow, retired.id).status = "retired"
    session.flush()
    before_rotation = list(session.get(StrategyRow, rotation.id).instruments)
    before_retired = dict(session.get(StrategyRow, retired.id).params_json)

    ex = Exchange()
    ex.spot = {"HYPE": date(2026, 9, 24)}
    ex.perps = {"HYPE": date(2025, 5, 30)}
    report = sync_listings(session, ex.source(), now=evening(date(2026, 9, 24)))

    assert report.added[0].strategies == (record.id,)
    assert list(session.get(StrategyRow, rotation.id).instruments) == before_rotation
    assert session.get(StrategyRow, retired.id).params_json == before_retired


def test_no_listing_strategy_means_no_calls(session):
    ex = Exchange()
    report = sync_listings(session, ex.source(), now=evening(date(2026, 9, 28)))

    assert report.note and ex.transport.calls == []


def test_exchange_down_changes_nothing_and_alerts(session, record):
    ex = Exchange()
    ex.transport.offline = True
    alerts: list[dict] = []

    job = listings_job(
        Scope(session),
        source=ex.source(),
        alert=lambda kind, payload: alerts.append(payload),
        clock=lambda: evening(date(2026, 9, 28)),
    )
    report = job.func()

    assert report.errors and alerts and alerts[0]["service"] == "listings"
    assert session.get(SystemFlagRow, SEEN_FLAG) is None
    assert composition(session, record.id)[0] == [OLD]


def test_job_fills_data_and_names_the_coin_in_the_alert(session, record, monkeypatch):
    """Новый инструмент получает свечи и ставки от дня листинга с запасом, а владелец —
    карточку с датой, когда стратегия примет решение."""
    from lab.data.backfill_cex import SymbolResult

    asked: list[tuple[str, str, int]] = []

    def fake_candles(store, venue, symbols, tf, days):
        asked.append(("свечи", symbols[0], days))
        return [SymbolResult(venue=venue, instrument=s, tf=tf, rows_written=days) for s in symbols]

    def fake_funding(store, venue, symbols, days):
        asked.append(("фандинг", symbols[0], days))
        return [(s, 6, None) for s in symbols]

    monkeypatch.setattr("lab.data.backfill_cex.backfill_venue", fake_candles)
    monkeypatch.setattr("lab.data.backfill_cex.backfill_funding", fake_funding)
    ex = Exchange()
    ex.spot = {"HYPE": date(2026, 9, 24)}
    ex.perps = {"HYPE": date(2025, 5, 30)}
    alerts: list[dict] = []
    now = evening(date(2026, 9, 24))

    report = listings_job(
        Scope(session),
        source=ex.source(),
        root="unused",
        alert=lambda kind, payload: alerts.append(payload),
        clock=lambda: now,
    ).func()

    assert report.errors == []
    # От дня листинга плюс три дня запаса: у перпа, запущенного раньше спота, ряд длиннее.
    assert asked == [("свечи", "HYPE/USDT:USDT", 3), ("фандинг", "HYPE/USDT:USDT", 3)]
    assert "HYPE" in alerts[0]["detail"] and "25.09" in alerts[0]["detail"]


def test_fill_data_reports_backfill_errors(monkeypatch):
    from lab.data.backfill_cex import SymbolResult
    from lab.ops.jobs.listings import Added

    monkeypatch.setattr(
        "lab.data.backfill_cex.backfill_venue",
        lambda store, venue, symbols, tf, days: [
            SymbolResult(venue=venue, instrument=s, tf=tf, error="429") for s in symbols
        ],
    )
    monkeypatch.setattr(
        "lab.data.backfill_cex.backfill_funding",
        lambda store, venue, symbols, days: [(s, 0, "timeout") for s in symbols],
    )
    added = Added("HYPE", "HYPE/USDT:USDT", date(2026, 9, 4), date(2025, 5, 30), ("x",))

    errors = fill_data([added], root="unused", now=evening(date(2026, 9, 28)))

    assert errors == ["свечи HYPE/USDT:USDT 1d: 429", "фандинг HYPE/USDT:USDT: timeout"]
