#!/usr/bin/env python
"""Эффект листинга (D1): что происходит с монетой после первого дня торгов на Binance.

Литература (Blockchain Research Lab, 327 листингов): +14.7% накопленной аномальной
доходности К дню листинга и отрицательная доходность ПОСЛЕ. Свежие данные говорят
об обратном знаке ещё жёстче: из листингов Binance 2025 года в плюсе осталось 11%.
Оба варианта торгуемы — покупать нельзя, но можно не покупать или шортить перп.

Дата листинга берётся из архива: первая дневная свеча ряда. Вселенная из архива
листингов содержит и умершие пары — иначе выживших было бы больше, чем есть.

Что считается:

* доходность от закрытия ПЕРВОГО полного дня (день листинга сам не торгуем — цена
  открытия там условна) на горизонты 3, 7, 30, 90 дней;
* то же у BTC за те же дни — ориентир обязателен: листинги идут волнами в бычьи
  фазы, и без него «эффект листинга» окажется эффектом рынка;
* **наблюдение — листинг, но шум — по МЕСЯЦАМ**: листинги одной недели идут в один
  рынок и не независимы;
* разбивка по годам — эффект менял знак, и это надо увидеть, а не усреднить.

    python scripts/listing_effect.py --root /app/data
    python scripts/listing_effect.py --from-year 2023 --root /app/data
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from math import sqrt
from pathlib import Path
from statistics import fmean, median, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

HORIZONS = (3, 7, 30, 90)
# Установившиеся альты для КОНТРОЛЯ. Сравнение с одним BTC недостаточно: альты проигрывают
# биткойну и без всяких листингов, и «эффект листинга» может оказаться эффектом альта.
BASKET = (
    "ETH/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT", "LTC/USDT",
    "TRX/USDT", "LINK/USDT", "ATOM/USDT", "ETC/USDT", "XLM/USDT", "VET/USDT",
    "FIL/USDT", "EOS/USDT", "ALGO/USDT", "NEO/USDT",
)


def first_days(cs: CandleStore, name: str, need: int) -> list[tuple[date, float, float]]:
    """Первые `need` дневных баров ряда: дата, закрытие и максимум.

    Максимум нужен для стопа: шорт выносит внутри дня, а не по закрытию.
    """
    rows = cs.query(
        "select ts, close::DOUBLE as c, high::DOUBLE as h "
        f"from {{candles}} order by ts limit {need}",
        "binance",
        name,
        "1d",
    )
    return [
        (r["ts"].date(), float(r["c"]), float(r["h"] or r["c"]))
        for r in rows
        if r["c"] and r["c"] > 0
    ]


def perp_onboard_dates() -> dict[str, date]:
    """Когда Binance запустила бессрочный контракт по каждой монете.

    Берётся у биржи: в нашем хранилище перпы собраны лишь по ликвидной сотне,
    и их отсутствие там говорит о полноте сбора, а не о торгуемости.
    """
    import httpx

    try:
        r = httpx.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=60)
        r.raise_for_status()
    except Exception as err:  # noqa: BLE001 — без дат просто не будет колонки
        print(f"даты запуска перпов недоступны: {type(err).__name__}: {err}")
        return {}
    out: dict[str, date] = {}
    for s in r.json().get("symbols", []):
        if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
            continue
        stamp = s.get("onboardDate")
        if stamp:
            out[s["baseAsset"]] = datetime.fromtimestamp(stamp / 1000, UTC).date()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe", default="universe-1d.txt")
    ap.add_argument("--from-year", type=int, default=2021)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    cs = CandleStore(root)
    names = [ln.strip() for ln in (root / args.universe).read_text().splitlines() if ln.strip()]
    def series(name: str) -> dict[date, float]:
        return {
            r["ts"].date(): float(r["c"])
            for r in cs.query(
                "select ts, close::DOUBLE as c from {candles} order by ts", "binance", name, "1d"
            )
            if r["c"] and r["c"] > 0
        }

    btc = series("BTC/USDT")
    basket = {n: series(n) for n in BASKET}
    basket = {n: s for n, s in basket.items() if len(s) > 500}
    longest = max(HORIZONS)

    def basket_move(d0: date, d1: date) -> float | None:
        """Медианная доходность корзины установившихся альтов за те же дни."""
        vals = [
            (s[d1] / s[d0] - 1) * 100
            for s in basket.values()
            if d0 in s and d1 in s and s[d0] > 0
        ]
        return median(vals) if len(vals) >= 5 else None

    # По месяцам листинга: несколько листингов одного месяца — одно наблюдение.
    by_month: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_year: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    vs_basket: dict[int, list[float]] = defaultdict(list)
    raw: dict[int, list[float]] = defaultdict(list)
    # Для проверки шорта нужен ПУТЬ цены, а не только точка выхода: стоп срабатывает
    # по дороге. Держим первые 30 дней после входа.
    shorts: list[tuple[date, float, list[tuple[date, float, float]], bool]] = []
    listings = 0
    skipped_btc = 0
    onboard = perp_onboard_dates()
    perp_gap: list[int] = []
    no_perp = 0
    for name in names:
        days = first_days(cs, name, longest + 3)
        # Требовать полные 92 дня истории — значит выбросить монеты, умершие за три
        # месяца, а это лучшие кандидаты на шорт: отбор по выживанию работал бы ПРОТИВ
        # нас. Минимум — пять дней, дальше каждый горизонт проверяет себя сам.
        if len(days) < 5:
            continue
        d0, p0, _ = days[1]  # закрытие первого ПОЛНОГО дня
        if d0.year < args.from_year:
            continue
        if d0 not in btc:
            skipped_btc += 1
            continue
        listings += 1
        base = name.split("/")[0]
        tradable = base in onboard and onboard[base] <= d0
        if len(days) >= 32:
            shorts.append((d0, p0, days[2:32], tradable))
        # Шортить можно только перп. Даты запуска берутся у БИРЖИ, а не из нашего
        # хранилища: перпы мы собирали лишь по ликвидной сотне, и «перпа нет»
        # означало бы дыру в сборе, а не отсутствие инструмента.
        if base in onboard:
            perp_gap.append((onboard[base] - days[0][0]).days)
        else:
            no_perp += 1
        key = d0.year * 100 + d0.month
        for h in HORIZONS:
            if len(days) < 2 + h:
                continue
            d1, p1, _ = days[1 + h]
            coin = (p1 / p0 - 1) * 100
            raw[h].append(coin)
            b1 = btc.get(d1)
            if b1 is None:
                continue
            excess = coin - (b1 / btc[d0] - 1) * 100
            by_month[h][key].append(excess)
            by_year[h][d0.year].append(excess)
            alt = basket_move(d0, d1)
            if alt is not None:
                vs_basket[h].append(coin - alt)

    print(f"листингов с {args.from_year}: {listings} (без ориентира BTC: {skipped_btc})")
    print(f"шортить есть чем: перп у {len(perp_gap)} монет, нет у {no_perp}")
    if perp_gap:
        same = sum(1 for g in perp_gap if abs(g) <= 3)
        within = sum(1 for g in perp_gap if g <= 30)
        print(
            f"  запущен в те же дни у {same}, в первый месяц у {within}; "
            f"медианная задержка {median(perp_gap):+.0f} дн"
        )
    print(f"\n{'горизонт':10}{'сверх BTC, средняя':>20}{'медиана':>10}{'шум (2σ по мес.)':>18}"
          f"{'доля в плюсе':>14}{'месяцев':>9}")
    for h in HORIZONS:
        months = by_month[h]
        if not months:
            continue
        per_month = [fmean(v) for v in months.values()]
        all_vals = [x for v in months.values() for x in v]
        se = stdev(per_month) / sqrt(len(per_month)) if len(per_month) > 1 else 0.0
        up = sum(1 for x in all_vals if x > 0) / len(all_vals) * 100
        print(
            f"{h:>4} дн   {fmean(all_vals):>19.2f}%{median(all_vals):>9.2f}%"
            f"{2 * se:>18.2f}{up:>13.0f}%{len(per_month):>9}"
        )

    # Контроль: против корзины установившихся альтов, а не против BTC.
    print(f"\nКОНТРОЛЬ: против корзины из {len(basket)} установившихся альтов")
    print(f"{'горизонт':10}{'средняя':>10}{'медиана':>10}{'в плюсе':>10}{'наблюдений':>13}")
    for h in HORIZONS:
        v = vs_basket[h]
        if not v:
            continue
        up = sum(1 for x in v if x > 0) / len(v) * 100
        print(f"{h:>4} дн   {fmean(v):>9.2f}%{median(v):>9.2f}%{up:>9.0f}%{len(v):>13}")

    # Хвост решает для ШОРТА: медиана может быть минус двадцать, а один листинг
    # с плюс тысячей съест всё. Проценты распределения важнее средней.
    print(f"\nРАСПРЕДЕЛЕНИЕ доходности самой монеты (без ориентира), горизонт 30 дн")
    vals = sorted(raw[30])
    if vals:
        qs = [(0.05, "5%"), (0.25, "25%"), (0.5, "медиана"), (0.75, "75%"), (0.95, "95%")]
        line = "  ".join(f"{lab}: {vals[int(len(vals) * q)]:+.0f}%" for q, lab in qs)
        print(f"  {line}   максимум: {vals[-1]:+.0f}%")

    # Главный вопрос: переживает ли ШОРТ свой правый хвост. Стоп срабатывает внутри дня
    # по максимуму, а не по закрытию, — так это и происходит на бирже.
    print("\nШОРТ со стопом, горизонт 30 дн (вход по закрытию первого полного дня)")
    head = f"{'стоп':10}{'средняя':>10}{'шум (2σ)':>11}{'медиана':>10}"
    print(head + f"{'вынесло':>9}{'худший':>9}{'в плюсе':>9}")
    per_year_short: dict[int, list[float]] = defaultdict(list)
    for stop_pct in (25, 50, 100, 200, None):
        results = []
        by_mon: dict[int, list[float]] = defaultdict(list)
        stopped = 0
        for d0, p0, path, _tradable in shorts:
            hit_stop = None
            if stop_pct is not None:
                limit = p0 * (1 + stop_pct / 100)
                for _, _, high in path:
                    if high >= limit:
                        hit_stop = -stop_pct
                        break
            got = hit_stop if hit_stop is not None else -(path[-1][1] / p0 - 1) * 100
            if hit_stop is not None:
                stopped += 1
            results.append(got)
            by_mon[d0.year * 100 + d0.month].append(got)
            if stop_pct == 100:
                per_year_short[d0.year].append(got)
        if not results:
            continue
        label = f"+{stop_pct}%" if stop_pct else "без стопа"
        up = sum(1 for x in results if x > 0) / len(results) * 100
        # Шум по МЕСЯЦАМ: листинги одного месяца идут в один рынок и не независимы.
        per_mon = [fmean(v) for v in by_mon.values()]
        se = stdev(per_mon) / sqrt(len(per_mon)) if len(per_mon) > 1 else 0.0
        print(
            f"{label:10}{fmean(results):>9.1f}%{2 * se:>11.1f}{median(results):>9.1f}%"
            f"{stopped * 100 / len(results):>8.0f}%{min(results):>8.0f}%{up:>8.0f}%"
        )

    # Решающий срез: только то, что РЕАЛЬНО можно было шортить — перп существовал
    # на момент входа. Если эффект живёт лишь там, где инструмента не было,
    # он не торгуем, каким бы сильным ни выглядел.
    real = [s for s in shorts if s[3]]
    print(f"\nТОЛЬКО ТОРГУЕМЫЕ (перп уже существовал): {len(real)} из {len(shorts)}")
    if len(real) >= 30:
        print(f"{'стоп':10}{'средняя':>10}{'шум (2σ)':>11}{'медиана':>10}{'в плюсе':>9}")
        for stop_pct in (50, 100, None):
            res = []
            mon: dict[int, list[float]] = defaultdict(list)
            for d0, p0, path, _t in real:
                hit = None
                if stop_pct is not None:
                    limit = p0 * (1 + stop_pct / 100)
                    if any(high >= limit for _, _, high in path):
                        hit = -stop_pct
                got = hit if hit is not None else -(path[-1][1] / p0 - 1) * 100
                res.append(got)
                mon[d0.year * 100 + d0.month].append(got)
            per_mon = [fmean(v) for v in mon.values()]
            se = stdev(per_mon) / sqrt(len(per_mon)) if len(per_mon) > 1 else 0.0
            up = sum(1 for x in res if x > 0) / len(res) * 100
            label = f"+{stop_pct}%" if stop_pct else "без стопа"
            print(
                f"{label:10}{fmean(res):>9.1f}%{2 * se:>11.1f}{median(res):>9.1f}%{up:>8.0f}%"
            )

    print("\nШОРТ со стопом +100% ПО ГОДАМ")
    print(f"{'год':7}{'листингов':>11}{'средняя':>10}{'медиана':>10}{'в плюсе':>9}")
    for year in sorted(per_year_short):
        v = per_year_short[year]
        up = sum(1 for x in v if x > 0) / len(v) * 100
        print(f"{year:<7}{len(v):>11}{fmean(v):>9.1f}%{median(v):>9.1f}%{up:>8.0f}%")

    print(f"\nПО ГОДАМ, сверх BTC, горизонт 30 дн")
    print(f"{'год':7}{'листингов':>11}{'средняя':>10}{'медиана':>10}{'в плюсе':>9}")
    for year in sorted(by_year[30]):
        vals = by_year[30][year]
        up = sum(1 for x in vals if x > 0) / len(vals) * 100
        print(f"{year:<7}{len(vals):>11}{fmean(vals):>9.1f}%{median(vals):>9.1f}%{up:>8.0f}%")
    print(
        "\nЧитать так: медиана важнее средней — один листинг с +2000% тянет среднюю,\n"
        "а купить его заранее было нельзя. Отрицательная медиана при доле в плюсе\n"
        "ниже 40% — это торгуемо в обратную сторону, если есть перп для шорта."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
