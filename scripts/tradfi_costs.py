#!/usr/bin/env python
"""Легче ли торговать спокойный рынок — вопрос про ОТНОШЕНИЕ хода к издержкам.

Гипотеза владельца: мировые активы (валюты, индексы, металлы) менее волатильны, стратегий
и трейдеров на них больше, значит торговать по книжкам проще. Первая половина верна:
EURUSD ходит на десятые доли процента там, где альткойн ходит на проценты.

Но «легче» определяется не размахом, а **отношением размаха к издержкам**. Стратегия
живёт, если типичный ход больше того, что стоит войти и выйти. Уменьшите и ход, и издержки
вдвое — ничего не изменится; уменьшите только ход — исчезнет всё.

И здесь у CFD есть особенность, которой нет у спота: **плата за перенос через ночь**.
Спот можно держать бесплатно. CFD — нет: брокер финансирует позицию и берёт за это
ставку рынка плюс наценку, каждый день. На горизонте в неделю эта плата обычно больше
спреда, а на горизонте в месяц — больше в разы.

Считается по каждому активу:

* типичный дневной ход (медиана |доходности| и годовая волатильность);
* во сколько дней обходится круг по спреду;
* за сколько дней плата за перенос съедает один дневной ход;
* итог — **сколько дневных ходов стоит удержание позиции неделю**.

Тарифы — допущение, вынесенное в таблицу, а не спрятанное в коде: у разных брокеров
они разные, и менять их надо в одном месте.

    python scripts/tradfi_costs.py --root /app/data
    python scripts/tradfi_costs.py --hold-days 30 --root /app/data
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

# Спред CFD (базисных пунктов за круг) и годовая наценка за перенос сверх базовой ставки.
# Типичные розничные условия 2026 года (Bybit MT5, IC Markets, Pepperstone одного порядка).
# Базовая ставка финансирования принята 4% годовых — та же, что в пороге лаборатории.
BASE_RATE_PCT = 4.0
COSTS = {
    "EURUSD": (1.5, 1.0),
    "USDJPY": (1.5, 1.0),
    "GBPUSD": (2.0, 1.0),
    "AUDUSD": (2.0, 1.0),
    "USDCHF": (2.5, 1.0),
    "USDCAD": (2.5, 1.0),
    "XAUUSD": (3.0, 2.5),
    "XAGUSD": (8.0, 2.5),
    "WTI": (5.0, 2.5),
    "NATGAS": (12.0, 2.5),
    "SPX": (1.5, 2.5),
    "NDX": (2.0, 2.5),
    "DJI": (2.0, 2.5),
    "DAX": (2.0, 2.5),
    "NIKKEI": (5.0, 2.5),
    "FTSE": (3.0, 2.5),
}
# Для сравнения: крипта на бирже. Круга по тейкеру два по 10 б.п.; «перенос» — фандинг,
# который в среднем около 8% годовых и платится ТОЛЬКО за плечо, а не за всю позицию.
CRYPTO = {"BTC/USDT": (20.0, 8.0), "ETH/USDT": (20.0, 8.0)}


def daily_moves(cs: CandleStore, venue: str, name: str, years: int) -> list[float]:
    to = datetime.now(UTC)
    rows = cs.query(
        "select ts, open::DOUBLE as o, close::DOUBLE as c from {candles} "
        "where ts >= ? order by ts",
        venue,
        name,
        "1d",
        params=[datetime(to.year - years, to.month, to.day)],
    )
    out = []
    for r in rows:
        if r["o"] and r["o"] > 0:
            out.append(abs(r["c"] / r["o"] - 1) * 100)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hold-days", type=int, default=7, help="горизонт удержания позиции")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    cs = CandleStore(args.root)
    hold = args.hold_days
    print(f"окно {args.years} лет | горизонт удержания {hold} дн | базовая ставка {BASE_RATE_PCT}%")
    head = f"{'актив':10}{'дней':>7}{'ход за день':>13}{'спред':>9}{'перенос':>10}"
    print(f"\n{head}{'итого б.п.':>12}{'ходов на круг':>15}")

    rows: list[tuple[str, str, float, float, float, int]] = []
    for venue, table in (("yahoo", COSTS), ("binance", CRYPTO)):
        for name, (spread_bps, markup_pct) in table.items():
            moves = daily_moves(cs, venue, name, args.years)
            if len(moves) < 250:
                continue
            move = median(moves)
            # Плата за перенос: (базовая ставка + наценка) годовых, начисляется ежедневно.
            # У крипты это фандинг, и он платится только за плечо — но допущение то же:
            # позиция открыта целиком, значит и платим за неё целиком.
            carry_bps = (BASE_RATE_PCT + markup_pct) / 365 * hold * 100
            total = spread_bps + carry_bps
            rows.append((venue, name, move, spread_bps, carry_bps, len(moves)))
            print(
                f"{name:10}{len(moves):>7}{move:>12.3f}%{spread_bps:>9.1f}{carry_bps:>10.1f}"
                f"{total:>12.1f}{total / (move * 100):>15.2f}"
            )

    if not rows:
        print("\nрядов нет — сначала scripts/tradfi_import.py")
        return 1
    fx = [r for r in rows if r[0] == "yahoo"]
    cr = [r for r in rows if r[0] == "binance"]
    print(
        "\nЧитать так: последний столбец — сколько ТИПИЧНЫХ ДНЕВНЫХ ХОДОВ стоит один круг\n"
        f"с удержанием {hold} дней. Это и есть «легче или тяжелее»: чем число больше, тем\n"
        "большую долю движения забирают издержки и тем точнее должно быть правило."
    )
    if fx and cr:
        f = median(r[3] + (BASE_RATE_PCT + 2.0) / 365 * hold * 100 for r in fx) / median(
            r[2] * 100 for r in fx
        )
        c = median(r[3] + (BASE_RATE_PCT + 8.0) / 365 * hold * 100 for r in cr) / median(
            r[2] * 100 for r in cr
        )
        print(f"\nмедиана по мировым активам: {f:.2f} ходов на круг")
        print(f"медиана по крипте:          {c:.2f} ходов на круг")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
