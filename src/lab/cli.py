"""CLI: python -m lab <команда>.

lab venues                              — таблица площадок «подключена / только данные»
lab strategy add --file examples/strategy.yaml
lab strategy list [--branch B] [--status S]
lab strategy retire <id> --reason "..."
lab candidate add <kind> <ref>
lab measure run <id> [--mode M] [--days N] [--root DIR]   — замерить стратегию сейчас
lab measure show <id> [--limit N]       — что уже замерено и с каким порогом
lab service <worker|bot|web>            — worker: планировщик; bot: Trader (тикет 05); web: таск 06

`.env` выкладывается в окружение до разбора команды (`apply_dotenv`): иначе `lab venues`
(читает `.env`) и проверки доступа исполнителей (читают `os.environ`) расходятся в оценке
одних и тех же ключей.
"""

import argparse
import secrets
import sys
import time
from pathlib import Path

import yaml

from lab import __version__
from lab.config import apply_dotenv, environment, format_venues_table, venues_report
from lab.core.registry import (
    DuplicateStrategy,
    IncompleteManifest,
    Registry,
    StrategyNotFound,
)
from lab.db import make_engine, make_session_factory, session_scope
from lab.ops.measure import make_measure

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


def cmd_measure_run(args: argparse.Namespace) -> int:
    """Замер руками (R12): та же обёртка, что у `remeasure` и кнопки «В замер» в боте."""
    from datetime import UTC, datetime, timedelta

    from lab.core.registry import StrategyNotFound
    from lab.ops.measure import MeasureUnavailable, make_measure

    to = datetime.now(UTC)
    window = (to - timedelta(days=args.days), to)
    measure = make_measure(_scope(), root=args.root)
    try:
        m = measure(strategy_id=args.strategy_id, mode=args.mode, window=window)
    except (StrategyNotFound, MeasureUnavailable) as err:
        print(f"Замер не выполнен: {err}", file=sys.stderr)
        return 2
    print(format_measurement(m, window=window))
    return 0


def cmd_measure_show(args: argparse.Namespace) -> int:
    """Последние снимки стратегии — что именно замерено и когда."""
    from lab.core.measure import history

    with _scope()() as session:
        rows = history(session, args.strategy_id, limit=args.limit)
        if not rows:
            print(
                f"{args.strategy_id}: ещё не мерил — запусти "
                f"`lab measure run {args.strategy_id}`"
            )
            return 0
        for m in rows:
            print(format_measurement(m))
    return 0


def format_measurement(m, *, window=None) -> str:
    """Одна строка «что замерено» + порог; incomplete печатает причину, а не пустоту."""
    at = f"{m.created_at:%d.%m %H:%M}" if m.created_at else "—"
    win = window or (m.window_from, m.window_to)
    head = (
        f"{m.strategy_id} · {m.mode} · {win[0]:%d.%m.%Y}–{win[1]:%d.%m.%Y}"
        f" · {m.status} · {at}"
    )
    if m.status != "ok" or m.metrics is None:
        return f"{head}\n  причина: {m.reason or 'нет данных'}"
    lines = [head]
    mt = m.metrics
    for name in ("n_trades", "net_pnl_pct", "max_dd_pct", "sharpe", "win_rate", "vs_btc"):
        value = getattr(mt, name, None)
        if value is not None:
            lines.append(f"  {name}: {value}")
    stopped_at = getattr(mt, "stopped_at", None)
    if stopped_at is not None:
        rule = {
            "strategy_stop_dd": "по просадке",
            "strategy_stop_daily": "дневной",
        }.get(getattr(mt, "stop_rule", ""), getattr(mt, "stop_rule", ""))
        lines.append(
            f"  СТОП стратегии сработал ({rule}) {stopped_at:%d.%m.%Y}: дальше открытия "
            f"запрещены, пропущено сигналов {getattr(mt, 'blocked_signals', 0)}"
        )
    if m.threshold is not None:
        failed = ", ".join(m.threshold.failed_names()) or "—"
        lines.append(f"  порог: {m.threshold.status} (не прошло: {failed})")
    lines.append(f"  снимок: данные {m.data_hash[:12]} · код {m.code_version[:12]}")
    return "\n".join(lines)


def cmd_service(args: argparse.Namespace) -> int:
    from lab.db.engine import DatabaseUrlMissing

    print_startup_banner(args.name)
    try:
        if args.name == "web":
            return cmd_service_web(args)
        if args.name == "bot":
            return cmd_service_bot(args)
        return cmd_service_worker(args)
    except DatabaseUrlMissing as err:  # сервисы без базы не поднимаются — скажем это по-русски
        print(f"Сервис {args.name} не поднят: {err}", file=sys.stderr)
        return 2


def _scope():
    """Фабрика контекста сессии: `with scope() as session`. Её ждут модули `lab.ops`."""
    factory = _session_factory()
    return lambda: session_scope(factory)


