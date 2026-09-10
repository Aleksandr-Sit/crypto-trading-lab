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
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab.contracts import StopSpec  # noqa: E402
from lab.core.registry import DuplicateStrategy, Registry  # noqa: E402
from lab.db import make_engine, make_session_factory, session_scope  # noqa: E402
from lab.db.models import StrategyRow  # noqa: E402
from lab.strategies import registry as code_registry  # noqa: E402


def _params(pairs: list[str]) -> dict[str, object]:
    """`имя=значение` → словарь параметров; числа приводятся к числам.

    Строковый «0.25» вместо числа тихо ломает арифметику правил, поэтому разбор здесь,
    а не в стратегии: она вправе рассчитывать на нормальные типы.
    """
    out: dict[str, object] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        if not sep or not name.strip():
            raise SystemExit(f"параметр должен быть вида имя=значение, получено {pair!r}")
        value: object = raw
        try:
            value = int(raw) if raw.strip().lstrip("-").isdigit() else float(raw)
        except ValueError:
            value = raw
        out[name.strip()] = value
    return out


def manifest_id(manifest) -> str:
    return f"{manifest.branch}-{manifest.source_kind}-{manifest.slug}"


def _sync(_registry, session, manifest) -> list[str]:
    """Подтянуть в существующую запись то, что изменилось в карточке. Возврат — что поменяли.

    Зачем: карточку правят, а запись в базе остаётся прежней. Так два перп-пресета почти
    год ходили с инструментами `BTCUSDT-PERP` — имя, которого фид не понимает («bybit does
    not have market symbol»), — хотя в карточках давно стоят ccxt-имена `BTC/USDT:USDT`.
    Замер при этом не падает, а честно отвечает `incomplete`, и расхождение легко не заметить.

    Параметры и ступень НЕ трогаем: ступень — это история стратегии, а параметры могли быть
    заданы вариантом (`--slug-suffix`) осознанно.
    """
    # Пишем в СТРОКУ базы, а не в модель из `registry.get`: та заморожена (pydantic),
    # и присваивание ей молча ничего бы не изменило… точнее, не молча — но и не изменило.
    row = session.get(StrategyRow, manifest_id(manifest))
    if row is None:
        return []
    changed: list[str] = []
    if list(row.instruments or []) != list(manifest.instruments):
        row.instruments = list(manifest.instruments)
        changed.append("инструменты")
    if row.venue != manifest.venue:
        row.venue = manifest.venue
        changed.append("площадка")
    if manifest.timeframe and row.timeframe != manifest.timeframe:
        row.timeframe = manifest.timeframe
        changed.append("таймфрейм")
    if changed:
        session.flush()
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="Регистрация стратегии с кодом в реестре базы")
    ap.add_argument("--id", action="append", default=[], help="id стратегии (можно повторить)")
    ap.add_argument("--all", action="store_true", help="все стратегии из реестра кода")
    ap.add_argument("--list", action="store_true", help="показать, что умеет код, и выйти")
    ap.add_argument("--venue", help="другая площадка (например bitstamp — ряд с 2011 года)")
    ap.add_argument(
        "--update",
        action="store_true",
        help="подтянуть в существующую запись инструменты, площадку и таймфрейм из карточки",
    )
    ap.add_argument(
        "--instrument",
        action="append",
        default=[],
        help="другой инструмент под эту площадку (можно повторить)",
    )
    ap.add_argument(
        "--instruments-file",
        help="файл со списком пар (по одной в строке) — ДИНАМИЧЕСКАЯ вселенная. "
        "Кросс-секционные правила выбирают состав сами на каждом ребалансе, в карточке "
        "стоит только якорь; список живёт в записи реестра, поэтому суффикс здесь не нужен",
    )
    ap.add_argument(
        "--slug-suffix",
        help="хвост к слагу: id собирается как <ветка>-<источник>-<слаг>, "
        "без своего слага запись просто столкнётся с исходной",
    )
    ap.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="ИМЯ=ЗНАЧЕНИЕ",
        help="параметр правил поверх карточки, например risk_unit_pct=0.25 "
        "(так проверяют размер позиции, не трогая исходную запись)",
    )
    args = ap.parse_args()
    if (args.venue or args.instrument or args.param) and not args.slug_suffix:
        print(
            "с --venue/--instrument/--param нужен --slug-suffix: иначе id совпадёт с исходной "
            "стратегией и запись будет отклонена как дубль",
            file=sys.stderr,
        )
        return 2
    if args.slug_suffix and (args.all or len(args.id) != 1):
        print("--slug-suffix применим ровно к одной стратегии (--id)", file=sys.stderr)
        return 2
    universe: list[str] = []
    if args.instruments_file:
        if args.all or len(args.id) != 1:
            print("--instruments-file применим ровно к одной стратегии (--id)", file=sys.stderr)
            return 2
        universe = [
            line.strip()
            for line in Path(args.instruments_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not universe:
            print(f"{args.instruments_file}: ни одной пары", file=sys.stderr)
            return 2

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
    added = skipped = updated = 0
    with session_scope(factory) as session:
        registry = Registry(session)
        for sid in wanted:
            manifest = code_registry.manifest(sid)
            if args.slug_suffix:
                # Те же правила, но другая площадка, другой ряд или другие параметры: манифест
                # копируется с новым слагом, иначе id совпадёт с исходной записью.
                update: dict[str, object] = {
                    "slug": f"{manifest.slug}-{args.slug_suffix}",
                    # id копии реестру кода неизвестен, поэтому в параметрах остаётся ссылка
                    # на исходные правила: по ней `ops.measure` соберёт стратегию (иначе замер
                    # ответит «нет кода правил» — ровно как на шаблон из examples/).
                    "params": {**manifest.params, **_params(args.param), "code_id": sid},
                }
                if args.venue:
                    update["venue"] = args.venue
                if args.instrument or universe:
                    update["instruments"] = list(args.instrument) or universe
                stop_pct = _params(args.param).get("stop_loss_pct")
                if stop_pct:
                    # Стоп живёт не в params, а отдельным полем манифеста: без этой строки
                    # параметр записался бы, а стратегия осталась со старым стопом.
                    update["stop"] = StopSpec(max_dd_pct=Decimal(str(stop_pct)))
                manifest = manifest.model_copy(update=update)
            elif universe:
                # Вселенная без суффикса: id остаётся исходным, потому что это НЕ вариант
                # правил, а их рабочий состав. В карточке лежит только якорь (BTC для
                # фильтра режима), торговать по нему одному стратегия не собиралась.
                manifest = manifest.model_copy(update={"instruments": universe})
                print(f"  вселенная: {len(universe)} пар")
            try:
                row = registry.add(manifest)
            except DuplicateStrategy:
                if args.update:
                    changed = _sync(registry, session, manifest)
                    if changed:
                        updated += 1
                        print(f"  обновлена: {manifest_id(manifest)} — {', '.join(changed)}")
                    else:
                        skipped += 1
                        print(f"  уже совпадает: {sid}")
                    continue
                skipped += 1
                print(f"  уже в реестре: {sid}")
                continue
            added += 1
            print(f"  добавлена: {row.id} [{row.status}, ступень {row.rung}]")
    print(f"добавлено {added}, обновлено {updated}, пропущено {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
