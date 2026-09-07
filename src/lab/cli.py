"""CLI: python -m lab <команда>.

lab venues                              — таблица площадок «подключена / только данные»
lab strategy add --file examples/strategy.yaml
lab strategy list [--branch B] [--status S]
lab strategy retire <id> --reason "..."
lab candidate add <kind> <ref>
lab service <worker|bot|web>            — worker: планировщик; bot: Trader (тикет 05); web: таск 06
"""

import argparse
import sys
import time
from pathlib import Path

import yaml

from lab import __version__
from lab.config import environment, format_venues_table, venues_report
from lab.core.registry import (
    DuplicateStrategy,
    IncompleteManifest,
    Registry,
    StrategyNotFound,
)
from lab.db import make_engine, make_session_factory, session_scope

EMPTY_HINT = "Реестр пуст: добавь кандидата (lab strategy add --file ...) или запусти поиск."
HEARTBEAT_DIR = Path("/tmp/lab")


def print_startup_banner(service: str) -> None:
    print(f"crypto-trading-lab {__version__} — сервис {service}")
    print(format_venues_table(venues_report(environment())))


def _session_factory():
    return make_session_factory(make_engine())


def cmd_venues(_: argparse.Namespace) -> int:
    print(format_venues_table(venues_report(environment())))
    return 0


def cmd_strategy_add(args: argparse.Namespace) -> int:
    spec = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or {}
    with session_scope(_session_factory()) as session:
        try:
            strategy = Registry(session).add(spec)
        except DuplicateStrategy as err:
            print(f"Отказ: {err}", file=sys.stderr)
            return 2
        except IncompleteManifest as err:
            session.commit()  # черновик сохраняется даже при отказе
            print(f"Ошибка манифеста: {err}", file=sys.stderr)
            for field, msg in err.fields.items():
                print(f"  - {field}: {msg}", file=sys.stderr)
            return 3
    print(f"Добавлена стратегия {strategy.id} [{strategy.status}, ступень {strategy.rung}]")
    return 0


def cmd_strategy_list(args: argparse.Namespace) -> int:
    with session_scope(_session_factory()) as session:
        rows = Registry(session).list(branch=args.branch, status=args.status)
    if not rows:
        print(EMPTY_HINT)
        return 0
    width = max(len(r.id) for r in rows)
    for r in rows:
        print(f"{r.id.ljust(width)}  {r.branch:<10} {r.venue:<12} {r.rung:<9} {r.status}")
    return 0


def cmd_strategy_retire(args: argparse.Namespace) -> int:
    with session_scope(_session_factory()) as session:
        try:
            s = Registry(session).retire(args.id, reason=args.reason)
        except StrategyNotFound as err:
            print(f"Отказ: {err}", file=sys.stderr)
            return 2
    print(f"Стратегия {s.id} отправлена в архив: {s.retired_reason}")
    return 0


def cmd_candidate_add(args: argparse.Namespace) -> int:
    with session_scope(_session_factory()) as session:
        c = Registry(session).enqueue_candidate(args.kind, args.ref)
    print(f"Кандидат #{c.id} {c.kind}:{c.ref} — {c.decision}")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    print_startup_banner(args.name)
    if args.name == "web":  # таск 06: uvicorn на WEB_BIND, остальное — в lab.web
        from lab.web import serve

        return serve(_session_factory, once=args.once)
    if args.name == "bot":  # таск 05
        return cmd_service_bot(args)
    return cmd_service_worker(args)  # таск 05: планировщик


def _heartbeat(name: str):
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    beat = HEARTBEAT_DIR / f"{name}.heartbeat"

    def tick() -> None:
        beat.write_text(str(int(time.time())), encoding="utf-8")

    return beat, tick


def _trader_bot(env: dict[str, str]):
    """Тикет 05: TraderBot на aiogram-транспорте; None (с причиной), если нет токена/админа."""
    from lab.bot import TraderBot
    from lab.bot.telegram import TelegramTransport, make_aiogram_bot
    from lab.core.ladder import Ladder, default_threshold_fn
    from lab.core.risk import DbHaltSwitch

    token, admin = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_ADMIN_ID")
    if not token or not admin:
        return None, None, "TELEGRAM_BOT_TOKEN/TELEGRAM_ADMIN_ID не заданы в .env"
    aio = make_aiogram_bot(token)
    factory = _session_factory()
    bot = TraderBot(
        session_factory=factory,
        admin_id=int(admin),
        transport=TelegramTransport(aio),
        ladder_factory=lambda s: Ladder(s, threshold=default_threshold_fn(), halt=DbHaltSwitch(s)),
    )
    return bot, aio, None