def _feeds_registry(scope):
    """Реестр источников: квоты, здоровье, бюджет. Он же — источник для `/feeds` и отчёта."""
    from lab.ops.feeds_registry import FeedsRegistry

    try:
        return FeedsRegistry(session_factory=scope)
    except Exception as err:  # noqa: BLE001 — без базы реестр не поднимется, сервис живёт
        print(f"Реестр источников не поднят: {err}", file=sys.stderr)
        return None


def _db_beat(scope, service: str, period_s: int = 30):
    """Фоновый heartbeat сервиса: файл (healthcheck контейнера) и база (watchdog, R32i.2)."""
    import threading

    from lab.ops.watchdog import beat as db_beat

    _, tick = _heartbeat(service)

    def loop() -> None:
        while True:
            tick()
            try:
                with scope() as session:
                    db_beat(session, service)
            except Exception:  # noqa: BLE001 — обрыв базы не должен ронять сервис
                pass
            time.sleep(period_s)

    threading.Thread(target=loop, daemon=True, name=f"heartbeat-{service}").start()


def cmd_service_web(args: argparse.Namespace) -> int:
    """Веб-экран (таск 06) с подключённым реестром источников на `/feeds`."""
    from lab.web import bind_address, create_app, credentials_from_env

    beat, tick = _heartbeat("web")
    tick()
    env = environment()  # `.env` поверх окружения — как обещает README
    auth, generated = credentials_from_env(env), False
    if auth is None:
        # `cp .env.example .env` без правок не должен упираться в отказ: логин остаётся
        # обязательным (§15), но пароль на этот запуск генерируем и показываем оператору.
        auth, generated = ((env.get("WEB_USER") or "lab").strip(), secrets.token_urlsafe(12)), True
    host, port = bind_address(env)
    scope = _scope()
    app = create_app(scope, feeds=_feeds_registry(scope), auth=auth)
    print(f"Веб-экран: http://{host}:{port}/ (пользователь {auth[0]}); heartbeat → {beat}")
    if generated:
        print(
            f"WEB_USER/WEB_PASSWORD не заданы в .env — разовый пароль на этот запуск: {auth[1]}."
            " Впиши свои в .env, чтобы он не менялся при перезапуске."
        )
    if args.once:
        return 0
    _db_beat(scope, "web")
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def _heartbeat(name: str):
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    beat = HEARTBEAT_DIR / f"{name}.heartbeat"

    def tick() -> None:
        beat.write_text(str(int(time.time())), encoding="utf-8")

    return beat, tick


def _trader_bot(env: dict[str, str]):
    """Тикет 05: TraderBot на aiogram-транспорте; None (с причиной), если нет токена/админа.

    Здесь же — швы таска 14: `/feeds` и утренний отчёт читают реестр источников, а
    подтверждение сигнала (`on_confirm`) уходит в исполнение через риск-ядро (`ops.worker`).
    """
    from lab.bot import TraderBot
    from lab.bot.telegram import TelegramTransport, make_aiogram_bot
    from lab.core.ladder import Ladder, default_threshold_fn
    from lab.core.risk import DbHaltSwitch
    from lab.discovery import candidate_hook
    from lab.ops.jobs import rebalance_hook

    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    admin = (env.get("TELEGRAM_ADMIN_ID") or "").strip()
    if not token or not admin.isdigit():
        return None, None, "TELEGRAM_BOT_TOKEN/TELEGRAM_ADMIN_ID не заданы в .env"
    try:
        aio = make_aiogram_bot(token)
    except Exception as err:  # noqa: BLE001 — мусор в токене не должен ронять worker
        return None, None, f"TELEGRAM_BOT_TOKEN не принят: {err}"
    factory = _session_factory()
    scope = lambda: session_scope(factory)  # noqa: E731
    feeds = _feeds_registry(scope)
    ladder_factory = lambda s: Ladder(  # noqa: E731
        s, threshold=default_threshold_fn(), halt=DbHaltSwitch(s)
    )
    worker = None
    try:
        from lab.ops.worker import Worker

        worker = Worker(scope, env=env)
    except Exception as err:  # noqa: BLE001 — без исполнения бот всё равно нужен
        print(f"Исполнение сигналов недоступно: {err}", file=sys.stderr)
    # Кнопка «В замер» обязана мерить: без `measure=` кандидат заводится, но замера нет (R12).
    measure = worker.measure if worker is not None else make_measure(scope)
    bot = TraderBot(
        session_factory=factory,
        admin_id=int(admin),
        transport=TelegramTransport(aio),
        ladder_factory=ladder_factory,
        feeds_status=feeds,
        on_confirm=(worker.place_signal if worker is not None else None),
        on_candidate=candidate_hook(scope, measure=measure, ladder_factory=ladder_factory),
        on_rebalance=rebalance_hook(scope),
    )
    if worker is not None:
        worker.bot = bot
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
        if args.once:
            return 0
        # Опрашивать Telegram нечем, но выходить нельзя: под `restart: unless-stopped`
        # это карусель перезапусков (17 за четыре минуты), лог забит стартовыми
        # сообщениями, healthcheck мигает. Стоим живыми с heartbeat'ом, пока в .env
        # не появится токен и сервис не перезапустят — так это и описано в README.
        while True:
            tick()
            time.sleep(30)
    if args.once:
        print(f"Сервис bot готов: админ {bot.admin_id}; heartbeat → {beat}")
        return 0
    asyncio.run(run_bot(bot, aio, scheduler=default_scheduler(), heartbeat=tick))
    return 0


