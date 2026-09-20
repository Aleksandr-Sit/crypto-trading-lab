#!/usr/bin/env python
"""Внешние суточные ряды для яруса 2 очереди замеров — сбор в `data/external/*.csv`.

Три ряда, у каждого открытый источник без ключа и без регистрации:

* `fng`      — индекс страха и жадности (alternative.me, с 01.02.2018). Гипотеза E1:
  экстремумы настроений связаны с доходностью вперёд, то есть индекс работает
  КОНТР-сигналом. Ряд один на весь рынок, инструмент сравнения — BTC.
* `hashrate` — хешрейт сети Bitcoin (blockchain.info, с 2009). Гипотеза E2:
  «капитуляция майнеров» — пересечение скользящих хешрейта отмечает дно.
* `dvol`     — индекс ожидаемой волатильности BTC (Deribit, примерно с 2021).
  Гипотеза E3: это не сигнал, а УСЛОВИЕ — работает ли что-то только при низкой воле.

Формат один на все ряды: `date,value` с суточным шагом, UTC. Проверять их следует
`signal_check.py --csv data/external/<имя>.csv` — тем же инструментом и той же
дисциплиной, что и показатели позиционирования, иначе выводы несопоставимы.

Ряд копится ВПЕРЁД: повторный запуск дописывает новые дни, старые не переписывает.

    python scripts/external_series.py --series fng --root /app/data
    python scripts/external_series.py --series all --root /app/data
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean

import httpx

SERIES = ("fng", "hashrate", "dvol")
TIMEOUT = httpx.Timeout(30.0)


def fetch_fng() -> dict[str, float]:
    """Индекс страха и жадности: 0 — крайний страх, 100 — крайняя жадность."""
    r = httpx.get("https://api.alternative.me/fng/", params={"limit": 0, "format": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    out: dict[str, float] = {}
    for row in r.json().get("data", []):
        ts = datetime.fromtimestamp(int(row["timestamp"]), UTC)
        out[ts.date().isoformat()] = float(row["value"])
    return out


def fetch_hashrate() -> dict[str, float]:
    """Хешрейт сети, TH/s, суточные точки."""
    r = httpx.get(
        "https://api.blockchain.info/charts/hash-rate",
        params={"timespan": "all", "format": "json", "sampled": "false"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out: dict[str, float] = {}
    for p in r.json().get("values", []):
        ts = datetime.fromtimestamp(int(p["x"]), UTC)
        out[ts.date().isoformat()] = float(p["y"])
    return out


def fetch_dvol() -> dict[str, float]:
    """DVOL Deribit: ожидаемая волатильность BTC на 30 дней, закрытие суток.

    Источник отдаёт ограниченное окно за вызов, поэтому идём кусками по 200 суток
    назад от сегодня и останавливаемся, когда данные кончились.
    """
    out: dict[str, float] = {}
    end = datetime.now(UTC)
    empty_rounds = 0
    while empty_rounds < 2 and end.year >= 2020:
        start = end - timedelta(days=200)
        r = httpx.get(
            "https://www.deribit.com/api/v2/public/get_volatility_index_data",
            params={
                "currency": "BTC",
                "start_timestamp": int(start.timestamp() * 1000),
                "end_timestamp": int(end.timestamp() * 1000),
                "resolution": "43200",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json().get("result", {}).get("data", [])
        if not rows:
            empty_rounds += 1
        else:
            empty_rounds = 0
            for row in rows:
                ts = datetime.fromtimestamp(int(row[0]) / 1000, UTC)
                out[ts.date().isoformat()] = float(row[4])
        end = start
    return out


def ribbon(series: dict[str, float], fast: int = 30, slow: int = 60) -> dict[str, float]:
    """Производный ряд «hash ribbons»: быстрая средняя против медленной, в процентах.

    Сырой хешрейт хвостами мерить бессмысленно — он монотонно растёт, и «нижние 10%»
    это просто 2009 год, а «верхние» — 2026. Торгуют не уровень, а ПЕРЕСЕЧЕНИЕ средних:
    быстрая ниже медленной — майнеры выключают технику, капитуляция; возврат выше —
    сигнал на покупку. Значение ниже нуля и есть «идёт капитуляция».
    """
    keys = sorted(series)
    values = [series[k] for k in keys]
    out: dict[str, float] = {}
    for i in range(slow, len(keys)):
        ma_fast = fmean(values[i - fast : i])
        ma_slow = fmean(values[i - slow : i])
        if ma_slow > 0:
            out[keys[i]] = (ma_fast / ma_slow - 1) * 100
    return out


# Производные ряды: из какого сырого что считается. Объявлено ПОСЛЕ функций —
# словарь на уровне модуля вычисляется при импорте, и ссылка вперёд упала бы.
DERIVED = {"hashrate": ("hashrate_ribbon", ribbon)}


def store(root: Path, name: str, fresh: dict[str, float]) -> tuple[int, int, str, str]:
    """Дописать ряд, не переписывая уже собранное. Возвращает (было, стало, первый, последний)."""
    path = root / "external" / f"{name}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    have: dict[str, float] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                have[row["date"]] = float(row["value"])
    before = len(have)
    have.update(fresh)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "value"])
        for d in sorted(have):
            w.writerow([d, have[d]])
    keys = sorted(have)
    return before, len(have), keys[0] if keys else "-", keys[-1] if keys else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default="all", choices=(*SERIES, "all"))
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    wanted = SERIES if args.series == "all" else (args.series,)
    fetchers = {"fng": fetch_fng, "hashrate": fetch_hashrate, "dvol": fetch_dvol}
    failed = 0
    for name in wanted:
        try:
            fresh = fetchers[name]()
        except Exception as exc:  # источник открытый и без гарантий — падать целиком незачем
            print(f"{name}: источник не ответил — {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if not fresh:
            print(f"{name}: источник ответил пустым рядом")
            failed += 1
            continue
        before, after, first, last = store(root, name, fresh)
        print(f"{name}: {after} суток ({first} — {last}), новых {after - before}")
        if name in DERIVED:
            dname, fn = DERIVED[name]
            _, d_after, d_first, d_last = store(root, dname, fn(fresh))
            print(f"{dname}: {d_after} суток ({d_first} — {d_last})")
    return 1 if failed == len(wanted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
