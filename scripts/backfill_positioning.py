#!/usr/bin/env python
"""Метрики позиционирования из архива Binance: открытый интерес, лонг/шорт, поток тейкеров.

Зачем это отдельно от ставок фандинга. Ставка говорит, сколько ПЛАТЯТ за плечо;
открытый интерес — сколько его НАБРАЛИ. Проверка 11.09.2026: связь изменения открытого
интереса со ставкой равна −0.03, то есть её нет. Это первый источник, не дублирующий
премию за плечо, — ради него сбор и делается.

Почему дорого: помесячных файлов у метрик НЕТ, только посуточные (289 строк по пять
минут). Год истории одного символа — 365 запросов. Поэтому символов берём немного:
сигнал рыночный, BTC и ETH его описывают.

    python scripts/backfill_positioning.py --symbols BTC/USDT:USDT --days 400
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.positioning import Positioning, PositioningStore  # noqa: E402

ARCHIVE = "https://data.binance.vision/data/futures/um/daily/metrics"


def archive_symbol(instrument: str) -> str:
    base, rest = instrument.split("/")
    return f"{base}{rest.partition(':')[0]}".upper()


_SCALE = Decimal("0.000000000001")  # 12 знаков — как в схеме хранилища


def _dec(raw: str) -> Decimal:
    """Число из файла, приведённое к точности схемы.

    Биржа пишет значения с 16 знаками после запятой (`5529872248.0063800000000000`),
    а в партиции их 12. Без явного округления pyarrow отказывается писать вовсе
    («Rescaling Decimal value would cause data loss») — и правильно делает: молча
    терять знаки у денег нельзя. Здесь это метрики, а не деньги, и 12 знаков с запасом.
    """
    try:
        value = Decimal(raw) if raw not in ("", "\\N") else Decimal(0)
    except InvalidOperation:
        return Decimal(0)
    return value.quantize(_SCALE)


def parse_day(data: bytes) -> list[Positioning]:
    """Разбор посуточного файла. Колонки проверены на живом файле 05.03.2024."""
    out: list[Positioning] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        text = z.read(next(n for n in z.namelist() if n.endswith(".csv"))).decode("utf-8")
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 8 or not parts[0][:4].isdigit():
            continue  # заголовок
        ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        out.append(
            Positioning(
                ts=ts,
                open_interest=_dec(parts[2]),
                open_interest_value=_dec(parts[3]),
                top_accounts_ratio=_dec(parts[4]),
                top_positions_ratio=_dec(parts[5]),
                accounts_ratio=_dec(parts[6]),
                taker_ratio=_dec(parts[7]),
            )
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="BTC/USDT:USDT,ETH/USDT:USDT")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    store = PositioningStore(args.root)
    names = [s.strip() for s in args.symbols.split(",") if s.strip()]
    today = datetime.now(UTC).date()
    for name in names:
        symbol = archive_symbol(name)
        written = missing = 0
        batch: list[Positioning] = []
        with httpx.Client(timeout=30) as client:
            for i in range(args.days, 0, -1):
                day = today - timedelta(days=i)
                r = client.get(f"{ARCHIVE}/{symbol}/{symbol}-metrics-{day:%Y-%m-%d}.zip")
                if r.status_code != 200:
                    missing += 1
                    continue
                batch += parse_day(r.content)
                # Пишем помесячно, а не в конце: сбор идёт часами, и обрыв не должен
                # обнулять работу.
                if len(batch) > 8000:
                    written += store.write("binance", name, batch)
                    batch = []
                if i % 100 == 0:
                    print(f"  {name}: осталось {i} дней, записано {written}", flush=True)
        written += store.write("binance", name, batch)
        print(f"{name}: {written} снимков, дней без данных {missing}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
