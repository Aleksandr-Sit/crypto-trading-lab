#!/usr/bin/env python
"""Кто из публичных трейдеров действительно хорош, а не нарисован.

Лидерборд — машина по производству ложных находок: из сорока пяти тысяч счетов кто-то
показывает выдающийся результат просто по теории вероятностей, и именно он оказывается
наверху. Поэтому здесь НЕ берётся готовый рейтинг биржи — берётся полный список счетов,
и рейтинг строится заново по нашим правилам.

Источник — Hyperliquid: единственная площадка, где данные нельзя нарисовать, потому что
они ончейн. Лидерборд отдаёт все счета с капиталом и оборотом, `portfolio` — кривую
капитала И ОТДЕЛЬНО кривую прибыли. Второе важнее первого: капитал растёт и от пополнений,
и только разделив их, можно посчитать доходность, а не приток денег.

Четыре отсева, каждый убирает свой способ выглядеть лучше, чем ты есть:

1. **Оборачиваемость.** Верхние строки лидерборда занимают маркет-мейкеры: две тысячи
   сделок за четыре часа, оборот в сотни капиталов за месяц. Их преимущество — скорость,
   и скопировать его нельзя в принципе: копия входит с задержкой в те самые сделки,
   на которых они зарабатывают.
2. **Капитал.** «+2000% годовых» на счёте в пятьсот долларов с плечом 50 — это не навык,
   это лотерейный билет, и повторить его нельзя ни на какой значимой сумме.
3. **Бета.** Счёт с нулевым оборотом и прибылью в двадцать миллионов за месяц ничего
   не торгует — он держит лонг, и рынок вырос. Поэтому доходность раскладывается
   на движение рынка и остаток; интересен только остаток.
4. **Устойчивость по времени.** Тот же урок, что стоил нам замера потока тейкеров:
   согласованность внутри одного периода не доказывает ничего.

    python scripts/copytrade_screen.py --stage leaderboard --root /app/data
    python scripts/copytrade_screen.py --stage portfolios --root /app/data
    python scripts/copytrade_screen.py --stage rank --root /app/data
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, date, datetime
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO = "https://api.hyperliquid.xyz/info"
DIR = "copytrade"
WEEKS_MIN = 26  # полгода истории: меньше — не отличить навык от полосы везения
SANE_JUMP = 300.0  # прирост прибыли за период, выше которого это перевод, а не торговля


def _dir(root: Path) -> Path:
    d = root / DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_leaderboard(root: Path, min_capital: float, max_turnover: float) -> int:
    """Полный список счетов → кандидаты, пригодные к копированию."""
    import httpx

    raw = _dir(root) / "leaderboard.json"
    if not raw.exists():
        with httpx.stream("GET", LEADERBOARD, timeout=180) as r:
            r.raise_for_status()
            with raw.open("wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
    rows = json.loads(raw.read_text())["leaderboardRows"]
    out = []
    for r in rows:
        capital = float(r["accountValue"])
        w = dict(r["windowPerformances"])
        volume = float(w["month"]["vlm"])
        if capital < min_capital or volume <= 0:
            continue
        turnover = volume / capital
        if turnover > max_turnover:
            continue
        if float(w["allTime"]["pnl"]) <= 0:
            continue
        out.append(
            {
                "address": r["ethAddress"],
                "capital": capital,
                "turnover": turnover,
                "pnl_all": float(w["allTime"]["pnl"]),
                "pnl_month": float(w["month"]["pnl"]),
            }
        )
    out.sort(key=lambda c: -c["pnl_all"])
    (_dir(root) / "candidates.json").write_text(json.dumps(out, indent=1))
    print(f"счетов всего {len(rows)} → кандидатов {len(out)}")
    print(f"  отсев: капитал ≥ ${min_capital:,.0f}, оборот ≤ {max_turnover}/мес, прибыль > 0")
    return 0


def stage_portfolios(root: Path, limit: int, pause: float) -> int:
    """Кривые капитала и прибыли кандидатов. Уже скачанные не трогаем."""
    import httpx

    cands = json.loads((_dir(root) / "candidates.json").read_text())[:limit]
    path = _dir(root) / "portfolios.jsonl"
    done = set()
    if path.exists():
        done = {json.loads(ln)["address"] for ln in path.read_text().splitlines() if ln.strip()}
    todo = [c for c in cands if c["address"] not in done]
    print(f"кандидатов {len(cands)}, уже есть {len(done)}, качаем {len(todo)}")
    with path.open("a", encoding="utf-8") as fh, httpx.Client(timeout=60) as client:
        for i, c in enumerate(todo, 1):
            try:
                r = client.post(INFO, json={"type": "portfolio", "user": c["address"]})
                r.raise_for_status()
                fh.write(json.dumps({"address": c["address"], "portfolio": r.json()}) + "\n")
            except Exception as err:  # noqa: BLE001 — один адрес не роняет прогон
                print(f"  {c['address']}: {type(err).__name__}")
            if i % 100 == 0:
                print(f"  {i}/{len(todo)}")
            time.sleep(pause)
    return 0


def btc_weekly(root: Path) -> dict[date, float]:
    cs = CandleStore(root)
    rows = cs.query(
        "select ts, close::DOUBLE as close from {candles} order by ts", "binance", "BTC/USDT", "1d"
    )
    return {r["ts"].date(): float(r["close"]) for r in rows if r["close"]}


def returns(
    portfolio: list, prices: dict[date, float], floor: float
) -> tuple[list[float], list[float], list[date]]:
    """Доходности за период — из ПРИБЫЛИ, а не из капитала.

    Капитал растёт и от пополнений; если считать доходность по нему, каждое пополнение
    засчитается в заслугу трейдера. Прибыль в `pnlHistory` накопительная, поэтому берётся
    её приращение, делённое на капитал на начало периода.

    `floor` отсекает младенчество счёта, и без него замер бессмыслен: история начинается
    с момента, когда на счёте лежало десять долларов, и первая же прибыль в сто долларов
    даёт «тысячу процентов за неделю». При перемножении таких периодов получаются
    миллионы процентов годовых — ровно это и вышло в первом прогоне.
    """
    data = dict(portfolio).get("allTime") or {}
    av = data.get("accountValueHistory") or []
    pnl = data.get("pnlHistory") or []
    if len(av) < 10 or len(pnl) != len(av):
        return [], [], []
    # Окно берётся СПЛОШНЫМ с первого момента, когда капитал превысил порог. Пропуск
    # отдельных периодов посреди истории склеивал бы куски разного времени в один шаг
    # и давал бы разрывы, неотличимые от гигантской доходности.
    start = next((i for i in range(len(av)) if float(av[i][1]) >= floor), None)
    if start is None:
        return [], [], []
    mine: list[float] = []
    market: list[float] = []
    stamps: list[date] = []
    for i in range(start + 1, len(av)):
        base = float(av[i - 1][1])
        if base < floor:
            break
        d0 = datetime.fromtimestamp(av[i - 1][0] / 1000, UTC).date()
        d1 = datetime.fromtimestamp(av[i][0] / 1000, UTC).date()
        p0 = prices.get(d0)
        p1 = prices.get(d1)
        if not p0 or not p1:
            continue
        mine.append((float(pnl[i][1]) - float(pnl[i - 1][1])) / base * 100)
        market.append((p1 / p0 - 1) * 100)
        stamps.append(d1)
    # Скачок прибыли больше +300% за период — это не торговля, а движение средств,
    # попавшее в `pnlHistory`. Окно обрывается на нём: то, что было до, измеримо.
    bad = next((i for i, r in enumerate(mine) if r > SANE_JUMP), None)
    if bad is not None:
        return mine[:bad], market[:bad], stamps[:bad]
    return mine, market, stamps


def beta_alpha(mine: list[float], market: list[float]) -> tuple[float, float]:
    """Сколько результата объясняется рынком и что остаётся сверх него."""
    if len(mine) < 10:
        return 0.0, 0.0
    mm, mk = fmean(mine), fmean(market)
    var = sum((x - mk) ** 2 for x in market)
    if var <= 0:
        return 0.0, mm
    beta = sum((x - mk) * (y - mm) for x, y in zip(market, mine, strict=True)) / var
    return beta, mm - beta * mk


def curve(mine: list[float]) -> tuple[float, float]:
    """Итоговый множитель капитала и худшая просадка — без влияния пополнений.

    Множитель обрезается снизу нулём: счёт нельзя потерять дважды, а арифметически
    несколько периодов по −100% дают отрицательный капитал.
    """
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for r in mine:
        equity = max(equity * (1 + r / 100), 1e-9)
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1)
    return equity, worst * 100


def stage_rank(root: Path, top: int, floor: float) -> int:
    prices = btc_weekly(root)
    facts = {c["address"]: c for c in json.loads((_dir(root) / "candidates.json").read_text())}
    path = _dir(root) / "portfolios.jsonl"
    if not path.exists():
        print("сначала стадия portfolios")
        return 1

    scored = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        mine, market, stamps = returns(rec["portfolio"], prices, floor)
        if len(mine) < WEEKS_MIN:
            continue
        span = (stamps[-1] - stamps[0]).days / 365.25
        if span < 0.5:
            continue
        per_year = len(mine) / span
        sd = stdev(mine) if len(mine) > 1 else 0.0
        beta, alpha = beta_alpha(mine, market)
        years: dict[int, list[float]] = {}
        for d, r in zip(stamps, mine, strict=True):
            years.setdefault(d.year, []).append(r)
        good = sum(1 for v in years.values() if sum(v) > 0)
        total, dd = curve(mine)
        scored.append(
            {
                **facts.get(rec["address"], {"address": rec["address"]}),
                "periods": len(mine),
                "years": len(years),
                "good_years": good,
                # Сложный процент, а не средняя за период, помноженная на число периодов:
                # вторая завышает тем сильнее, чем разбросаннее результаты.
                "ret": (total ** (1 / span) - 1) * 100,
                "alpha": alpha * per_year,
                "beta": beta,
                "sharpe": (fmean(mine) / sd * sqrt(per_year)) if sd else 0.0,
                "dd": dd,
                "worst": min(mine),
                "best": max(mine),
            }
        )

    scored.sort(key=lambda s: -s["sharpe"])
    print(f"счетов с историей ≥ {WEEKS_MIN} периодов при капитале ≥ ${floor:,.0f}: {len(scored)}\n")
    head = f"{'адрес':14}{'капитал':>9}{'лет':>5}{'+лет':>6}{'годовых':>10}"
    print(head + f"{'сверх рынка':>13}{'бета':>7}{'дох/риск':>10}{'просадка':>10}")
    for s in scored[:top]:
        print(
            f"{s['address'][:12]:14}{s['capital'] / 1e6:>8.1f}м{s['years']:>5}"
            f"{s['good_years']:>6}{s['ret']:>9.0f}%{s['alpha']:>12.0f}%"
            f"{s['beta']:>7.2f}{s['sharpe']:>10.2f}{s['dd']:>9.0f}%"
        )
    print(
        "\nЧитать так: «доход» — что счёт заработал, «сверх рынка» — что осталось после\n"
        "вычета движения биткойна. Высокий доход при бете около единицы означает лонг\n"
        "с плечом, а не мастерство. Вердикт по годам: полос везения короче года хватает."
    )
    (_dir(root) / "ranked.json").write_text(json.dumps(scored, indent=1))
    return 0


def stage_persist(root: Path, floor: float, min_years: float) -> int:
    """Переносится ли результат из первой половины истории во вторую.

    Это ЕДИНСТВЕННАЯ проверка, отвечающая на вопрос «навык или везение». Список кандидатов
    отобран по прибыли за всё время, поэтому любые их метрики за ту же историю красивы
    по построению — мы измеряем победителей. Здесь ранжирование строится по ПЕРВОЙ половине
    истории, а результат смотрится во ВТОРОЙ, которую отбор не видел.

    Если навык есть, верхняя часть первой половины обгоняет остальных во второй.
    Если нет — связь будет около нуля, и это значит, что лидерборд показывает
    не мастеров, а тех, кому повезло.
    """
    prices = btc_weekly(root)
    path = _dir(root) / "portfolios.jsonl"
    pairs: list[tuple[float, float, float, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        mine, market, stamps = returns(rec["portfolio"], prices, floor)
        if len(mine) < WEEKS_MIN * 2:
            continue
        if (stamps[-1] - stamps[0]).days / 365.25 < min_years:
            continue
        half = len(mine) // 2
        a, b = mine[:half], mine[half:]
        ma, mb = market[:half], market[half:]
        if stdev(a) <= 0 or stdev(b) <= 0:
            continue
        pairs.append(
            (
                fmean(a) / stdev(a),
                fmean(b) / stdev(b),
                beta_alpha(a, ma)[1],
                beta_alpha(b, mb)[1],
            )
        )

    if len(pairs) < 30:
        print(f"счетов с историей на две половины: {len(pairs)} — слишком мало")
        return 0
    print(f"счетов с полной историей на две половины: {len(pairs)}\n")
    for label, i, j in (("доход/риск", 0, 1), ("результат сверх рынка", 2, 3)):
        xs = [p[i] for p in pairs]
        ys = [p[j] for p in pairs]
        mx, my = fmean(xs), fmean(ys)
        dx = sqrt(sum((x - mx) ** 2 for x in xs))
        dy = sqrt(sum((y - my) ** 2 for y in ys))
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
        r = num / (dx * dy) if dx and dy else 0.0
        order = sorted(pairs, key=lambda p: -p[i])
        k = max(3, len(order) // 4)
        top = fmean(p[j] for p in order[:k])
        rest = fmean(p[j] for p in order[k:])
        print(f"{label}:")
        print(f"  связь первой половины со второй: {r:+.2f}")
        print(f"  верхняя четверть первой половины во второй: {top:+.3f}")
        print(f"  все остальные во второй:                    {rest:+.3f}")
    print(
        "\nЧитать так: связь около нуля означает, что прошлый результат не переносится,\n"
        "и выбирать по нему некого. Отрицательная связь — что лидерборд показывает тех,\n"
        "кто рискнул сильнее всех и на этот раз угадал."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--stage", required=True, choices=("leaderboard", "portfolios", "rank", "persist")
    )
    ap.add_argument("--min-capital", type=float, default=250_000)
    ap.add_argument("--max-turnover", type=float, default=20.0, help="оборотов капитала в месяц")
    ap.add_argument("--limit", type=int, default=600, help="сколько кандидатов качать")
    ap.add_argument("--pause", type=float, default=0.15, help="пауза между запросами, с")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--floor", type=float, default=50_000, help="капитал, ниже которого не считаем")
    ap.add_argument("--min-years", type=float, default=0.0, help="минимум истории для persist")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    if args.stage == "leaderboard":
        return stage_leaderboard(root, args.min_capital, args.max_turnover)
    if args.stage == "portfolios":
        return stage_portfolios(root, args.limit, args.pause)
    if args.stage == "persist":
        return stage_persist(root, args.floor, args.min_years)
    return stage_rank(root, args.top, args.floor)


if __name__ == "__main__":
    raise SystemExit(main())
