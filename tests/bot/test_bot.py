"""Бот Trader: только админ, карточки, кнопки сигналов, /halt /resume /promote."""

from __future__ import annotations

from datetime import timedelta

from lab.bot import CARD_KINDS, render_card
from lab.contracts import Rung, SignalOutcome, Status
from lab.core.registry import Registry
from lab.db.models import StrategyRow
from tests.bot.conftest import ADMIN, T0, make_signal

STRANGER = 777


# -- allowlist (R13.6) ---------------------------------------------------------------------


async def test_foreign_user_is_ignored_silently(bot, tg) -> None:
    assert await bot.handle_command(STRANGER, "/status") is None
    assert await bot.handle_command(STRANGER, "/halt") is None
    assert await bot.handle_callback(STRANGER, "confirm:whatever") is None
    assert tg.sent == [] and tg.edited == []


async def test_admin_gets_status(bot, strategy) -> None:
    reply = await bot.handle_command(ADMIN, "/status")
    assert "стоп всё: нет" in reply.text
    assert strategy.id in reply.text


# -- карточки ------------------------------------------------------------------------------


def test_every_card_kind_renders_in_russian() -> None:
    assert set(CARD_KINDS) == {
        "signal", "confirm", "fill", "transition", "candidate", "rebalance", "allowlist", "alert"
    }
    samples = {
        "signal": dict(signal_id="s1", strategy_id="x", rung="signal", instrument="BTC-USDT",
                       side="buy", size="0.01", price_ref="60000", venue="bybit", ttl_s=600),
        "confirm": dict(token="t1", action="halt", text="Остановить всё?"),
        "fill": dict(strategy_id="x", instrument="BTC-USDT", side="buy", qty="0.01",
                     price="60010", venue="bybit", costs="1.2"),
        "transition": dict(strategy_id="x", from_rung="paper", to_rung="micro",
                           status="measuring", reason="порог пройден", by="system"),
        "candidate": dict(candidate_id=7, kind="github", ref="octo/repo"),
        "rebalance": dict(proposal_id=3, from_branch="meme", to_branch="cex-spot",
                          amount_usd="120"),
        "allowlist": dict(request_id=9, address="0xabc", chain="evm", reason="лидер OKX"),
        "alert": dict(service="worker", silent_for_s=300, detail="heartbeat устарел"),
    }
    for kind in CARD_KINDS:
        card = render_card(kind, samples[kind])
        assert any("а" <= ch <= "я" or "А" <= ch <= "Я" for ch in card.text), kind


def test_signal_card_buttons_depend_on_rung() -> None:
    base = dict(signal_id="s1", strategy_id="x", instrument="BTC-USDT", side="sell",
                size="1", price_ref="100", venue="okx", ttl_s=60)
    labels = lambda rung: [  # noqa: E731
        b.text for row in render_card("signal", {**base, "rung": rung}).buttons for b in row
    ]
    assert labels("signal") == ["Исполнил", "Пропустил"]
    assert labels("semi") == ["Подтвердить", "Отклонить"]
    assert labels("auto") == []


# -- кнопки сигналов → journal (R02, R03, R03.1) -------------------------------------------


async def test_signal_button_sets_outcome_via_journal(bot, tg, journal, strategy) -> None:
    rec = journal.record_signal(make_signal(strategy.id))
    await bot.send_card("signal", dict(signal_id=rec.id, strategy_id=strategy.id, rung="signal",
                                       instrument="BTC-USDT", side="buy", size="0.01",
                                       price_ref="60000", venue="bybit", ttl_s=600))
    assert len(tg.sent) == 1 and tg.sent[0].ref == rec.id
    button = tg.sent[0].buttons[0][0]
    assert await bot.handle_callback(STRANGER, button.data) is None
    assert journal.signal(rec.id).outcome == SignalOutcome.PENDING

    reply = await bot.handle_callback(ADMIN, button.data)
    assert journal.signal(rec.id).outcome == SignalOutcome.EXECUTED
    assert journal.signal(rec.id).outcome_at == T0
    assert "исполнен" in reply.answer.lower()
    assert tg.edited and tg.edited[-1][1].buttons == []  # кнопки сняты


