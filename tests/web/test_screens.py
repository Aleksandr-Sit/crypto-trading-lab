"""Экраны с данными (R13.2, R11.1, R12.1, R12.2, R34i, A02, A03): 200 с фикстурами,
карточка с порогом/CI95/SVG, `incomplete` и «недостаточно данных», кладбище, CSV, HTMX-фрагмент."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import Costs, Fill, OrderIntent, Signal, StopSpec, StrategyManifest
from lab.core.journal import Journal
from lab.core.ladder import Ladder, default_threshold_fn
from lab.core.measure import ClosedTrade, run
from lab.core.registry import Registry

T0 = datetime(2026, 5, 1, tzinfo=UTC)
WINDOW = (T0, T0 + timedelta(days=30))


def _manifest(slug: str, branch: str = "cex-spot", **kw) -> StrategyManifest:
    return StrategyManifest(
        slug=slug,
        branch=branch,
        venue="bybit",
        source_kind="test",
        instruments=["BTC/USDT"],
        timeframe="1h",
        params={"tag": slug},  # отпечаток дубликата не включает slug
        stop=StopSpec(daily_pct=Decimal(5)),
        **kw,
    )


def _trade(i: int, pnl: str) -> ClosedTrade:
    return ClosedTrade(
        instrument="BTC/USDT",
        side="long",
        qty=Decimal(1),
        entry_price=Decimal(100),
        exit_price=Decimal(100) + Decimal(pnl),
        opened_at=T0 + timedelta(hours=6 * i),
        closed_at=T0 + timedelta(hours=6 * i + 2),
        pnl_gross=Decimal(pnl),
        costs=Costs(fee=Decimal("0.1")),
    )


def _forward(session, strategy_id: str, trades: list[ClosedTrade], **kw):
    return run(
        strategy_id,
        "forward",
        WINDOW,
        trades=trades,
        benchmark=Decimal(0),
        capital=Decimal(1000),
        session=session,
        **kw,
    )


def _journal_round_trip(session, strategy_id: str) -> None:
    j = Journal(session)
    s = j.record_signal(
        Signal(
            strategy_id=strategy_id,
            decided_at=T0,
            instrument="BTC/USDT",
            side="buy",
            size=Decimal(1),
            price_ref=Decimal(100),
            inputs_hash="abc",
            ttl_s=600,
        )
    )
    for side, coid, price, ts in (
        ("buy", "c1", "100", T0 + timedelta(minutes=1)),
        ("sell", "c2", "110", T0 + timedelta(hours=1)),
    ):
        o = j.record_order(
            OrderIntent(
                strategy_id=strategy_id,
                venue="bybit",
                instrument="BTC/USDT",
                side=side,
                qty=Decimal(1),
                order_type="market",
                mode="paper",
                signal_id=s.id,
                client_order_id=coid,
            ),
            order_id=f"o-{coid}",
        )
        j.record_fill(
            Fill(
                id=f"f-{coid}",
                order_id=o.id,
                price=Decimal(price),
                qty=Decimal(1),
                fee=Decimal("0.1"),
                fee_asset="USDT",
                ts=ts,
            )
        )


@pytest.fixture
def world(session):
    """Четыре стратегии: прошла порог (30 сделок), мало сделок, `incomplete`, не прошла."""
    reg = Registry(session)
    ladder = Ladder(session, threshold=default_threshold_fn())
    passed = reg.add(_manifest("winner"))
    m = _forward(session, passed.id, [_trade(i, "5") for i in range(30)])
    ladder.evaluate(passed.id, m.metrics)  # порог пройден → ступень выше, статус measuring
    _journal_round_trip(session, passed.id)

    few = reg.add(_manifest("few", branch="meme", can_backtest=False))
    _forward(session, few.id, [_trade(i, "2") for i in range(3)])

    broken = reg.add(_manifest("broken"))

    def dead_source(instrument, tf, from_ts, to_ts):
        raise ConnectionError("feed down")

    class Never:
        manifest = _manifest("broken")

        def on_bar(self, bar):
            return []

        def on_event(self, evt):
            return []

    run(broken.id, "backtest", WINDOW, strategy=Never(), source=dead_source, session=session)

    loser = reg.add(_manifest("loser"))
    m = _forward(session, loser.id, [_trade(i, "-3") for i in range(30)])
    ladder.evaluate(loser.id, m.metrics)
    retired = reg.add(_manifest("old"))
    reg.retire(retired.id, "дубликат winner")
    reg.enqueue_candidate("wallet", "0xabc")
    session.flush()
    return {"passed": passed.id, "few": few.id, "broken": broken.id, "loser": loser.id}


def test_every_screen_is_200_with_data(client, world):
    for path in ("/", "/strategies", "/feeds", "/queue", "/graveyard"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "undefined" not in r.text and "None" not in r.text, path


def test_strategies_table_has_same_metric_columns_for_all_branches(client, world):
    body = client.get("/strategies").text
    for column in ("Сделок", "EV/сделку, USD", "Макс. просадка, %", "vs BTC, п.п.", "Sharpe"):
        assert body.count(column) == 1, column
    assert world["passed"] in body and world["few"] in body
    assert "недостаточно данных: 3 из 30" in body
    # ветко-специфичные метрики в таблице не показываются
    assert "Brier" not in body and "copy" not in body.split("<tbody>")[1].split("</tbody>")[0]


def test_strategies_filters_and_sort_return_fragment_for_htmx(client, world):
    full = client.get("/strategies?branch=meme")
    assert world["few"] in full.text and world["passed"] not in full.text
    assert "<html" in full.text
    frag = client.get("/strategies?sort=net_pnl&dir=desc", headers={"HX-Request": "true"})
    assert frag.status_code == 200 and "<html" not in frag.text
    assert frag.text.index(world["passed"]) < frag.text.index(world["loser"])
    assert "Стратегий нет" not in frag.text


def test_card_shows_criteria_ci95_svg_and_trades(client, world):
    body = client.get(f"/strategies/{world['passed']}").text
    assert "<svg" in body and 'class="curve"' in body
    assert "CI95 [" in body  # A04: интервал рядом с EV
    for name in ("сделок", "EV на сделку", "макс. просадка, %", "vs BTC"):
        assert name in body
    assert "✓" in body and "пройден" in body
    assert "≥ 30" in body  # порог «значение / порог / ✓✗»
    assert "Метрики ветки" in body and "н/д" in body
    assert "110.00" in body  # сделка из журнала: выход по 110
    assert f"/strategies/{world['passed']}/trades.csv" in body


def test_forward_only_card_shows_no_backtest_and_counter(client, world):
    body = client.get(f"/strategies/{world['few']}").text
    assert "Бэктест: н/д — нет честной истории" in body
    assert "недостаточно данных: 3 из 30" in body
    assert "сделок, " in body and "дней" in body


def test_incomplete_measurement_card_names_reason_not_zeros(client, world):
    body = client.get(f"/strategies/{world['broken']}").text
    assert "Замер не завершён" in body and "feed down" in body
    assert "не завершён" in body
    assert "0.00" not in body.split("Метрики")[1].split("Журнал сделок")[0]


def test_unknown_strategy_is_404(client):
    assert client.get("/strategies/no-such-id").status_code == 404


def test_graveyard_lists_failed_and_retired_with_reason_filter(client, world):
    body = client.get("/graveyard").text
    assert world["loser"] in body and "порог не пройден" in body
    assert "cex-spot-test-old" in body and "дубликат winner" in body
    filtered = client.get("/graveyard?reason=дубликат", headers={"HX-Request": "true"}).text
    assert "cex-spot-test-old" in filtered and world["loser"] not in filtered
    nothing = client.get("/graveyard?reason=xyz").text
    assert "ничего нет" in nothing


def test_csv_export_uses_journal(client, world):
    r = client.get(f"/strategies/{world['passed']}/trades.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert len(lines) == 2 and world["passed"] in lines[1] and "110" in lines[1]
    assert client.get("/trades.csv").status_code == 200


def test_index_summary_counts_week_and_decisions(client, world):
    body = client.get("/").text
    assert "Прошли: 1" in body and "Упали: 1" in body
    assert "Кандидатов в очереди: 1" in body