def cmd_service_bot(args: argparse.Namespace) -> int:
    """Бот Trader: long polling + утренний отчёт/протухание/outbox по schedule.yaml."""
    import asyncio

    from lab.bot.telegram import run_bot
    from lab.ops.scheduler import default_scheduler

    beat, tick = _heartbeat("bot")
    bot, aio, why = _trader_bot(environment())
    tick()
    if bot is None:
        print(f"Сервис bot: {why}; heartbeat → {beat}")
        return 0 if args.once else 1
    if args.once:
        print(f"Сервис bot готов: админ {bot.admin_id}; heartbeat → {beat}")
        return 0
    asyncio.run(run_bot(bot, aio, scheduler=default_scheduler(), heartbeat=tick))
    return 0


def cmd_service_worker(args: argparse.Namespace) -> int:
    """Worker: APScheduler с расписанием из schedule.yaml. Задания ядра (фиды, замеры,
    сверка, бэкап) регистрируют свои тикеты через `ops.scheduler.register(job)`;
    здесь — только запуск планировщика и heartbeat."""
    from lab.ops.scheduler import default_scheduler

    beat, tick = _heartbeat("worker")
    sched = default_scheduler()
    tick()
    print(
        f"Сервис worker: планировщик {sched.tz.key}, заданий: {len(sched.jobs())}; "
        f"heartbeat → {beat}"
    )
    if args.once:
        return 0
    sched.start()
    try:
        while True:
            tick()
            time.sleep(10)
    except KeyboardInterrupt:
        return 0
    finally:
        sched.shutdown()


def cmd_data_backfill(args: argparse.Namespace) -> int:
    """Таск 04: свечи CEX за N дней в Parquet с прогрессом; прерывание — повтор продолжит."""
    from lab.data import CandleStore
    from lab.data.backfill_cex import backfill_venue

    symbols = [s.strip() for chunk in args.symbols for s in chunk.split(",") if s.strip()]
    last: dict[str, int] = {}

    def progress(instrument: str, done: int, total: int) -> None:
        pct = 100 * done // total if total else 100
        if last.get(instrument) != pct:
            last[instrument] = pct
            label = f"{args.venue} {instrument} {args.tf}"
            print(f"{label}: {done}/{total} свечей ({pct}%)", flush=True)

    results = backfill_venue(
        CandleStore(args.root), args.venue, symbols, args.tf, args.days, progress=progress
    )
    failed = 0
    for r in results:
        if r.error:
            failed += 1
            print(
                f"{r.venue} {r.instrument} {r.tf}: прерван на {r.resume_from:%Y-%m-%d %H:%M}"
                f" — {r.error}; записано {r.rows_written}."
                " Повторите команду — продолжится с этой точки."
            )
        elif r.result and r.result.skipped:
            print(f"{r.venue} {r.instrument} {r.tf}: уже загружено, пропуск")
        elif r.result:
            n = r.result.rows_written
            print(f"{r.venue} {r.instrument} {r.tf}: готово, записано {n} свечей")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lab", description="crypto-trading-lab")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("venues", help="площадки: подключена / только данные").set_defaults(
        func=cmd_venues
    )

    st = sub.add_parser("strategy", help="реестр стратегий").add_subparsers(
        dest="action", required=True
    )
    add = st.add_parser("add")
    add.add_argument("--file", required=True)
    add.set_defaults(func=cmd_strategy_add)
    lst = st.add_parser("list")
    lst.add_argument("--branch")
    lst.add_argument("--status")
    lst.set_defaults(func=cmd_strategy_list)
    ret = st.add_parser("retire")
    ret.add_argument("id")
    ret.add_argument("--reason", required=True)
    ret.set_defaults(func=cmd_strategy_retire)

    cand = sub.add_parser("candidate", help="очередь кандидатов").add_subparsers(
        dest="action", required=True
    )
    cadd = cand.add_parser("add")
    cadd.add_argument("kind")
    cadd.add_argument("ref")
    cadd.set_defaults(func=cmd_candidate_add)

    data = sub.add_parser("data", help="данные: бэкфилл свечей").add_subparsers(
        dest="action", required=True
    )
    bf = data.add_parser("backfill", help="свечи CEX за N дней в Parquet (таск 04)")
    bf.add_argument("--venue", required=True, choices=["bybit", "okx", "binance", "hyperliquid"])
    bf.add_argument(
        "--symbols", required=True, action="append", help="через запятую или повтором флага"
    )
    bf.add_argument("--tf", default="1h")
    bf.add_argument("--days", type=int, default=365)
    bf.add_argument("--root", default="data", help="корень Parquet-хранилища")
    bf.set_defaults(func=cmd_data_backfill)

    svc = sub.add_parser("service", help="запуск сервиса worker|bot|web")
    svc.add_argument("name", choices=["worker", "bot", "web"])
    svc.add_argument("--once", action="store_true", help="напечатать баннер и выйти")
    svc.set_defaults(func=cmd_service)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
