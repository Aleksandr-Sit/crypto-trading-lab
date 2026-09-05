"""CLI: python -m lab <команда>.

lab venues                              — таблица площадок «подключена / только данные»
lab strategy add --file examples/strategy.yaml
lab strategy list [--branch B] [--status S]
lab strategy retire <id> --reason "..."
lab candidate add <kind> <ref>
lab service <worker|bot|web>            — пустой сервис: баннер + heartbeat (тикет 01)
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
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    beat = HEARTBEAT_DIR / f"{args.name}.heartbeat"
    print(f"Сервис {args.name}: логика появится в следующих тикетах; heartbeat → {beat}")
    try:
        while True:
            beat.write_text(str(int(time.time())), encoding="utf-8")
            if args.once:
                return 0
            time.sleep(10)
    except KeyboardInterrupt:
        return 0


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
