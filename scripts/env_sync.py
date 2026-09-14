#!/usr/bin/env python
"""Перенести ИМЕНОВАННЫЕ переменные из одного `.env` в другой, не показывая значений.

Зачем отдельный инструмент. Ключи заводятся на машине владельца, а лаборатория работает
на сервере, и эти два `.env` расходятся молча: 14.09.2026 выяснилось, что `DUNE_API_KEY`,
`TELEGRAM_API_ID` и `TELEGRAM_API_HASH` заполнены локально и ПУСТЫ на сервере — то есть
для лаборатории их не существовало. Понять это по поведению нельзя: источник просто спит.

Правило владельца: значения секретов не читать вслух, не логировать и не показывать.
Поэтому здесь перенос ФАЙЛ→ФАЙЛ: скрипт видит значения только внутри себя, печатает
исключительно имена и слова «перенесено / уже было / пусто в источнике».

Три предосторожности, каждая из-за цены ошибки:

* **только названные переменные** — остальное в целевом файле не трогается вовсе,
  иначе можно затереть `DATABASE_URL` сервера локальным;
* **резервная копия** цели рядом, с меткой времени;
* **`--dry-run` по умолчанию**: без `--write` файл не меняется, печатается только план.

    python scripts/env_sync.py --from .env --to /tmp/env-part --names DUNE_API_KEY
    python scripts/env_sync.py --from /tmp/env-part --to .env --names DUNE_API_KEY --write
"""

from __future__ import annotations

import argparse
import shutil
from datetime import UTC, datetime
from pathlib import Path


def read_pairs(path: Path) -> dict[str, str]:
    """KEY=VALUE из файла. Комментарии и пустые строки пропускаются."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        out[key.strip()] = value.strip()
    return out


def apply(target: Path, updates: dict[str, str]) -> None:
    """Заменить значения названных переменных, сохранив порядок строк и комментарии.

    Переменная, которой в целевом файле нет вовсе, дописывается в конец: имя должно
    появиться, иначе перенос молча не состоится.
    """
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.partition("=")[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    target.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="src", required=True, help="откуда брать значения")
    ap.add_argument("--to", dest="dst", required=True, help="куда класть")
    ap.add_argument("--names", required=True, help="имена переменных через запятую")
    ap.add_argument("--write", action="store_true", help="без него файл не меняется")
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    if not src.is_file():
        print(f"нет файла-источника: {src}")
        return 2
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    have = read_pairs(src)
    already = read_pairs(dst)

    updates: dict[str, str] = {}
    print(f"{'переменная':26}{'состояние':<24}")
    for name in names:
        value = have.get(name, "")
        if not value:
            print(f"{name:26}пусто в источнике — пропуск")
            continue
        if already.get(name):
            print(f"{name:26}в цели уже заполнено — пропуск")
            continue
        updates[name] = value
        print(f"{name:26}будет перенесено")

    if not updates:
        print("\nпереносить нечего")
        return 0
    if not args.write:
        print(f"\nэто план. Чтобы применить, добавьте --write (файл {dst} не изменён)")
        return 0

    if dst.is_file():
        backup = dst.with_suffix(f".bak-{datetime.now(UTC):%Y%m%d-%H%M%S}")
        shutil.copy2(dst, backup)
        print(f"\nрезервная копия: {backup}")
    apply(dst, updates)
    print(f"перенесено имён: {len(updates)} → {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
