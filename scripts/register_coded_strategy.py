"""Завести в реестр (`core.registry`) стратегию, у которой уже есть код.

Два реестра легко перепутать: `strategies.registry` — какие правила умеет собирать код,
`core.registry` — записи в базе со ступенью и статусом. Замер работает только когда id
есть в ОБОИХ. `lab strategy add --file` принимает YAML, поэтому манифест приходилось бы
переписывать руками — а он уже есть в коде, и любая опечатка дала бы вторую стратегию
с другим отпечатком.

    python scripts/register_coded_strategy.py --list
    python scripts/register_coded_strategy.py --id cex-spot-indicator-pifagor-mfi-v0
    python scripts/register_coded_strategy.py --all

Повторный запуск безопасен: уже заведённые пропускаются (DuplicateStrategy).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab.core.registry import DuplicateStrategy, Registry  # noqa: E402
from lab.db import make_engine, make_session_factory, session_scope  # noqa: E402
from lab.strategies import registry as code_registry  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Регистрация стратегии с кодом в реестре базы")
    ap.add_argument("--id", action="append", default=[], help="id стратегии (можно повторить)")
    ap.add_argument("--all", action="store_true", help="все стратегии из реестра кода")
    ap.add_argument("--list", action="store_true", help="показать, что умеет код, и выйти")
    args = ap.parse_args()

    known = sorted(code_registry.ids())
    if args.list or (not args.id and not args.all):
        print(f"Стратегии в реестре кода ({len(known)}):")
        for sid in known:
            m = code_registry.manifest(sid)
            tf = m.timeframe or "-"
            print(f"  {sid:50} {m.branch.value:9} {m.venue:9} {tf:4} {', '.join(m.instruments)}")
        return 0

    wanted = known if args.all else args.id
    unknown = [sid for sid in wanted if sid not in known]
    if unknown:
        print("нет кода правил для: " + ", ".join(unknown), file=sys.stderr)
        print("что умеет код — покажет --list", file=sys.stderr)
        return 2

    factory = make_session_factory(make_engine())
    added = skipped = 0
    with session_scope(factory) as session:
        registry = Registry(session)
        for sid in wanted:
            try:
                row = registry.add(code_registry.manifest(sid))
            except DuplicateStrategy:
                skipped += 1
                print(f"  уже в реестре: {sid}")
                continue
            added += 1
            print(f"  добавлена: {row.id} [{row.status}, ступень {row.rung}]")
    print(f"добавлено {added}, пропущено {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