async def test_semi_confirm_calls_executor_hook_and_marks_executed(
    session, tg, halt, clock, journal, strategy
) -> None:
    from lab.bot import TraderBot
    from lab.core.ladder import Ladder
    from tests.core.test_ladder import FakeThreshold

    executed: list[str] = []
    bot = TraderBot(
        session_factory=lambda: session, admin_id=ADMIN, transport=tg, clock=clock,
        ladder_factory=lambda s: Ladder(s, threshold=FakeThreshold(), halt=halt),
        on_confirm=lambda signal_id: executed.append(signal_id), close_sessions=False,
    )
    rec = journal.record_signal(make_signal(strategy.id))
    await bot.send_card("signal", dict(signal_id=rec.id, strategy_id=strategy.id, rung="semi",
                                       instrument="BTC-USDT", side="buy", size="0.01",
                                       price_ref="60000", venue="bybit", ttl_s=600))
    confirm, reject = tg.sent[0].buttons[0]
    await bot.handle_callback(ADMIN, confirm.data)
    assert executed == [rec.id]
    assert journal.signal(rec.id).outcome == SignalOutcome.EXECUTED


async def test_signal_timeout_expires_and_edits_message(
    bot, tg, journal, strategy, clock, session
) -> None:
    # таймаут действует только на ступенях signal/semi (R03.1); ttl — params.ttl_s манифеста
    session.get_one(StrategyRow, strategy.id).rung = "signal"
    session.flush()
    rec = journal.record_signal(make_signal(strategy.id, ttl_s=600))
    await bot.send_card("signal", dict(signal_id=rec.id, strategy_id=strategy.id, rung="signal",
                                       instrument="BTC-USDT", side="buy", size="0.01",
                                       price_ref="60000", venue="bybit", ttl_s=600))
    clock.set(T0 + timedelta(seconds=599))
    assert await bot.expire_pending() == []
    clock.set(T0 + timedelta(seconds=601))
    assert await bot.expire_pending() == [rec.id]
    assert journal.signal(rec.id).outcome == SignalOutcome.EXPIRED
    msg_id, edited = tg.edited[-1]
    assert msg_id == 1001 and "просрочен" in edited.text.lower() and edited.buttons == []


# -- /halt, /resume с двойным подтверждением (G04.1) ---------------------------------------


async def test_halt_and_resume_need_second_press(bot, halt) -> None:
    reply = await bot.handle_command(ADMIN, "/halt")
    assert not halt.is_halted()
    assert [b.text for b in reply.buttons[0]] == ["Подтвердить", "Отмена"]
    ok = await bot.handle_callback(ADMIN, reply.buttons[0][0].data)
    assert halt.is_halted() and "останов" in ok.answer.lower()

    reply = await bot.handle_command(ADMIN, "/resume")
    assert halt.is_halted()
    cancel = await bot.handle_callback(ADMIN, reply.buttons[0][1].data)
    assert halt.is_halted() and "отмен" in cancel.answer.lower()
    reply = await bot.handle_command(ADMIN, "/resume")
    await bot.handle_callback(ADMIN, reply.buttons[0][0].data)
    assert not halt.is_halted()


async def test_confirmation_token_is_single_use_and_expires(bot, halt, clock) -> None:
    reply = await bot.handle_command(ADMIN, "/halt")
    clock.set(T0 + timedelta(minutes=5))
    late = await bot.handle_callback(ADMIN, reply.buttons[0][0].data)
    assert not halt.is_halted() and "устарел" in late.answer.lower()


# -- /promote только semi → auto (R02.3) ---------------------------------------------------


async def test_promote_refuses_everything_but_semi_to_auto(bot, session, strategy) -> None:
    reply = await bot.handle_command(ADMIN, f"/promote {strategy.id}")
    assert "semi" in reply.text and "backtest" in reply.text
    assert Registry(session).get(strategy.id).rung == Rung.BACKTEST

    session.get_one(StrategyRow, strategy.id).rung = "semi"
    session.flush()
    reply = await bot.handle_command(ADMIN, f"/promote {strategy.id}")
    s = Registry(session).get(strategy.id)
    assert s.rung == Rung.AUTO and s.status == Status.MEASURING
    assert "auto" in reply.text

    reply = await bot.handle_command(ADMIN, "/promote nope")
    assert "не найдена" in reply.text


# -- утренний отчёт (R13) -----------------------------------------------------------------


async def test_morning_report_is_one_message_plus_pending_cards(
    bot, tg, journal, strategy, session
) -> None:
    session.get_one(StrategyRow, strategy.id).rung = "semi"
    session.flush()
    rec = journal.record_signal(make_signal(strategy.id))
    await bot.send_morning_report()
    assert [m.kind for m in tg.sent] == ["report", "signal"]
    report = tg.sent[0].text
    for piece in (
        "Утренний отчёт", "P&amp;L за сутки", "Лестница", "Ждут решения:</b> 1", "Источники", "н/д",
    ):
        assert piece in report
    card = tg.sent[1]
    assert card.ref == rec.id and [b.text for b in card.buttons[0]] == ["Подтвердить", "Отклонить"]


