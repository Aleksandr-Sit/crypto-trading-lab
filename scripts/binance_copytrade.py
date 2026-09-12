#!/usr/bin/env python
"""Копитрейдинг Binance: что обещает лидерборд и что достаётся копирующему.

У Hyperliquid мы выяснили, что прошлый результат трейдера не переносится в будущий
(связь −0.03). Binance — другая площадка и другой вопрос: здесь лидеров **отбирает
и показывает сама биржа**, и у неё есть поле, которого нет больше нигде, — `copierPnl`,
итог тех, кто копировал, ОТДЕЛЬНО от итога самого лидера.

Это прямая проверка обещания. Лидерборд ранжирует по `roi` лидера; копирующий получает
другое: он входит позже, платит свои комиссии, а его доля прибыли делится с лидером.
Разница между `pnl` и `copierPnl` и есть цена копирования, измеренная самой биржей.

Что считается:

* **доля портфелей, где лидер в плюсе, а копирующие в минусе** — самый недвусмысленный
  показатель, тут не нужно знать капитал копирующих;
* **связь ROI лидера с итогом копирующих** — если её нет, ранжирование по ROI бесполезно
  для того, кто собирается копировать;
* **ROI против просадки** — высокий ROI при высокой просадке это плечо, а не навык;
* **переносимость** по ряду `chartItems`: первая половина против второй.

Метка `tradFiTag` отделяет портфели на мировых активах от криптовалютных — Binance ведёт
и те и другие, и сравнить их полезно: рынок спокойнее, но и издержки относительно хода
цены другие.

    python scripts/binance_copytrade.py --stage list --root /app/data
    python scripts/binance_copytrade.py --stage rank --root /app/data
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from math import sqrt
from pathlib import Path
from statistics import fmean, median, stdev

URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0",
    "clienttype": "web",
}
PAGE = 100
FILE = "binance-leaders.jsonl"


def _dir(root: Path) -> Path:
    d = root / "copytrade"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_list(root: Path, pages: int, pause: float, window: str) -> int:
    """Все лид-портфели постранично. Ряд доходности приходит вместе со строкой."""
    import httpx

    path = _dir(root) / FILE
    seen: set[str] = set()
    if path.exists():
        seen = {
            json.loads(ln)["leadPortfolioId"]
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        }
    added = 0
    with path.open("a", encoding="utf-8") as fh, httpx.Client(timeout=60) as client:
        for page in range(1, pages + 1):
            body = {
                "pageNumber": page,
                "pageSize": PAGE,
                "timeRange": window,
                "dataType": "ROI",
                "favoriteOnly": False,
                "hideFull": False,
                "nickname": "",
                "order": "DESC",
                "portfolioType": "ALL",
                "sortBy": "ROI",
            }
            try:
                r = client.post(URL, json=body, headers=HEADERS)
                r.raise_for_status()
                data = (r.json() or {}).get("data") or {}
            except Exception as err:  # noqa: BLE001 — страница не роняет прогон
                print(f"  страница {page}: {type(err).__name__}")
                break
            rows = data.get("list") or []
            if not rows:
                break
            for row in rows:
                pid = str(row.get("leadPortfolioId") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                fh.write(json.dumps(row) + "\n")
                added += 1
            if page == 1:
                print(f"всего портфелей по данным биржи: {data.get('total')}")
            if page % 10 == 0:
                print(f"  страница {page}, собрано {added}")
            time.sleep(pause)
    print(f"новых записей: {added}, всего в файле: {len(seen)}")
    return 0


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 5:
        return 0.0
    mx, my = fmean(xs), fmean(ys)
    dx = sqrt(sum((x - mx) ** 2 for x in xs))
    dy = sqrt(sum((y - my) ** 2 for y in ys))
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return num / (dx * dy) if dx and dy else 0.0


def _persist(rows: list[dict]) -> None:
    """Переносимость по ряду доходности: первая половина против второй."""
    pairs: list[tuple[float, float]] = []
    for row in rows:
        chart = row.get("chartItems") or []
        vals = [_f(p, "value") for p in chart]
        if len(vals) < 20:
            continue
        # `chartItems` — НАКОПЛЕННЫЙ ROI, поэтому берутся приращения, иначе вторая
        # половина несёт в себе всю первую и связь получится из воздуха.
        steps = [b - a for a, b in zip(vals, vals[1:], strict=False)]
        half = len(steps) // 2
        a, b = steps[:half], steps[half:]
        if len(a) < 5 or len(b) < 5 or stdev(a) <= 0 or stdev(b) <= 0:
            continue
        pairs.append((fmean(a) / stdev(a), fmean(b) / stdev(b)))
    if len(pairs) < 30:
        print(f"\nпереносимость: рядов хватило только у {len(pairs)} — мало")
        return
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    order = sorted(pairs, key=lambda p: -p[0])
    k = max(3, len(order) // 4)
    print(f"\nПЕРЕНОСИМОСТЬ (доход на риск), портфелей {len(pairs)}")
    print(f"  связь первой половины ряда со второй: {corr(xs, ys):+.2f}")
    print(f"  верхняя четверть первой половины во второй: {fmean(p[1] for p in order[:k]):+.3f}")
    print(f"  все остальные во второй:                    {fmean(p[1] for p in order[k:]):+.3f}")


def stage_rank(root: Path, min_aum: float) -> int:
    path = _dir(root) / FILE
    if not path.exists():
        print("сначала --stage list")
        return 1
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    live = [r for r in rows if _f(r, "aum") >= min_aum]
    tradfi = [r for r in live if r.get("tradFiTag")]
    print(f"портфелей всего {len(rows)}, с капиталом ≥ ${min_aum:,.0f}: {len(live)}")
    print(f"  из них на мировых активах (метка TradFi): {len(tradfi)}")
    if len(live) < 30:
        print("мало для выводов")
        return 0

    # Главный вопрос: доходит ли обещанное до копирующего.
    both = [r for r in live if _f(r, "pnl") != 0]
    leader_up = [r for r in both if _f(r, "pnl") > 0]
    bad = [r for r in leader_up if _f(r, "copierPnl") <= 0]
    print(f"\nЛИДЕР В ПЛЮСЕ, А КОПИРУЮЩИЕ НЕТ: {len(bad)} из {len(leader_up)} "
          f"({len(bad) / max(1, len(leader_up)) * 100:.0f}%)")
    share = [
        _f(r, "copierPnl") / _f(r, "pnl")
        for r in leader_up
        if _f(r, "pnl") > 0 and abs(_f(r, "copierPnl")) < abs(_f(r, "pnl")) * 100
    ]
    if share:
        print(f"  итог копирующих к итогу лидера: медиана {median(share):+.2f}")

    roi = [_f(r, "roi") for r in live]
    mdd = [_f(r, "mdd") for r in live]
    cop = [_f(r, "copierPnl") for r in live]
    print("\nСВЯЗИ")
    print(f"  ROI лидера ↔ итог копирующих:  {corr(roi, cop):+.2f}")
    print(f"  ROI лидера ↔ его просадка:      {corr(roi, mdd):+.2f}")
    print(f"  доля выигрышных ↔ итог копир.:  "
          f"{corr([_f(r, 'winRate') for r in live], cop):+.2f}")

    print(f"\n{'группа':22}{'портфелей':>11}{'медиана ROI':>14}{'медиана просадки':>18}")
    for label, group in (("все", live), ("мировые активы", tradfi),
                         ("крипта", [r for r in live if not r.get("tradFiTag")])):
        if not group:
            continue
        print(
            f"{label:22}{len(group):>11}{median(_f(r, 'roi') for r in group):>13.1f}%"
            f"{median(_f(r, 'mdd') for r in group):>17.1f}%"
        )
    _persist(live)
    print(
        "\nЧитать так: ранжирование по ROI полезно копирующему, только если ROI лидера\n"
        "связан с ИТОГОМ КОПИРУЮЩИХ. Связь около нуля означает, что сортировка,\n"
        "которую показывает биржа, не отвечает на вопрос «что получу я»."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", required=True, choices=("list", "rank"))
    ap.add_argument("--pages", type=int, default=60)
    ap.add_argument("--pause", type=float, default=0.4)
    ap.add_argument("--window", default="90D", help="90D | 30D | 7D")
    ap.add_argument("--min-aum", type=float, default=10_000)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    if args.stage == "list":
        return stage_list(root, args.pages, args.pause, args.window)
    return stage_rank(root, args.min_aum)


if __name__ == "__main__":
    raise SystemExit(main())
