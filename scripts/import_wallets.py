"""Посев списка отслеживаемых кошельков в `wallets_tracked`.

Зачем: отбор кошельков с нуля — это недели наблюдения. Соседний проект `smart-money`
уже отобрал 387 адресов Solana с медианой 7478 сделок по 1586 токенам (январь–июль 2026),
и их можно взять как СПИСОК ДЛЯ НАБЛЮДЕНИЯ.

Важно, чего скрипт НЕ делает: он не переносит чужую статистику. `stats_json` остаётся
пустым, `last_recalc` — NULL, то есть кошелёк числится «отслеживаем, но не мерен».
Метрики лаборатория считает сама (`lab.wallets.recalc`) по своей истории и своим порогам
из `config/chains.yaml`. Чужие цифры, посчитанные другой методикой и на другом окне,
в реестр не попадают — иначе ступень «кандидат» получила бы недостоверную опору.

Список адресов — текстовый файл, по одному в строке (`#` — комментарий):

    python scripts/import_wallets.py --chain solana --file /import/wallets.txt

Повторный запуск безопасен: существующие адреса пропускаются, не затираются.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab.db import make_engine, make_session_factory, session_scope  # noqa: E402
from lab.db.models import WalletTrackedRow  # noqa: E402


def read_addresses(path: Path) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Посев отслеживаемых кошельков")
    ap.add_argument("--file", required=True, type=Path, help="файл со списком адресов")
    ap.add_argument("--chain", required=True, help="сеть: solana, evm, ton")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет добавлено")
    args = ap.parse_args()

    if not args.file.exists():
        raise SystemExit(f"нет файла {args.file}")
    addresses = read_addresses(args.file)
    if not addresses:
        print("список пуст, нечего добавлять")
        return 0

    factory = make_session_factory(make_engine())
    added = skipped = 0
    with session_scope(factory) as session:
        for address in addresses:
            if session.get(WalletTrackedRow, (address, args.chain)) is not None:
                skipped += 1
                continue
            added += 1
            if not args.dry_run:
                session.add(WalletTrackedRow(address=address, chain=args.chain))
        if args.dry_run:
            session.rollback()

    word = "будет добавлено" if args.dry_run else "добавлено"
    print(f"{word}: {added}, уже было: {skipped}, всего в списке: {len(addresses)}")
    print("статистика НЕ переносилась — считает сама лаборатория (lab.wallets.recalc)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
