#!/usr/bin/env python
"""Толпа как сигнал: куда наклонены спокойные трейдеры против горячих — и что потом с ценой.

Три захода (12.09 и 26.09.2026, `copytrading-2026-09-12.md`, `traders-regimes-2026-09-26.md`)
закрыли вопрос «за кем идти»: прошлый результат человека не переносится ни в целом,
ни по режимам, ни по поведению. Этот скрипт задаёт другой вопрос — не про людей,
а про КЛАССЫ: предсказывает ли суммарный чистый поток трейдеров с малым плечом (условие
владельца: до ×2–3) против трейдеров с большим движение цены на часы вперёд.

Это не пересекается с `wake`: там — следование за отдельными лидерами на горизонте
секунд (полураспад преимущества ~20 с). Здесь — класс целиком и горизонт в часы.
Данные `wake` только читаются (`/opt/wake/data/trades`, смонтировано только на чтение).

Поток `trades` Hyperliquid несёт адреса обеих сторон (`users = [покупатель, продавец]`),
`side` — сторона агрессора. Четыре стадии:

1. `scan` — проход по всем сделкам: по адресу — число исполнений, доля пассивных,
   объём покупок и продаж, активные часы, главная монета; по монете — цена на конец часа.
2. `classify` — отсев маркет-мейкеров (признаки из плана `wake`: пассивные исполнения,
   баланс сторон около нуля, частота на порядки выше медианы) и плечо у крупнейших
   направленных адресов: выставленное на позициях, а у пустого счёта — настройка по его
   главной монете (`activeAssetData`). Запросы лёгкие.
3. `flow` — второй проход: почасовой чистый поток (покупки − продажи) каждого класса
   по каждой монете, плюс поток агрессоров всего рынка как контроль (его эффект
   лаборатория уже мерила: живёт в 2023–2024, по годам разваливается).
4. `check` — порядок проверки гипотезы из CLAUDE.md: хвосты показателя против обычного
   часа; шум по независимым моментам с прореживанием по горизонту; контроль по цене
   (равное прошлое движение); вход через час как реалистичный; устойчивость по неделям;
   порог издержек Hyperliquid 0.091% за круг (замер `wake`).

ДВЕ ОГОВОРКИ, без которых результат читать нельзя:

* **Окно — дни, а не годы.** Сбор идёт с 14.08.2026; устойчивость по неделям одного
  режима рынка — слабая замена проверке по годам. Находка здесь — повод копить
  дальше, а не кодировать стратегию.
* **Плечо измерено СЕЙЧАС, а поток — в прошлом.** Настройка плеча липкая, но у
  разорившихся её могли поменять после. Чтобы это не стало подглядыванием, пустые
  счета (капитал меньше $1000 сейчас) идут в «плечо неизвестно».

    python scripts/crowd_flow.py --stage scan --trades /wake/trades --root /app/data
    python scripts/crowd_flow.py --stage classify --root /app/data
    python scripts/crowd_flow.py --stage flow --trades /wake/trades --root /app/data
    python scripts/crowd_flow.py --stage check --root /app/data
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from statistics import fmean, pvariance

sys.path.insert(0, str(Path(__file__).resolve().parent))

from copytrade_screen import DIR, _post, leverage_now  # noqa: E402

SUB = "crowd"
HOUR_MS = 3_600_000
MIN_GROSS_USD = 10_000  # адрес с меньшим оборотом за всё окно в файл не пишется
TOP_COINS = 30
CLASSES = ("calm", "mid", "hot", "unknown")
NAMES = {
    "calm": "плечо до ×3",
    "mid": "плечо ×3–10",
    "hot": "плечо больше ×10",
    "unknown": "плечо неизвестно",
}
COST_ROUND_PCT = 0.091  # круг издержек Hyperliquid со спредом, замер wake (BTC/SOL/HYPE)
MAX_GAP_S = 300  # час, где связи не было дольше, неполон — его поток не считается


def _out(root: Path) -> Path:
    d = root / DIR / SUB
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trades(folder: Path, gaps: dict[int, float] | None = None):
    """Все сделки по порядку файлов (генератор).

    Текущий час пишется без сжатия и неполон — пропускается. Сборщик пишет в поток
    отметки разрыва связи (`{"_": "gap", "с": …, "секунд": 3.6}`); они не сделки,
    а их секунды копятся в `gaps` по часам — час с долгим разрывом неполон, и его
    поток выглядел бы затишьем.
    """
    bad = 0
    for path in sorted(folder.glob("*.jsonl.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    if "_" in rec:
                        if gaps is not None and rec["_"] == "gap":
                            start = datetime.fromisoformat(rec["с"]).timestamp() * 1000
                            gaps[int(start // HOUR_MS)] += float(rec.get("секунд") or 0)
                        continue
                    yield rec
        except (EOFError, OSError, json.JSONDecodeError):
            bad += 1
    if bad:
        print(f"  битых файлов пропущено: {bad}")


# --- 1. scan -----------------------------------------------------------------------------


def stage_scan(trades: Path, root: Path) -> int:
    # адрес -> [исполнений, пассивных, покупки $, продажи $, последний час, активных часов]
    stats: dict[str, list] = {}
    coins_of: dict[str, dict[str, float]] = defaultdict(dict)
    price: dict[str, dict[int, float]] = defaultdict(dict)
    coin_usd: dict[str, float] = defaultdict(float)
    gaps: dict[int, float] = defaultdict(float)
    n = 0
    first = last = None
    for t in _trades(trades, gaps):
        n += 1
        coin = t["coin"]
        px = float(t["px"])
        usd = px * float(t["sz"])
        hour = t["time"] // HOUR_MS
        first = hour if first is None else first
        last = hour
        buyer, seller = t["users"]
        taker = buyer if t["side"] == "B" else seller
        for addr, buys in ((buyer, True), (seller, False)):
            s = stats.get(addr)
            if s is None:
                s = stats[addr] = [0, 0, 0.0, 0.0, -1, 0]
            s[0] += 1
            if addr != taker:
                s[1] += 1
            s[2 if buys else 3] += usd
            if s[4] != hour:
                s[4] = hour
                s[5] += 1
            c = coins_of[addr]
            c[coin] = c.get(coin, 0.0) + usd
        price[coin][hour] = px
        coin_usd[coin] += usd
        if n % 20_000_000 == 0:
            print(f"  {n / 1e6:.0f} млн сделок, адресов {len(stats)}")
    if not n:
        print("сделок нет — проверь путь --trades")
        return 1
    out = _out(root)
    kept = 0
    with gzip.open(out / "addresses.jsonl.gz", "wt", encoding="utf-8") as fh:
        for addr, s in stats.items():
            gross = s[2] + s[3]
            if gross < MIN_GROSS_USD:
                continue
            top = max(coins_of[addr].items(), key=lambda kv: kv[1])[0]
            fh.write(
                json.dumps(
                    {
                        "a": addr,
                        "fills": s[0],
                        "maker": s[1] / s[0],
                        "buy": round(s[2]),
                        "sell": round(s[3]),
                        "hours": s[5],
                        "top": top,
                        "coins": len(coins_of[addr]),
                    }
                )
                + "\n"
            )
            kept += 1
    top_coins = sorted(coin_usd, key=lambda c: -coin_usd[c])[:TOP_COINS]
    with (out / "prices.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["coin", "hour", "px"])
        for coin in top_coins:
            for hour, px in sorted(price[coin].items()):
                w.writerow([coin, hour, px])
    (out / "gaps.json").write_text(json.dumps({str(h): s for h, s in sorted(gaps.items())}))
    long_gaps = sum(1 for s in gaps.values() if s > MAX_GAP_S)
    span = (last - first + 1) if first is not None else 0
    print(f"сделок {n:,}, часов {span} ({span / 24:.1f} сут), адресов {len(stats):,}")
    print(
        f"  разрывов связи: {len(gaps)} часов с разрывом, {sum(gaps.values()):.0f} с всего; "
        f"часов с разрывом дольше {MAX_GAP_S} с (исключаются): {long_gaps}"
    )
    print(f"  с оборотом от ${MIN_GROSS_USD:,}: {kept:,} → addresses.jsonl.gz")
    print(f"  монет в ценах: {len(top_coins)} — {', '.join(top_coins[:12])} …")
    return 0


# --- 2. classify -------------------------------------------------------------------------


def _load_addresses(root: Path) -> list[dict]:
    with gzip.open(_out(root) / "addresses.jsonl.gz", "rt", encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def directional(rows: list[dict], max_per_hour: float) -> tuple[list[dict], dict[str, int]]:
    """Отсев маркет-мейкеров и ботов ДО всякого замера — признаки из плана `wake`."""
    why = {"частота": 0, "мейкер": 0}
    keep = []
    for r in rows:
        gross = r["buy"] + r["sell"]
        balance = abs(r["buy"] - r["sell"]) / gross if gross else 0.0
        if r["fills"] / max(r["hours"], 1) > max_per_hour:
            why["частота"] += 1
            continue
        if r["maker"] >= 0.9 and balance <= 0.05:
            why["мейкер"] += 1
            continue
        keep.append(r)
    return keep, why


def lev_class(lev: float | None, value: float) -> str:
    if lev is None or value < 1000:
        return "unknown"
    if lev <= 3:
        return "calm"
    if lev <= 10:
        return "mid"
    return "hot"


def stage_classify(root: Path, top: int, pause: float, max_per_hour: float) -> int:
    import httpx

    rows = _load_addresses(root)
    # Смысл полей потока сверен 26.09.2026 с собственными исполнениями 11 адресов
    # (userFillsByTime, по tid): users[0] — покупатель и агрессор по `side` совпали
    # в 13 135 сделках из 13 135. Косвенная проверка «у самых частых адресов доля
    # пассивных высокая» НЕ годится: среди самых частых есть агрессивные боты (0.30–0.43).
    keep, why = directional(rows, max_per_hour)
    print(f"адресов {len(rows):,}, направленных {len(keep):,}; отсеяно: {why}")
    keep.sort(key=lambda r: -(r["buy"] + r["sell"]))
    keep = keep[:top]
    out = _out(root) / "classes.jsonl"
    done = {}
    if out.exists():
        done = {json.loads(ln)["a"]: 1 for ln in out.read_text().splitlines() if ln.strip()}
    todo = [r for r in keep if r["a"] not in done]
    print(f"классифицируем {len(todo)} (уже есть {len(done)})")
    counts: dict[str, int] = defaultdict(int)
    with out.open("a", encoding="utf-8") as fh, httpx.Client(timeout=60) as client:
        for i, r in enumerate(todo, 1):
            state = _post(client, {"type": "clearinghouseState", "user": r["a"]})
            time.sleep(pause)
            if not isinstance(state, dict):
                continue
            value = float((state.get("marginSummary") or {}).get("accountValue") or 0)
            lev_now, lev_set = leverage_now(state)
            source = "позиции"
            if lev_set is None and value >= 1000:
                body = {"type": "activeAssetData", "user": r["a"], "coin": r["top"]}
                asset = _post(client, body)
                time.sleep(pause)
                if isinstance(asset, dict):
                    lev_set = float((asset.get("leverage") or {}).get("value") or 0) or None
                    source = "настройка"
            cls = lev_class(lev_set, value)
            counts[cls] += 1
            rec = {
                "a": r["a"],
                "cls": cls,
                "lev": lev_set,
                "lev_now": lev_now,
                "value": value,
                "src": source,
            }
            fh.write(json.dumps(rec) + "\n")
            if i % 500 == 0:
                print(f"  {i}/{len(todo)} {dict(counts)}")
    print(f"готово: {dict(counts)}")
    return 0


# --- 3. flow -----------------------------------------------------------------------------


def stage_flow(trades: Path, root: Path) -> int:
    out = _out(root)
    cls_of = {}
    for ln in (out / "classes.jsonl").read_text().splitlines():
        if ln.strip():
            rec = json.loads(ln)
            cls_of[rec["a"]] = rec["cls"]
    coins = set()
    with (out / "prices.csv").open(encoding="utf-8") as fh:
        coins = {row["coin"] for row in csv.DictReader(fh)}
    # (класс, монета, час) -> [чистый $, валовый $]; класс "taker" — агрессоры всего рынка
    flow: dict[tuple[str, str, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
    n = 0
    for t in _trades(trades):
        coin = t["coin"]
        if coin not in coins:
            continue
        n += 1
        usd = float(t["px"]) * float(t["sz"])
        hour = t["time"] // HOUR_MS
        buyer, seller = t["users"]
        cell = flow[("taker", coin, hour)]
        cell[0] += usd if t["side"] == "B" else -usd
        cell[1] += usd
        for addr, sign in ((buyer, 1.0), (seller, -1.0)):
            cls = cls_of.get(addr)
            if cls is not None:
                cell = flow[(cls, coin, hour)]
                cell[0] += sign * usd
                cell[1] += usd
    with gzip.open(out / "flows.csv.gz", "wt", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cls", "coin", "hour", "net", "gross"])
        for (cls, coin, hour), (net, gross) in sorted(flow.items()):
            w.writerow([cls, coin, hour, round(net), round(gross)])
    print(f"сделок по монетам списка {n:,}, ячеек {len(flow):,} → flows.csv.gz")
    return 0


# --- 4. check ----------------------------------------------------------------------------


def _load_check(root: Path) -> tuple[dict, dict]:
    out = _out(root)
    px: dict[str, dict[int, float]] = defaultdict(dict)
    with (out / "prices.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            px[row["coin"]][int(row["hour"])] = float(row["px"])
    fl: dict[tuple[str, str], dict[int, tuple[float, float]]] = defaultdict(dict)
    with gzip.open(out / "flows.csv.gz", "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cell = (float(row["net"]), float(row["gross"]))
            fl[(row["cls"], row["coin"])][int(row["hour"])] = cell
    # Час с долгим разрывом связи неполон: его поток убирается, окно считается без него.
    gaps = json.loads((out / "gaps.json").read_text()) if (out / "gaps.json").exists() else {}
    broken = {int(h) for h, s in gaps.items() if s > MAX_GAP_S}
    for cells in fl.values():
        for h in broken & cells.keys():
            del cells[h]
    return px, fl


def imbalance(cells: dict[int, tuple[float, float]], hour: int, window: int) -> float | None:
    net = gross = 0.0
    for h in range(hour - window + 1, hour + 1):
        c = cells.get(h)
        if c:
            net += c[0]
            gross += c[1]
    return net / gross if gross > 0 else None


def _pctl(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(q * len(s))))]


def evaluate(
    obs: list[tuple[int, str, float, float, float]], h: int, tail: float
) -> dict[str, float] | None:
    """obs: (час, монета, сигнал, доходность вперёд %, прошлое движение %).

    Хвосты — по собственному распределению сигнала КАЖДОЙ монеты. Шум — по независимым
    часам: монеты в один час — одно наблюдение, соседние часы на горизонте h делят путь,
    поэтому берётся каждый h-й час (урок `signal_check`: перекрытие горизонтов занижало
    шум в √h раз и дважды выдавало шум за находку).
    """
    by_coin: dict[str, list] = defaultdict(list)
    for o in obs:
        by_coin[o[1]].append(o)
    tagged: list[tuple[int, str, float, float]] = []  # час, хвост, доходность, прошлое
    for rows in by_coin.values():
        sig = [r[2] for r in rows]
        lo, hi = _pctl(sig, tail), _pctl(sig, 1 - tail)
        past = [r[4] for r in rows]
        p1, p2 = _pctl(past, 1 / 3), _pctl(past, 2 / 3)
        for r in rows:
            side = "hi" if r[2] >= hi else "lo" if r[2] <= lo else "mid"
            tercile = 0 if r[4] <= p1 else 1 if r[4] <= p2 else 2
            tagged.append((r[0], side, r[3], tercile))
    thin = [t for t in tagged if t[0] % h == 0]

    def spread(rows: list) -> tuple[float, float, int] | None:
        per_hour: dict[str, dict[int, list[float]]] = {
            "hi": defaultdict(list),
            "lo": defaultdict(list),
        }
        for hour, side, ret, _ in rows:
            if side in per_hour:
                per_hour[side][hour].append(ret)
        hi = [fmean(v) for v in per_hour["hi"].values()]
        lo = [fmean(v) for v in per_hour["lo"].values()]
        if len(hi) < 5 or len(lo) < 5:
            return None
        se = sqrt(pvariance(hi) / len(hi) + pvariance(lo) / len(lo))
        return fmean(hi) - fmean(lo), 2 * se, min(len(hi), len(lo))

    base = spread(thin)
    if base is None:
        return None
    # Контроль по цене: внутри каждой трети прошлого движения, затем среднее по третям.
    within = [spread([t for t in thin if t[3] == k]) for k in range(3)]
    within = [w for w in within if w]
    weeks: dict[int, list] = defaultdict(list)
    for t in thin:
        weeks[t[0] // (24 * 7)].append(t)
    signs = [s[0] > 0 for s in (spread(v) for v in weeks.values()) if s]
    return {
        "spread": base[0],
        "noise": base[1],
        "n": base[2],
        "controlled": fmean(w[0] for w in within) if within else float("nan"),
        "weeks_pos": sum(signs),
        "weeks": len(signs),
    }


def stage_check(root: Path, window: int, tail: float, horizons: list[int], delay: int) -> int:
    px, fl = _load_check(root)
    coins = sorted(px)
    signals = {
        **{c: NAMES[c] for c in ("calm", "mid", "hot")},
        "diff": "до ×3 минус больше ×10",
        "taker": "агрессоры всего рынка (контроль)",
    }
    print(
        f"окно показателя {window} ч, хвосты {tail:.0%}, вход через {delay} ч, "
        f"монет {len(coins)}, издержки круга {COST_ROUND_PCT}%"
    )
    for h in horizons:
        print(f"\nгоризонт {h} ч")
        print(
            f"  {'сигнал':<34}{'разница':>9}{'шум 2σ':>9}{'часов':>7}"
            f"{'с контр. цены':>15}{'недель +':>10}  вердикт"
        )
        for key, label in signals.items():
            obs = []
            for coin in coins:
                prices = px[coin]
                for hour in prices:
                    entry = prices.get(hour + delay)
                    exit_ = prices.get(hour + delay + h)
                    before = prices.get(hour - window)
                    if not entry or not exit_ or not before:
                        continue
                    if key == "diff":
                        a = imbalance(fl.get(("calm", coin), {}), hour, window)
                        b = imbalance(fl.get(("hot", coin), {}), hour, window)
                        s = a - b if a is not None and b is not None else None
                    else:
                        s = imbalance(fl.get((key, coin), {}), hour, window)
                    if s is None:
                        continue
                    ret = (exit_ / entry - 1) * 100
                    past = (prices[hour] / before - 1) * 100
                    obs.append((hour, coin, s, ret, past))
            res = evaluate(obs, h, tail)
            if res is None:
                print(f"  {label:<34} мало наблюдений")
                continue
            real = abs(res["spread"]) > res["noise"]
            costly = abs(res["spread"]) > COST_ROUND_PCT
            verdict = "за шумом и издержками" if real and costly else (
                "за шумом, меньше издержек" if real else "шум")
            print(
                f"  {label:<34}{res['spread']:>+9.3f}{res['noise']:>9.3f}{res['n']:>7}"
                f"{res['controlled']:>+15.3f}{res['weeks_pos']:>5}/{res['weeks']:<4}  {verdict}"
            )
    print(
        "\nЧитать так: «разница» — доходность вперёд (п.п.) после верхних 10% показателя минус\n"
        "после нижних. Находка — только если она за шумом, больше издержек, не исчезает\n"
        "с контролем цены и держит знак в большинстве недель. Недели одного режима рынка\n"
        "годы не заменяют: даже находка здесь — довод копить данные, а не писать стратегию."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", required=True, choices=("scan", "classify", "flow", "check"))
    ap.add_argument("--root", default="data")
    ap.add_argument("--trades", default="/wake/trades", help="каталог часовых файлов wake")
    ap.add_argument("--top", type=int, default=5000, help="сколько направленных классифицировать")
    ap.add_argument("--pause", type=float, default=0.12)
    ap.add_argument(
        "--max-per-hour", type=float, default=30.0, help="исполнений в активный час, выше — бот"
    )
    ap.add_argument("--window", type=int, default=24, help="окно показателя, часов")
    ap.add_argument("--tail", type=float, default=0.10)
    ap.add_argument("--horizons", default="1,4,12,24")
    ap.add_argument("--delay", type=int, default=1, help="вход через столько часов после сигнала")
    args = ap.parse_args()

    root = Path(args.root)
    if args.stage == "scan":
        return stage_scan(Path(args.trades), root)
    if args.stage == "classify":
        return stage_classify(root, args.top, args.pause, args.max_per_hour)
    if args.stage == "flow":
        return stage_flow(Path(args.trades), root)
    horizons = [int(x) for x in args.horizons.split(",")]
    return stage_check(root, args.window, args.tail, horizons, args.delay)


if __name__ == "__main__":
    started = datetime.now(UTC)
    code = main()
    print(f"({(datetime.now(UTC) - started).total_seconds() / 60:.1f} мин)")
    raise SystemExit(code)