def test_bot_jobs_register_morning_report_at_nine_samara(bot) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from lab.config import load_schedule
    from lab.ops.scheduler import Scheduler

    sched = Scheduler(load_schedule())
    try:
        ids = [j.id for j in bot.jobs(sched)]
        assert "morning_report" in ids and "expire_signals" in ids and "outbox_flush" in ids
        samara = ZoneInfo("Europe/Samara")
        nxt = sched.next_run("morning_report", after=datetime(2026, 9, 5, 12, tzinfo=samara))
        assert nxt == datetime(2026, 9, 6, 9, 0, tzinfo=samara)
    finally:
        sched.shutdown()


# -- уведомление лестницы и watchdog ------------------------------------------------------


async def test_ladder_notify_and_watchdog_alert_go_through_outbox(bot, tg, session) -> None:
    from datetime import UTC, datetime

    from lab.core.ladder import Transition

    t = Transition(
        id=1, strategy_id="cex-perp-preset-x", from_rung="micro", to_rung="micro",
        status="degraded", reason="дневной стоп пробит", by="system",
        metrics_snapshot={}, ts=datetime(2026, 9, 5, tzinfo=UTC),
    )
    bot.notify_transition(t)
    await bot.flush()
    assert tg.sent[-1].kind == "transition" and "деградация" in tg.sent[-1].text

    await bot.alert_service_down("worker", silent_for_s=300)
    assert tg.sent[-1].kind == "alert" and "worker" in tg.sent[-1].text
    assert "5 мин" in tg.sent[-1].text


async def test_expiry_after_restart_edits_card_from_outbox(
    session, tg, halt, clock, journal, strategy
) -> None:
    """Карточку слал прошлый процесс: новый экземпляр бота берёт её из outbox, а не из памяти."""
    from lab.bot import TraderBot
    from lab.core.ladder import Ladder
    from tests.core.test_ladder import FakeThreshold

    def new_bot() -> TraderBot:
        return TraderBot(
            session_factory=lambda: session, admin_id=ADMIN, transport=tg, clock=clock,
            ladder_factory=lambda s: Ladder(s, threshold=FakeThreshold(), halt=halt),
            close_sessions=False,
        )

    session.get_one(StrategyRow, strategy.id).rung = "signal"
    session.flush()
    rec = journal.record_signal(make_signal(strategy.id, ttl_s=60))
    await new_bot().send_card("signal", dict(signal_id=rec.id, strategy_id=strategy.id,
                                             rung="signal", instrument="BTC-USDT", side="buy",
                                             size="0.01", price_ref="60000", venue="bybit",
                                             ttl_s=60))
    clock.set(T0 + timedelta(seconds=601))  # ttl — params.ttl_s=600 манифеста, не ttl сигнала
    assert await new_bot().expire_pending() == [rec.id]  # «перезапуск»
    msg_id, edited = tg.edited[-1]
    assert msg_id == 1001 and edited.buttons == [] and "просрочен" in edited.text.lower()
    assert "BTC-USDT" in edited.text  # исходный текст карточки сохранён


async def test_candidate_card_shows_what_the_hook_returned(session, tg, halt, clock) -> None:
    """«В замер» возвращает итог замера — он и попадает в карточку, а не «решение записано»."""
    from lab.bot import TraderBot
    from lab.core.ladder import Ladder
    from tests.core.test_ladder import FakeThreshold

    verdict = "В замер: cex-spot-x · backtest — замер выполнен, порог: passed"
    bot = TraderBot(
        session_factory=lambda: session,
        admin_id=ADMIN,
        transport=tg,
        clock=clock,
        ladder_factory=lambda s: Ladder(s, threshold=FakeThreshold(), halt=halt),
        on_candidate=lambda ref_id, decision: verdict,
        close_sessions=False,
    )
    await bot.send_card(
        "candidate", {"candidate_id": "7", "kind": "wallet", "ref": "0xabc", "summary": "тест"}
    )

    reply = await bot.handle_callback(ADMIN, "cand:7:accept")

    assert verdict in (reply.text or "")
    assert verdict in tg.edited[-1][1].text
