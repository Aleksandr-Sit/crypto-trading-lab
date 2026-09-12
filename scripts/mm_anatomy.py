#!/usr/bin/env python
"""Можно ли следовать за маркет-мейкером с отставанием — замер вместо рассуждения.

Верхние строки лидерборда Hyperliquid занимают счета с оборотом в сотни капиталов
за месяц: двенадцать тысяч сделок в сутки. Они устойчиво прибыльны, и вопрос законный —
почему бы не повторять их сделки, пусть и с задержкой.

Здесь считается анатомия их прибыли и строится **самая доброжелательная копия
из возможных**: без проскальзывания, без спреда, вход ровно по цене состоявшейся сделки
через заданную задержку. Реальная копия получит хуже по трём причинам сразу, поэтому
если убыточна даже эта — вопрос закрыт, и дело не в скорости.

Что разбирается:

* **из чего складывается прибыль** — движение цены или комиссии. У мейкера комиссия
  бывает отрицательной: биржа платит ему за то, что он стоит в стакане. Копия так
  не может по определению: она входит по рынку и платит комиссию тейкера;
* **сколько стоит один круг** — прибыль эпизода в базисных пунктах от оборота.
  Если она меньше комиссии копии, никакое отставание не помогает;
* **что достаётся копии** при задержке в 1, 5, 30 и 300 секунд.

Ценой служат сделки самого маркет-мейкера: он торгует непрерывно, и его собственная
лента — это готовый источник цен с точностью до секунд.

    python scripts/mm_anatomy.py --top 5 --root /app/data
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from bisect import bisect_left
from collections import defaultdict, deque
from pathlib import Path
from statistics import fmean, median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

INFO = "https://api.hyperliquid.xyz/info"
PAGE = 2000
TAKER_BPS = 3.5  # комиссия тейкера Hyperliquid, базисных пунктов с каждой стороны
LAGS = (1, 5, 30, 300)  # секунды отставания копии
SPOT = {"Buy", "Sell", "Spot Dust Conversion"}


def pick(root: Path, min_turnover: float, min_capital: float, top: int) -> list[dict]:
    """Счета с высоким оборотом — те самые, кого отсеял отбор на копируемость."""
    raw = root / "copytrade" / "leaderboard.json"
    rows = json.loads(raw.read_text())["leaderboardRows"]
    out = []
    for r in rows:
        capital = float(r["accountValue"])
        w = dict(r["windowPerformances"])
        volume = float(w["month"]["vlm"])
        if capital < min_capital or volume <= 0:
            continue
        turnover = volume / capital
        pnl = float(w["allTime"]["pnl"])
        if turnover < min_turnover or pnl <= 0:
            continue
        out.append({"address": r["ethAddress"], "capital": capital, "turnover": turnover, "pnl": pnl})
    out.sort(key=lambda c: -c["pnl"])
    return out[:top]


def fetch(client, address: str, pages: int, pause: float, hours: int) -> list[dict]:
    """Сделки счёта за последние часы.

    Начальная метка обязательна: запрос с одной только конечной биржа отвергает.
    Поэтому берём окно назад от текущего момента и идём вперёд страницами.
    """
    fills: list[dict] = []
    start = int((time.time() - hours * 3600) * 1000)
    for _ in range(pages):
        try:
            r = client.post(
                INFO,
                json={"type": "userFillsByTime", "user": address, "startTime": start},
            )
            r.raise_for_status()
            batch = r.json()
        except Exception as err:  # noqa: BLE001 — один счёт не роняет прогон
            print(f"  {address[:10]}: {type(err).__name__}")
            break
        if not batch:
            break
        fills += batch
        if len(batch) < PAGE:
            break
        start = batch[-1]["time"] + 1
        time.sleep(pause)
    fills.sort(key=lambda f: f["time"])
    return fills


def episodes(fills: list[dict]) -> list[tuple[str, int, int, float, int, float, float]]:
    """Замкнутые круги: монета, знак, время и цена входа, время и цена выхода, оборот."""
    queues: dict[str, deque[tuple[int, float, float]]] = defaultdict(deque)
    out = []
    for f in fills:
        d = f.get("dir") or ""
        if d in SPOT:
            continue
        px, sz, ts, coin = float(f["px"]), float(f["sz"]), f["time"], f["coin"]
        if d.startswith("Open") or ">" in d:
            queues[coin].append((ts, px, sz))
            continue
        if d.startswith("Close") and queues[coin]:
            t0, p0, _ = queues[coin].popleft()
            side = 1 if d.endswith("Long") else -1
            out.append((coin, side, t0, p0, ts, px, px * sz))
    return out


def tape(fills: list[dict]) -> dict[str, tuple[list[int], list[float]]]:
    """Лента цен по монетам из сделок самого счёта."""
    out: dict[str, tuple[list[int], list[float]]] = {}
    for f in fills:
        if (f.get("dir") or "") in SPOT:
            continue
        times, prices = out.setdefault(f["coin"], ([], []))
        times.append(f["time"])
        prices.append(float(f["px"]))
    return out


def price_at(book: tuple[list[int], list[float]], when: int) -> float | None:
    times, prices = book
    i = bisect_left(times, when)
    return prices[i] if i < len(times) else None


def report(name: str, fills: list[dict], info: dict) -> None:
    eps = episodes(fills)
    if len(eps) < 30:
        print(f"{name}: замкнутых кругов {len(eps)} — мало")
        return
    span = (fills[-1]["time"] - fills[0]["time"]) / 86_400_000
    fees = sum(float(f.get("fee") or 0) for f in fills)
    rebates = sum(1 for f in fills if float(f.get("fee") or 0) < 0)
    closed = sum(float(f.get("closedPnl") or 0) for f in fills)
    turnover = sum(float(f["px"]) * float(f["sz"]) for f in fills)

    own = [side * (p1 / p0 - 1) * 10_000 for _, side, _, p0, _, p1, _ in eps]
    holds = [(t1 - t0) / 1000 for _, _, t0, _, t1, _, _ in eps]
    print(f"\n=== {name} | капитал ${info['capital'] / 1e6:.1f}м, оборот {info['turnover']:.0f}/мес")
    print(f"сделок {len(fills)} за {span:.2f} сут ({len(fills) / max(span, 1e-9):.0f} в сутки)")
    print(f"замкнутых кругов {len(eps)}, медианное удержание {median(holds):.0f} с")
    print(f"прибыль круга: медиана {median(own):+.2f} б.п., средняя {fmean(own):+.2f} б.п.")
    print(f"комиссии: всего ${fees:,.0f} при обороте ${turnover / 1e6:.1f}м")
    print(f"  из них со скидкой (комиссия отрицательная): {rebates / len(fills) * 100:.0f}% сделок")
    print(f"  закрытая прибыль по данным биржи: ${closed:,.0f}")
    if closed:
        print(f"  доля комиссий в результате: {-fees / closed * 100:+.0f}%")

    books = tape(fills)
    print(f"\n{'задержка':10}{'кругов':>8}{'валом, б.п.':>14}{'за вычетом комиссии':>22}")
    for lag in LAGS:
        got = []
        for coin, side, t0, _, t1, _, _ in eps:
            book = books.get(coin)
            if not book:
                continue
            pi = price_at(book, t0 + lag * 1000)
            po = price_at(book, t1 + lag * 1000)
            if not pi or not po or pi <= 0:
                continue
            got.append(side * (po / pi - 1) * 10_000)
        if not got:
            continue
        net = fmean(got) - 2 * TAKER_BPS
        print(f"{lag:<10}{len(got):>8}{fmean(got):>13.2f}{net:>21.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--hours", type=int, default=24, help="окно истории назад, часов")
    ap.add_argument("--min-turnover", type=float, default=100.0)
    ap.add_argument("--min-capital", type=float, default=1_000_000)
    ap.add_argument("--pause", type=float, default=1.1)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    import httpx

    root = Path(args.root)
    chosen = pick(root, args.min_turnover, args.min_capital, args.top)
    print(f"счетов с оборотом ≥ {args.min_turnover:.0f}/мес и капиталом ≥ "
          f"${args.min_capital:,.0f}: {len(chosen)}")
    with httpx.Client(timeout=90) as client:
        for info in chosen:
            fills = fetch(client, info["address"], args.pages, args.pause, args.hours)
            if fills:
                report(info["address"][:12], fills, info)
            time.sleep(args.pause)
    print(
        "\nКопия здесь САМАЯ доброжелательная: вход по цене состоявшейся сделки, без спреда\n"
        "и без проскальзывания, комиссия только тейкерская. Настоящая получит хуже.\n"
        f"Круг по тейкеру — {2 * TAKER_BPS:.1f} б.п.; если прибыль круга меньше, дело\n"
        "не в скорости и отставание ни при чём."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
