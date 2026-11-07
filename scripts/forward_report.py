"""Independent paper reports; never modifies rules, observations, trades or ladder."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from lab.config import load_threshold
from lab.core.forward import rules_spec
from lab.core.journal import Journal
from lab.core.measure.metrics import metrics
from lab.core.registry import Strategy, _fingerprint
from lab.data import CandleStore
from lab.db.models import (
    FillRow,
    ForwardObservationRow,
    OrderRow,
    OutboxRow,
    SignalRow,
    StrategyRow,
    SystemFlagRow,
)
from lab.ops.measure import _benchmark, _journal_trades

ID = "cex-spot-external-rotation-gold-btc"
STATE_KEY = "forward_report:rotation"
TZ = ZoneInfo("Europe/Samara")
HEARTBEAT = Path("/tmp/forward-report-heartbeat")
PENDING_GRACE = timedelta(minutes=15)


def stamp(value):
    try:
        result = datetime.fromisoformat(value) if value else None
        return result if result is not None and result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def fmt(value):
    return "н/д" if value is None else f"{Decimal(str(value)):.2f}"


def collect(session, now):
    record = session.get(StrategyRow, ID)
    observation = session.scalar(select(ForwardObservationRow).where(
        ForwardObservationRow.strategy_id == ID,
    ).order_by(ForwardObservationRow.started_at.desc(), ForwardObservationRow.id.desc()).limit(1))
    flag = session.get(SystemFlagRow, f"virtual_forward:{ID}")
    state = dict(flag.value or {}) if flag else {}
    problems = []
    if record is None or observation is None:
        return {"problems": ["нет контракта наблюдения"], "observation_id": None,
                "state": state, "start": None, "record": record, "observation": observation}
    if observation.revision != _fingerprint(rules_spec(Strategy.from_row(record))):
        problems.append("правила отличаются от контракта")
    if state.get("observation_id") != observation.id:
        problems.append("сборщик не подключён к текущему наблюдению")
    if record.rung != "paper" or record.status not in ("measuring", "passed"):
        problems.append("стратегия не находится в активном paper")
    checked = stamp(state.get("checked_at"))
    if (checked is None or now - checked > timedelta(minutes=3)
            or checked > now + timedelta(seconds=5)):
        problems.append("сборщик не обновлялся больше 3 минут или время неверно")
    for key, label in (
        ("error", "ошибка сбора"), ("blocked", "сбор заблокирован"),
        ("archive_missing", "пропуски архива"),
        ("archive_mismatches", "расхождение с архивом"),
        ("unpriced_positions", "позиции без оценки"),
    ):
        if state.get(key):
            problems.append(label)
    start = max(observation.started_at, stamp(state.get("started_at")) or observation.started_at)
    if start > now:
        problems.append("начало наблюдения в будущем")
    return {"record": record, "observation": observation, "state": state,
            "observation_id": observation.id, "problems": problems, "start": start}


def report(session, snapshot, now, *, weekly=False, store=None):
    """Fresh diagnostics for one observation. Empty samples never become zero performance."""
    state = snapshot["state"]
    lines = [f"{'Недельный' if weekly else 'Ежедневный'} бумажный форвард · ротация золото/BTC",
             f"На {now.astimezone(TZ):%d.%m.%Y %H:%M} Самара",
             f"Наблюдение: {snapshot['observation_id'] or 'н/д'}"]
    problems = snapshot["problems"]
    lines.append("Сбор: " + ("; ".join(problems) if problems else "работает"))
    checked = stamp(state.get("checked_at"))
    lines.append(f"Последний тик: {checked.astimezone(TZ):%d.%m %H:%M:%S}" if checked
                 else "Последний тик: н/д")
    lines.append("Архив: ожидает / пропуски / расхождения = " + " / ".join(
        str(state.get(key, "н/д")) for key in (
            "archive_pending", "archive_missing", "archive_mismatches",
        )))
    observation, start = snapshot["observation"], snapshot["start"]
    if observation is None or start is None or start > now:
        return "\n".join([*lines, "Результат: недостаточно данных, показатели не определены."])
    lines.append(f"Начало сбора: {start.astimezone(TZ):%d.%m.%Y %H:%M}")
    since = max(start, now - timedelta(days=7 if weekly else 1))
    outcomes = Counter(session.execute(select(SignalRow.outcome).where(
        SignalRow.observation_id == observation.id, SignalRow.decided_at >= since,
        SignalRow.decided_at <= now,
    )).scalars())
    fills = session.scalar(select(func.count()).select_from(FillRow).join(OrderRow).join(
        SignalRow, OrderRow.signal_id == SignalRow.id,
    ).where(SignalRow.observation_id == observation.id, OrderRow.mode == "paper",
            FillRow.ts >= since, FillRow.ts <= now))
    lines.append(f"За последние {'7 суток' if weekly else '24 часа'} в пределах наблюдения: "
                 f"решений {sum(outcomes.values())}, исполнений {fills}, "
                 f"пропущено/отклонено {outcomes['skipped'] + outcomes['rejected']}, "
                 f"истекло {outcomes['expired']}")
    opened = [t for t in Journal(session).open_trades(ID) if t.mode == "paper"]
    lines.append(f"Открытых бумажных позиций: {len(opened)}")
    lines.append(f"Накопленных ошибок исполнения: {state.get('execution_errors', 0)}")
    marks = stamp(state.get("marks_at"))
    lines.append(f"Открытый P&L по последней оценке: {fmt(state.get('unrealized_pnl'))} USD"
                 + (f" на {marks.astimezone(TZ):%d.%m %H:%M}" if marks else " (время н/д)"))
    if checked is None or now - checked > timedelta(minutes=3):
        lines.append("Оценка устарела; текущая стоимость позиций неизвестна.")
    if not weekly:
        return "\n".join([*lines, "Отсутствие сделок не означает нулевой риск или доходность."])
    # No wrapper run(), no measurement writes, no creation of observations or promotion.
    if ("правила отличаются от контракта" in problems
            or state.get("observation_id") != observation.id):
        return "\n".join([*lines, "Метрики не рассчитаны: версия наблюдения не подтверждена."])
    excluded = {}
    trades = _journal_trades(session, ID, (start, now), observation=observation, excluded=excluded)
    lines.append(f"За всё наблюдение: зачтено закрытых сделок {len(trades)}, "
                 f"исключено {sum(excluded.values())}")
    if excluded:
        lines.append("Причины исключения: " + "; ".join(f"{k}: {v}" for k, v in excluded.items()))
    config = load_threshold()
    lines.append(f"Выборка: {len(trades)} из {config.min_trades} для общего порога")
    if not trades:
        return "\n".join([
            *lines, "P&L, EV, интервал, стресс и просадка: н/д — нет зачтённых сделок.",
            "Итог: недостаточно данных; допуска к следующей ступени нет.",
        ])
    benchmark = None
    benchmark_note = "н/д"
    try:
        if store is None:
            store = CandleStore(os.environ.get("LAB_DATA_ROOT", "/app/data"))
        bars = sorted([b for b in store.read("binance", "BTC/USDT", "1d", start, now)
                       if b.ts + timedelta(days=1) <= now], key=lambda b: b.ts)
        if (len(bars) >= 2 and bars[0].ts - start <= timedelta(days=1)
                and now - (bars[-1].ts + timedelta(days=1)) <= timedelta(days=1)
                and all(b.ts - a.ts == timedelta(days=1)
                        for a, b in zip(bars, bars[1:], strict=False))):
            # Restrict the core benchmark to fully closed, contiguous daily bars.
            benchmark = _benchmark(
                store, "binance", "1d", (bars[0].ts, bars[-1].ts + timedelta(days=1)),
                kind="btc_dca",
            )
            benchmark_note = (f"{fmt(benchmark)}%; дневное приближение "
                              f"{bars[0].ts:%d.%m}–{bars[-1].ts + timedelta(days=1):%d.%m}")
    except Exception:  # noqa: BLE001 — missing benchmark must not suppress the report
        benchmark_note = "н/д — ошибка чтения архива"
    measured = metrics(trades, benchmark, capital=Decimal(str(
        observation.spec["measurement_capital"])), window=(start, now), config=config)
    lines.extend([
        f"Закрытый чистый P&L: {fmt(measured.net_pnl)} USD",
        f"Издержки закрытых сделок: {fmt(measured.costs.total)} USD",
        f"Закрытый EV: {fmt(measured.ev_per_trade)} USD/сделку",
        f"Просадка по закрытым сделкам: {fmt(measured.closed_max_dd_pct)}%",
        "Полная просадка капитала с открытыми позициями: н/д; непрерывной кривой нет.",
        f"Стресс на тех же закрытых сделках: EV {fmt(measured.cost_stress.ev_per_trade)} USD",
        f"Бенчмарк btc_dca: {benchmark_note}",
    ])
    info = measured.bootstrap
    blocks = info["groups"] // info["block_groups"] if info["block_groups"] else 0
    sufficient = len(trades) >= config.min_trades and blocks >= config.bootstrap_min_blocks
    if sufficient and measured.ev_ci95:
        lines.append(f"Номинальный блоковый интервал EV: {fmt(measured.ev_ci95[0])}…"
                     f"{fmt(measured.ev_ci95[1])} USD; поправка за поиск здесь не рассчитана.")
    else:
        lines.append(f"Интервал EV: н/д — выборка/блоки недостаточны ({blocks} блоков).")
    if (opened or excluded or problems or benchmark is None or state.get("archive_pending")
            or state.get("execution_errors")):
        lines.append("Опережение по итоговой доходности: н/д — открытые позиции, "
                     "неполные данные или проблемы сбора.")
    else:
        lines.append(f"Закрытая доходность {fmt(measured.net_pnl_pct)}% против "
                     f"btc_dca {fmt(benchmark)}%; границы бенчмарка округлены до дневных свечей.")
    conclusion = ("описательная статистика; общий допуск этим отчётом не проверяется."
                  if sufficient else "недостаточно данных; допуска к следующей ступени нет.")
    lines.append("Итог: " + conclusion)
    return "\n".join(lines)


def enqueue(session, chat_id, message, ref, now):
    if not chat_id:
        raise ValueError("TELEGRAM_ADMIN_ID is required for reporting")
    row = OutboxRow(chat_id=str(chat_id), kind="forward_report", ref=ref,
                    payload={"text": message, "buttons": [], "parse_mode": None},
                    created_at=now, attempts=0)
    session.add(row)
    session.flush()
    return row.id


def due_periods(now, start):
    local = now.astimezone(TZ)
    daily = local.replace(hour=9, minute=5, second=0, microsecond=0)
    if daily > local:
        daily -= timedelta(days=1)
    weekly = local.replace(hour=22, minute=20, second=0, microsecond=0)
    weekly -= timedelta(days=(local.weekday() + 1) % 7)
    if weekly > local:
        weekly -= timedelta(days=7)
    return [(kind, due.isoformat()) for kind, due in (("daily", daily), ("weekly", weekly))
            if start is not None and due >= start]


def cycle(session, now, chat_id, *, send_now=None, store=None):
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(6041106, 51)")):
        return []
    snapshot = collect(session, now)
    flag = session.get(SystemFlagRow, STATE_KEY)
    if flag is None:
        flag = SystemFlagRow(key=STATE_KEY, value={}, updated_by="forward-report")
        session.add(flag)
    state = dict(flag.value or {})
    if state.get("observation_id") != snapshot["observation_id"]:
        state = {"observation_id": snapshot["observation_id"]}
    pending = bool(snapshot["state"].get("archive_pending"))
    pending_since = stamp(state.get("pending_since")) if pending else None
    if pending and pending_since is None:
        pending_since = now
    state["pending_since"] = pending_since.isoformat() if pending_since else None
    problems = list(snapshot["problems"])
    if pending_since and now - pending_since >= PENDING_GRACE:
        problems.append("архив не подтверждён больше 15 минут")
    snapshot["problems"] = problems
    sent = []
    previous = state.get("problems", [])
    if sorted(problems) != sorted(previous):
        message = ("⚠️ Бумажный форвард ротации: " + "; ".join(problems) if problems
                   else "✅ Бумажный форвард ротации: сбор и сверка восстановлены.")
        sent.append(enqueue(session, chat_id, message,
                            f"forward:health:{now.timestamp()}", now))
    state["problems"] = problems
    periods = due_periods(now, snapshot["start"])
    if send_now:
        periods = [(send_now, f"manual:{now.astimezone(TZ):%Y-%m-%d}")]
    for kind, period in periods:
        key = f"last_manual_{kind}" if period.startswith("manual:") else f"last_{kind}"
        if state.get(key) == period:
            continue
        message = report(session, snapshot, now, weekly=kind == "weekly", store=store)
        sent.append(enqueue(session, chat_id, message,
                            f"forward:{kind}:{period}", now))
        state[key] = period
    state["checked_at"] = now.isoformat()
    flag.value = state
    flag.updated_by = "forward-report"
    return sent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preview", "once", "loop"])
    parser.add_argument("--weekly", action="store_true", help="weekly preview")
    parser.add_argument("--send-now", choices=["daily", "weekly"])
    args = parser.parse_args()
    if args.action != "once" and args.send_now:
        parser.error("--send-now requires once")
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    try:
        while True:
            try:
                with Session(engine) as session:
                    now = datetime.now(UTC)
                    if args.action == "preview":
                        session.execute(text("SET TRANSACTION READ ONLY"))
                        print(report(session, collect(session, now), now, weekly=args.weekly))
                        return
                    ids = cycle(session, now, os.environ.get("TELEGRAM_ADMIN_ID"),
                                send_now=args.send_now)
                    session.commit()
                HEARTBEAT.write_text(str(int(time.time())))
                if ids:
                    print(json.dumps({"queued_outbox_ids": ids}), flush=True)
            except Exception as error:
                # Connection failures may carry a DSN; never print exception text.
                print(f"Report cycle failed: {type(error).__name__}", flush=True)
                if args.action != "loop":
                    raise SystemExit(1) from None
            if args.action != "loop":
                return
            time.sleep(60)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