def cmd_service_worker(args: argparse.Namespace) -> int:
    """Worker (решение §11): доступность площадок при старте, восстановление ордеров,
    планировщик со всеми заданиями, фиды с квотами, сверка, бэкап, watchdog."""
    from lab.ops.worker import Worker

    beat, tick = _heartbeat("worker")
    tick()
    scope = _scope()
    bot, _aio, why = _trader_bot(environment())
    if bot is None:
        print(f"Карточки в Telegram отключены: {why}", file=sys.stderr)
    worker = Worker(scope, env=environment(), bot=bot)
    print(f"Сервис worker: планировщик {worker.scheduler.tz.key}; heartbeat → {beat}")
    if not args.once:
        _db_beat(scope, "worker")
    return worker.run(once=args.once)


def cmd_ops(args: argparse.Namespace) -> int:
    """Эксплуатация: разовый бэкап, перезагрузка конфигов, состояние источников."""
    scope = _scope()
    if args.action == "backup":
        from lab.ops.backup import backup

        result = backup(dest=args.dest, keep_days=args.keep_days)
        if not result.ok:
            print(f"Бэкап не сделан: {result.error}", file=sys.stderr)
            return 1
        print(
            f"Копия: {result.path} ({result.size_bytes} байт);"
            f" удалено старых: {len(result.rotated)}"
        )
        return 0
    if args.action == "reload":
        from lab.ops.reload import ConfigReloader

        reloader = ConfigReloader(config_dir=None, session_factory=scope)
        reloader.reload(by="operator")  # снимок
        report = reloader.reload(by=args.by)
        if not report.applied:
            print(f"Конфиги не применены: {report.error}", file=sys.stderr)
            return 1
        changed = f": {', '.join(report.changed)}" if report.changed else ""
        print("Конфиги перезагружены" + changed)
        return 0
    if args.action == "feeds":
        registry = _feeds_registry(scope)
        if registry is None:
            return 1
        for row in registry.status():
            quota = (
                f"{row.quota_used}/{row.quota_limit} за {row.quota_period}"
                if row.quota_limit
                else "без лимита"
            )
            forecast = f", кончится {row.exhausted_at:%d.%m %H:%M}" if row.exhausted_at else ""
            print(f"{row.id:<18} {row.health:<9} {quota}{forecast}")
        b = registry.budget()
        print(
            f"Бюджет: {b.spent_usd:.2f} из {b.month_limit_usd:.2f} USD,"
            f" прогноз {b.forecast_usd:.2f}"
        )
        return 0
    if args.action == "status":
        from lab.ops.availability import (
            check_all,
            default_access_checks,
            default_probes,
            format_availability,
        )

        registry = _feeds_registry(scope)
        with scope() as session:
            rows = check_all(
                default_probes(quota=registry),
                session=session,
                access_checks=default_access_checks(),
            )
        print(format_availability(rows))
        return 0
    return 2


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

    ms = sub.add_parser("measure", help="замер стратегии: запустить руками и посмотреть")
    msub = ms.add_subparsers(dest="action", required=True)
    mrun = msub.add_parser("run", help="замерить стратегию сейчас")
    mrun.add_argument("strategy_id")
    mrun.add_argument(
        "--mode", default="backtest", choices=["backtest", "paper", "forward", "micro"]
    )
    mrun.add_argument("--days", type=int, default=365, help="длина окна замера в сутках")
    mrun.add_argument("--root", default=None, help="корень хранилища свечей (по умолчанию data)")
    mrun.set_defaults(func=cmd_measure_run)
    mshow = msub.add_parser("show", help="последние снимки замеров стратегии")
    mshow.add_argument("strategy_id")
    mshow.add_argument("--limit", type=int, default=5)
    mshow.set_defaults(func=cmd_measure_show)

    ops = sub.add_parser("ops", help="эксплуатация: бэкап, перезагрузка конфигов, источники")
    ops.add_argument(
        "action", choices=["backup", "reload", "feeds", "status"], help="что сделать"
    )
    ops.add_argument("--dest", help="каталог копий (по умолчанию BACKUP_DIR)")
    ops.add_argument("--keep-days", type=int, default=14, help="сколько суток хранить копии")
    ops.add_argument("--by", default="operator", help="кто перезагружает конфиги")
    ops.set_defaults(func=cmd_ops)

    svc = sub.add_parser("service", help="запуск сервиса worker|bot|web")
    svc.add_argument("name", choices=["worker", "bot", "web"])
    svc.add_argument("--once", action="store_true", help="напечатать баннер и выйти")
    svc.set_defaults(func=cmd_service)
    return p


def main(argv: list[str] | None = None) -> int:
    # `.env` в окружение до всего остального: иначе `lab venues` (читает .env) и проверки
    # доступа исполнителей (читают os.environ) расходятся в оценке одних и тех же ключей.
    apply_dotenv()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
