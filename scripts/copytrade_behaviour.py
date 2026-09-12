#!/usr/bin/env python
"""Какое ПОВЕДЕНИЕ связано с лучшим исходом — вопрос вместо «кто хороший трейдер».

Замер `copytrade_screen.py --stage persist` показал, что прошлый результат трейдера
не переносится в будущий: связь первой половины истории со второй −0.03 по доходу на риск
и +0.08 по результату сверх рынка на 634 счетах. Значит выбирать человека по его прошлому
нельзя, и всё, что строится на конкретных именах, эту ошибку наследует.

Но вопрос можно сменить. «Хорош ли трейдер X» безнадёжен статистически: на одного человека
приходится несколько десятков наблюдений. «Какое поведение связано с лучшим исходом»
опирается на сотни счетов сразу — и проверяется тем же разделением пополам: **поведение
замеряется в ПЕРВОЙ половине истории, исход — во ВТОРОЙ**.

Сырые сделки не сохраняются. На шести сотнях счетов за два года это миллионы записей
и гигабайты; вместо этого каждая страница ответа сразу превращается в счётчики, а на диск
ложится одна строка признаков на счёт. Тот же приём, что спас замер позиционирования
от OOM: агрегировать в запросе, а не копить объекты.

Что считается по сделкам (у Hyperliquid они отдаются все, с ценой, размером и комиссией):

* медианное время удержания и доля эпизодов короче часа;
* сколько монет в работе и доля эпизодов в самой частой;
* доля лонгов;
* **доливают ли к убыточной позиции** — единственное поведение, про которое есть внятная
  причина ожидать связь с разорением;
* доля выигрышных эпизодов и отношение среднего выигрыша к среднему проигрышу;
* сколько съедают комиссии от оборота.

    python scripts/copytrade_behaviour.py --stage fills --limit 400 --root /app/data
    python scripts/copytrade_behaviour.py --stage link --root /app/data
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from statistics import fmean, median, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from copytrade_screen import _dir, btc_weekly, returns  # noqa: E402

INFO = "https://api.hyperliquid.xyz/info"
PAGE = 2000
MAX_PAGES = 40  # предохранитель: счёт с высокой частотой не должен съесть весь прогон

FEATURES = (
    "hold_hours",
    "short_share",
    "coins",
    "top_coin_share",
    "long_share",
    "add_to_loser",
    "win_rate",
    "payoff",
    "fee_drag",
    "episodes",
)


SPOT_DIRS = {"Buy", "Sell", "Spot Dust Conversion"}


class Account:
    """Счётчики одного счёта. Сделки не хранятся, остаются только счётчики.

    Позиция НЕ восстанавливается сложением размеров: дробные доли накапливаются, и она
    никогда не возвращается ровно в ноль — в первом прогоне восемнадцать тысяч сделок дали
    двадцать «эпизодов». Биржа отдаёт в каждой сделке `startPosition` — позицию ДО неё,
    и это авторитетный источник.
    """

    def __init__(self) -> None:
        self.pos: dict[str, list[float]] = {}  # монета → [средняя цена входа, время входа]
        self.holds: list[float] = []
        self.wins: list[float] = []
        self.losses: list[float] = []
        self.per_coin: dict[str, int] = {}
        self.longs = 0
        self.episodes = 0
        self.adds = 0
        self.adds_bad = 0
        self.fees = 0.0
        self.notional = 0.0
        self.fills = 0
        self.pnl = 0.0

    def add(self, f: dict) -> None:
        if f.get("dir") in SPOT_DIRS:
            return  # спот: копируют бессрочные контракты, а не покупку монеты
        coin = f["coin"]
        px = float(f["px"])
        sz = float(f["sz"])
        signed = sz if f["side"] == "B" else -sz
        before = float(f.get("startPosition") or 0)
        after = before + signed
        self.fills += 1
        self.fees += float(f.get("fee") or 0)
        self.notional += px * sz
        self.pnl += float(f.get("closedPnl") or 0)
        flat = abs(after) < abs(sz) * 1e-6

        if before == 0:
            self.pos[coin] = [px, f["time"]]
            self.episodes += 1
            self.per_coin[coin] = self.per_coin.get(coin, 0) + 1
            if signed > 0:
                self.longs += 1
            return

        avg, opened = self.pos.get(coin, [px, f["time"]])
        if abs(after) > abs(before) and (after > 0) == (before > 0):
            # Долив. Убыточен ли он: для лонга — цена ниже средней, для шорта — выше.
            self.adds += 1
            if (before > 0 and px < avg) or (before < 0 and px > avg):
                self.adds_bad += 1
            self.pos[coin] = [(avg * abs(before) + px * abs(signed)) / abs(after), opened]
            return

        gain = float(f.get("closedPnl") or 0)
        if flat or (after > 0) != (before > 0):
            self.holds.append((f["time"] - opened) / 3_600_000)
            (self.wins if gain > 0 else self.losses).append(gain)
            if flat:
                self.pos.pop(coin, None)
            else:
                # Переворот: закрытие и новый вход одной сделкой.
                self.pos[coin] = [px, f["time"]]
                self.episodes += 1
                self.per_coin[coin] = self.per_coin.get(coin, 0) + 1
                if after > 0:
                    self.longs += 1

    def features(self) -> dict[str, float] | None:
        if self.episodes < 20 or not self.holds:
            return None
        total_eps = sum(self.per_coin.values()) or 1
        avg_win = fmean(self.wins) if self.wins else 0.0
        avg_loss = abs(fmean(self.losses)) if self.losses else 0.0
        closed = len(self.wins) + len(self.losses)
        return {
            "fills": self.fills,
            "episodes": self.episodes,
            "hold_hours": median(self.holds),
            "short_share": sum(1 for h in self.holds if h < 1) / len(self.holds) * 100,
            "coins": len(self.per_coin),
            "top_coin_share": max(self.per_coin.values()) / total_eps * 100,
            "long_share": self.longs / total_eps * 100,
            "add_to_loser": (self.adds_bad / self.adds * 100) if self.adds else 0.0,
            "win_rate": (len(self.wins) / closed * 100) if closed else 0.0,
            "payoff": (avg_win / avg_loss) if avg_loss else 0.0,
            "fee_drag": abs(self.fees) / self.notional * 100 if self.notional else 0.0,
        }


def split_times(root: Path, floor: float) -> dict[str, tuple[int, list[float]]]:
    """Момент раздела истории и доходности ВТОРОЙ половины — по кривой капитала."""
    prices = btc_weekly(root)
    out: dict[str, tuple[int, list[float]]] = {}
    path = _dir(root) / "portfolios.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        mine, _, stamps = returns(rec["portfolio"], prices, floor)
        if len(mine) < 52:
            continue
        half = len(mine) // 2
        cut = datetime(
            stamps[half].year, stamps[half].month, stamps[half].day, tzinfo=UTC
        ).timestamp()
        out[rec["address"]] = (int(cut * 1000), mine[half:])
    return out


def stage_fills(root: Path, limit: int, pause: float, floor: float) -> int:
    import httpx

    splits = split_times(root, floor)
    path = _dir(root) / "behaviour.jsonl"
    done = set()
    if path.exists():
        done = {json.loads(ln)["address"] for ln in path.read_text().splitlines() if ln.strip()}
    todo = [a for a in splits if a not in done][:limit]
    print(f"счетов с двумя половинами {len(splits)}, уже есть {len(done)}, качаем {len(todo)}")

    with path.open("a", encoding="utf-8") as fh, httpx.Client(timeout=90) as client:
        for n, address in enumerate(todo, 1):
            cut = splits[address][0]
            acc = Account()
            start = 1_577_836_800_000  # 2020-01-01: биржи не было раньше, ноль API не любит
            pages = 0
            truncated = False
            while pages < MAX_PAGES:
                batch = None
                for attempt in range(4):
                    try:
                        r = client.post(
                            INFO,
                            json={"type": "userFillsByTime", "user": address, "startTime": start},
                        )
                        r.raise_for_status()
                        batch = r.json()
                        break
                    except Exception as err:  # noqa: BLE001 — ограничение частоты, отступаем
                        if attempt == 3:
                            print(f"  {address[:10]}: {type(err).__name__}")
                        time.sleep(2 ** attempt)
                if batch is None:
                    break
                if not batch:
                    break
                stop = False
                for f in batch:
                    if f["time"] >= cut:
                        # Поведение меряется ТОЛЬКО до раздела: всё, что после, — исход.
                        stop = True
                        break
                    acc.add(f)
                if stop or len(batch) < PAGE:
                    break
                start = batch[-1]["time"] + 1
                pages += 1
                time.sleep(pause)
            else:
                truncated = True
            feats = acc.features()
            if feats:
                fh.write(
                    json.dumps({"address": address, "truncated": truncated, **feats}) + "\n"
                )
            if n % 50 == 0:
                print(f"  {n}/{len(todo)}")
            time.sleep(pause)
    return 0


def corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 5:
        return 0.0
    mx, my = fmean(xs), fmean(ys)
    dx = sqrt(sum((x - mx) ** 2 for x in xs))
    dy = sqrt(sum((y - my) ** 2 for y in ys))
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return num / (dx * dy) if dx and dy else 0.0


def stage_link(root: Path, floor: float) -> int:
    """Связь поведения ПЕРВОЙ половины с исходом ВТОРОЙ."""
    splits = split_times(root, floor)
    path = _dir(root) / "behaviour.jsonl"
    if not path.exists():
        print("сначала стадия fills")
        return 1
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        later = splits.get(rec["address"], (0, []))[1]
        if len(later) < 20 or stdev(later) <= 0:
            continue
        rec["outcome"] = fmean(later) / stdev(later)  # доход на риск во второй половине
        rec["ret"] = fmean(later)
        rows.append(rec)

    if len(rows) < 40:
        print(f"счетов с поведением и исходом: {len(rows)} — мало для вывода")
        return 0
    cut = sum(1 for r in rows if r.get("truncated"))
    print(f"счетов: {len(rows)} (у {cut} история сделок обрезана предохранителем)\n")
    print(f"{'признак':18}{'связь с исходом':>17}{'у худшей трети':>16}{'у лучшей трети':>16}")
    order = sorted(rows, key=lambda r: r["outcome"])
    k = max(5, len(order) // 3)
    worst, best = order[:k], order[-k:]
    for f in FEATURES:
        xs = [r[f] for r in rows]
        ys = [r["outcome"] for r in rows]
        print(
            f"{f:18}{corr(xs, ys):>+17.2f}"
            f"{fmean(r[f] for r in worst):>16.2f}{fmean(r[f] for r in best):>16.2f}"
        )
    print(
        "\nЧитать так: связь — это поведение ДО раздела против исхода ПОСЛЕ него.\n"
        "Колонки «худшая/лучшая треть» описывают, а не доказывают: они про тот же период.\n"
        "Доказывает только столбец связи, и только если он далёк от нуля."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", required=True, choices=("fills", "link"))
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--pause", type=float, default=0.4)
    ap.add_argument("--floor", type=float, default=50_000)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    if args.stage == "fills":
        return stage_fills(root, args.limit, args.pause, args.floor)
    return stage_link(root, args.floor)


if __name__ == "__main__":
    raise SystemExit(main())
