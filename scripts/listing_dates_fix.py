#!/usr/bin/env python
"""Даты состава шорта листингов: день ЛИСТИНГА вместо первого полного дня, с перемером.

**Что было сломано (найдено 28.09.2026).** `listing_universe.py` писал в `listing_dates`
поле `entry` из `listing_effect.py` — первый ПОЛНЫЙ день торгов. Стратегия же считает эту
дату днём листинга и входит на закрытии второго бара от неё. Итог: вход на сутки позже
карточки (AERO: спот с 17.07, вход по бару 19.07 вместо 18.07). Замер 13.09 (`passed`,
просадка 4.94% при лимите 5%) мерил правило «второй полный день».

**Решение владельца 28.09 — до перемера:** правило входа как в карточке, на закрытии
первого полного дня; вердикт перемера принимается любым.

Скрипт делает по порядку:

1. ОТЧЁТ (всегда, только чтение): по каждому инструменту состава — первый бар спота из
   хранилища (день листинга, как в исследовании: `days[0]` у `listing_effect.py`), сколько
   нынешних дат равны «листинг + 1», и в какой день стратегия войдёт по счёту баров перпа
   ДО и ПОСЛЕ исправления — против дня карточки (второй бар спота, `days[1]`). Там же —
   листинги раньше начала окна: прогрева у движка нет, и счёт баров идёт с границы окна;
2. «ДО» (`--measure ID`): замер на окне последнего замера `ok`, без сохранения. Обязан
   воспроизвести сохранённый снимок, иначе с 13.09 изменилось ещё что-то;
3. ЗАПИСЬ (`--apply`): даты всех записей с `listing_dates` (снятые с учёта — тоже, для
   истории) = день листинга. Отказ, если хоть у одного инструмента нет спотового ряда;
4. «ПОСЛЕ» (`--measure ID` вместе с `--apply`): тот же замер, сохраняется обычным снимком
   (`--no-save` — не сохранять).

    python scripts/listing_dates_fix.py                          # только отчёт
    python scripts/listing_dates_fix.py --measure cex-perp-paper-listing-fade-short
    python scripts/listing_dates_fix.py --apply --measure cex-perp-paper-listing-fade-short
    # на новом коде стратегии, когда даты уже записаны: только «после», со снимком
    python scripts/listing_dates_fix.py --apply --skip-before --measure …

С рабочей машины: `bash scripts/lab-oneoff.sh scripts/listing_dates_fix.py -- [ключи]`
(хранилище `LAB_DATA_ROOT=/app/data` скрипт запуска ставит сам). Замер 194 инструментов
на пяти годах дневок — долгий: с `--measure` — через `run-detached.sh`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from costs_remeasure import last_ok, summary  # noqa: E402

from lab.core.measure.runner import _row_to_measurement  # noqa: E402
from lab.data.store import CandleStore  # noqa: E402
from lab.db import make_engine, make_session_factory, session_scope  # noqa: E402
from lab.db.models import StrategyRow  # noqa: E402
from lab.ops.measure import data_root, make_measure  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VENUE = "binance"
DATES_PARAM = "listing_dates"
DAY = timedelta(days=1)


def dates_of(row: StrategyRow) -> dict[str, date]:
    """`listing_dates` записи — строкой JSON или словарём, как их пишут лента и вселенная."""
    raw = (row.params_json or {}).get(DATES_PARAM)
    if isinstance(raw, str):
        raw = json.loads(raw)
    return {str(k): date.fromisoformat(str(v)) for k, v in (raw or {}).items()}


def listing_rows(session) -> list[StrategyRow]:
    return [
        row
        for row in session.query(StrategyRow).order_by(StrategyRow.id).all()
        if row.venue == VENUE and DATES_PARAM in (row.params_json or {})
    ]


def bar_days(
    store: CandleStore, name: str, since: date | None = None, limit: int = 2
) -> list[date]:
    """Первые дни ряда с ненулевым закрытием — тот же отбор, что `first_days` исследования."""
    where = "close > 0"
    params: list = []
    if since is not None:
        where += " and ts >= ?"
        params.append(datetime.combine(since, datetime.min.time()))
    rows = store.query(
        f"select ts from {{candles}} where {where} order by ts limit {limit}",
        VENUE,
        name,
        "1d",
        params=params,
    )
    return [r["ts"].date() for r in rows]


def entry_by_count(perp: list[date], listed: date, entry_bar: int = 1) -> date | None:
    """День входа по правилу стратегии: `entry_bar + 1`-й бар ПОТОКА с даты `listed`.

    Повторяет `ListingFadeShortStrategy.on_bar`: счёт идёт по барам перпа, пришедшим в
    окне, а не по календарю. `perp` — бары перпа от начала окна.
    """
    n = 0
    for d in perp:
        if d >= listed:
            n += 1
            if n == entry_bar + 1:
                return d
    return None


def report(store: CandleStore, rows: list[StrategyRow], window_start: date) -> dict[str, date]:
    """Печатает сверку и возвращает исправленные даты «инструмент → день листинга»."""
    union: dict[str, date] = {}
    for row in rows:
        for name, stamp in dates_of(row).items():
            if union.setdefault(name, stamp) != stamp:
                print(f"  ! {name}: в записях разные даты ({union[name]} и {stamp})")
    print(f"записей с {DATES_PARAM}: {len(rows)} ({', '.join(r.id for r in rows)})")
    print(f"инструментов (объединение): {len(union)}; начало окна {window_start}")

    fixed: dict[str, date] = {}
    no_spot: list[str] = []
    shift = Counter()
    before_vs_card = Counter()
    after_vs_card = Counter()
    odd: list[str] = []
    for name, old in sorted(union.items()):
        spot = bar_days(store, name.removesuffix(":USDT"))
        if len(spot) < 2:
            no_spot.append(name)
            continue
        listed, card = spot
        fixed[name] = listed
        shift[(old - listed).days] += 1
        perp = bar_days(store, name, since=max(window_start, listed), limit=6)
        for label, stamp, tally in (("до", old, before_vs_card), ("после", listed, after_vs_card)):
            got = entry_by_count(perp, stamp)
            delta = None if got is None else (got - card).days
            tally[delta] += 1
            if label == "после" and delta != 0:
                first_perp = perp[0] if perp else None
                odd.append(
                    f"  {name:26} листинг {listed}, день карточки {card}, "
                    f"первый бар перпа в окне {first_perp}, вход по счёту {got}"
                )

    print(f"\nспотового ряда нет (дату не исправить): {len(no_spot)}")
    for name in no_spot[:20]:
        print(f"  {name}")
    print("\nнынешняя дата минус день листинга, дней → инструментов:")
    for days_, n in sorted(shift.items()):
        print(f"  {days_:+d}: {n}")
    print(f"  (совпало с «entry − 1»: {shift.get(1, 0)} из {sum(shift.values())})")
    for label, tally in (("ДО", before_vs_card), ("ПОСЛЕ", after_vs_card)):
        parts = ", ".join(
            f"{'нет входа' if k is None else f'{k:+d} дн'}: {v}"
            for k, v in sorted(tally.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
        )
        print(f"вход по счёту баров против дня карточки, {label}: {parts}")
    early = sorted(n for n, d in fixed.items() if d < window_start)
    names = f" — {', '.join(early[:10])}" if early else ""
    print(f"\nлистинг раньше начала окна: {len(early)}{names}")
    if odd:
        print(f"\nпосле исправления вход НЕ в день карточки: {len(odd)}")
        for line in odd[:30]:
            print(line)
    return fixed if not no_spot else {}


def apply(scope, fixed: dict[str, date]) -> None:
    encoded = {k: v.isoformat() for k, v in fixed.items()}
    with scope() as session:
        for row in listing_rows(session):
            current = dates_of(row)
            missing = sorted(set(current) - set(fixed))
            if missing:
                raise SystemExit(f"{row.id}: нет исправленной даты для {missing[:5]}")
            new = {k: encoded[k] for k in current}
            changed = sum(1 for k, v in current.items() if v.isoformat() != new[k])
            # Строкой, как пишут `listing_universe.py` и лента: формат записи не меняется.
            row.params_json = {
                **(row.params_json or {}),
                DATES_PARAM: json.dumps(new, indent=1, sort_keys=True),
            }
            print(f"  {row.id}: исправлено дат {changed} из {len(current)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--measure", action="append", default=[], help="id стратегии для перемера")
    ap.add_argument("--apply", action="store_true", help="записать исправленные даты")
    ap.add_argument("--no-save", action="store_true", help="не сохранять замер «после»")
    ap.add_argument(
        "--skip-before",
        action="store_true",
        help="не считать «до» (повторный прогон на новом коде, когда «до» уже есть)",
    )
    ap.add_argument("--mode", default="backtest")
    args = ap.parse_args()

    factory = make_session_factory(make_engine())

    def scope():
        return session_scope(factory)

    store = CandleStore(data_root())
    with scope() as session:
        rows = listing_rows(session)
        prev = {sid: last_ok(scope, sid, args.mode) for sid in args.measure}
        known = [p for p in prev.values() if p is not None]
        window_start = (
            min(p.window_from for p in known).date() if known else date(2021, 10, 9)
        )
        fixed = report(store, rows, window_start)

    measure = make_measure(scope)
    before = {}
    for sid, p in prev.items():
        if p is None:
            print(f"\n{sid}: нет замера ok в режиме {args.mode}")
            return 2
        window = (p.window_from, p.window_to)
        print(f"\n=== {sid} · окно {window[0]:%d.%m.%Y %H:%M}–{window[1]:%d.%m.%Y %H:%M} "
              f"(как у замера {p.id})", flush=True)
        if args.skip_before:
            continue
        before[sid] = measure(strategy_id=sid, mode=args.mode, window=window, session=None)
        print(f"  «до» посчитан: {before[sid].status}", flush=True)

    if not args.apply:
        for sid, m in before.items():
            print_table(sid, prev[sid], m, None)
        return 0
    if not fixed:
        print("\nзапись отменена: не у всех инструментов есть спотовый ряд")
        return 1
    print("\nзапись дат:")
    apply(scope, fixed)

    worst = 0
    for sid, p in prev.items():
        extra = {"session": None} if args.no_save else {}
        window = (p.window_from, p.window_to)
        after = measure(strategy_id=sid, mode=args.mode, window=window, **extra)
        print(f"  «после» {sid}: {after.status}, снимок {after.id or 'не сохранён'}", flush=True)
        was = before.get(sid)
        if after.status != "ok" or (was is not None and was.status != "ok"):
            worst = 1
        print_table(sid, p, was, after)
    return worst


def print_table(sid: str, prev, before, after) -> None:
    cols = [(f"замер {prev.id}", summary(_row_to_measurement(prev, cached=True)))]
    for name, m in (("до", before), ("после", after)):
        if m is None:
            continue
        if m.status != "ok":
            print(f"  {sid} {name}: {m.status} — {m.reason}")
            continue
        cols.append((name, summary(m)))
    print(f"\n--- {sid}")
    width = max(len(k) for k in cols[0][1])
    print(f"  {'':{width}}  " + "  ".join(f"{name:>22}" for name, _ in cols))
    for key in cols[0][1]:
        print(f"  {key:{width}}  " + "  ".join(f"{c[key]:>22}" for _, c in cols))


if __name__ == "__main__":
    raise SystemExit(main())
