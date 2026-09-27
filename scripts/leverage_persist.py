#!/usr/bin/env python
"""Плечо ×2–3: переносится ли результат у спокойных трейдеров лучше, чем у остальных.

Условие владельца (26.09.2026): смотреть на тех, кто торгует с небольшим плечом, до ×2–3.
В кривых прибыли плеча нет, поэтому оно восстанавливается по сделкам: после каждой
сделки — сумма позиций по всем монетам к капиталу на тот момент. Позиция по монете
берётся из `startPosition` самой биржи плюс размер сделки, а не накоплением размеров:
сложение дробных долей не возвращается в ноль (урок `copytrade_behaviour.py`).

Выборка — та же, что в `regime_persist.py`: счета с историей на две половины и всеми
тремя режимами рынка в каждой. Плечо меряется в ПЕРВОЙ половине, исход — во второй.

Что спрашивается:

1. держится ли привычка к плечу — если нет, отбирать по нему бессмысленно;
2. связано ли малое плечо само по себе с лучшим исходом (12.09 плечо не проверялось —
   среди десяти признаков поведения его не было);
3. переносится ли умение ВНУТРИ группы «до ×3» — главный вопрос владельца;
4. переносится ли прибыль по лонгам и по шортам по отдельности.

ОГОВОРКА, меняющая чтение группы с большим плечом: кандидаты — счета с капиталом от
$250 тыс. СЕГОДНЯ. Разорившиеся с плечом ×20 в список не попали, и выжившие из этой
группы выглядят лучше, чем группа была на самом деле. Честный ответ про плечо даёт
только замер вперёд (`copytrade_screen.py --stage forward`, плечо пишется в снимок).

Сырые сделки СОХРАНЯЮТСЯ (`copytrade/fills/<адрес>.jsonl.gz`, только нужные поля):
12.09 они шли сразу в счётчики, и следующий вопрос к тем же сделкам потребовал бы
скачать всё заново. Биржа отдаёт не больше 10 тысяч последних сделок счёта, поэтому
у самых активных первая половина истории может быть видна лишь частично — это
печатается как «покрытие».

    python scripts/leverage_persist.py --stage fills --root /app/data
    python scripts/leverage_persist.py --stage link --root /app/data
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from datetime import UTC, date, datetime
from math import sqrt
from pathlib import Path
from statistics import fmean, median, pstdev, quantiles

sys.path.insert(0, str(Path(__file__).resolve().parent))

from copytrade_behaviour import SPOT_DIRS  # noqa: E402
from copytrade_screen import DIR, _post, beta_alpha, btc_weekly, returns  # noqa: E402
from regime_persist import REGIMES, half_scores, spearman  # noqa: E402

PAGE = 2000
MAX_FILLS = 12_000  # биржа хранит 10 тысяч последних; больше — признак зацикливания страниц
DAY_MS = 86_400_000
CALM = 3.0  # граница владельца: «до ×2–3»
HOT = 10.0
MAKER = 0.5  # доля пассивных исполнений, с которой счёт считается маркет-мейкерским
LONG_CLOSE = {"Close Long", "Long > Short"}
SHORT_CLOSE = {"Close Short", "Short > Long"}


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def sample(root: Path, floor: float, band: float, min_periods: int) -> dict[str, dict]:
    """Счета выборки `regime_persist`: оценки половин, границы по времени, кривая капитала."""
    prices = btc_weekly(root)
    out: dict[str, dict] = {}
    for line in (root / DIR / "portfolios.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        mine, market, stamps = returns(rec["portfolio"], prices, floor)
        if len(mine) < 40:
            continue
        gaps = [(b - a).days for a, b in zip(stamps, stamps[1:], strict=False)]
        step = max(median(gaps), 1.0)
        half = len(mine) // 2
        a = half_scores(mine[:half], market[:half], step, band, min_periods)
        b = half_scores(mine[half:], market[half:], step, band, min_periods)
        if not (a and b):
            continue
        sd_a, sd_b = pstdev(mine[:half]), pstdev(mine[half:])
        beta_a, alpha_a = beta_alpha(mine[:half], market[:half])
        beta_b, alpha_b = beta_alpha(mine[half:], market[half:])
        rsd_a = pstdev(y - beta_a * x for x, y in zip(market[:half], mine[:half], strict=True))
        rsd_b = pstdev(y - beta_b * x for x, y in zip(market[half:], mine[half:], strict=True))
        av = (dict(rec["portfolio"]).get("allTime") or {}).get("accountValueHistory") or []
        out[rec["address"]] = {
            "a": a,
            "b": b,
            "alpha_a": alpha_a,
            "alpha_b": alpha_b,
            # «Сверх рынка» не делится на риск: у выживших средняя положительна почти
            # у всех, и тогда ранг по ней — это ранг раскачки счёта. Ниже контроль.
            "sd_a": sd_a,
            "sd_b": sd_b,
            "ir_a": alpha_a / rsd_a if rsd_a else 0.0,
            "ir_b": alpha_b / rsd_b if rsd_b else 0.0,
            # Длина периода кривой своя у каждого счёта и одна в обеих половинах:
            # мера «за период» растёт с ней и переносилась бы без всякого умения.
            "step": step,
            "sr_a": fmean(mine[:half]) / sd_a if sd_a else 0.0,
            "sr_b": fmean(mine[half:]) / sd_b if sd_b else 0.0,
            # Первый период начинается за шаг до первой отметки; раздел — конец
            # последнего периода первой половины.
            "start_ms": _ms(stamps[0]) - int(step * DAY_MS),
            "cut_ms": _ms(stamps[half - 1]),
            "end_ms": _ms(stamps[-1]) + DAY_MS,
            "av": [(int(t), float(v)) for t, v in av],
        }
    return out


def compact(f: dict) -> dict:
    return {
        "t": f["time"],
        "c": f["coin"],
        "px": f["px"],
        "sz": f["sz"],
        "s": f["side"],
        "d": f.get("dir"),
        "sp": f.get("startPosition"),
        "pnl": f.get("closedPnl"),
        "fee": f.get("fee"),
        "x": f.get("crossed"),
        "liq": bool(f.get("liquidation")),
    }


def stage_fills(root: Path, accounts: dict[str, dict], pause: float, limit: int) -> int:
    import httpx

    folder = root / DIR / "fills"
    folder.mkdir(parents=True, exist_ok=True)
    todo = [a for a in accounts if not (folder / f"{a}.jsonl.gz").exists()][:limit]
    print(f"счетов в выборке {len(accounts)}, качаем сделки {len(todo)}")
    failed = 0
    with httpx.Client(timeout=90) as client:
        for n, address in enumerate(todo, 1):
            fills: list[dict] = []
            start = accounts[address]["start_ms"]
            ok = True
            while len(fills) < MAX_FILLS:
                batch = _post(
                    client, {"type": "userFillsByTime", "user": address, "startTime": start}, 6
                )
                time.sleep(pause)
                if batch is None:
                    ok = False
                    break
                if not batch:
                    break
                fills.extend(compact(f) for f in batch)
                if len(batch) < PAGE:
                    break
                start = batch[-1]["time"] + 1
            if not ok:
                # Файл не пишется: неполный ряд неотличим от полного, докачается в следующий раз.
                failed += 1
                continue
            part = folder / f"{address}.jsonl.gz.part"
            with gzip.open(part, "wt", encoding="utf-8") as fh:
                for f in fills:
                    fh.write(json.dumps(f, separators=(",", ":")) + "\n")
            part.replace(folder / f"{address}.jsonl.gz")
            if n % 50 == 0:
                print(f"  {n}/{len(todo)}, отказов {failed}")
    print(f"готово: {len(todo) - failed}, отказов {failed} (перезапуск докачает только их)")
    return 0


def features(fills: list[dict], acc: dict, floor: float) -> dict | None:
    """Плечо и прибыль по направлениям — отдельно для каждой половины."""
    av = acc["av"]
    cut, start, end = acc["cut_ms"], acc["start_ms"], acc["end_ms"]
    pos: dict[str, float] = {}
    px: dict[str, float] = {}
    lev: dict[str, list[float]] = {"a": [], "b": []}
    longs = {"a": 0.0, "b": 0.0}
    shorts = {"a": 0.0, "b": 0.0}
    liq = {"a": 0, "b": 0}
    # Доля пассивных исполнений (`crossed` = нет): маркет-мейкер зарабатывает спредом
    # и ребейтами, и его доход стабилен устройством бизнеса, а не умением угадывать.
    passive = {"a": 0, "b": 0}
    count = {"a": 0, "b": 0}
    value = None
    j = 0
    first = None
    for f in fills:
        t = f["t"]
        if f.get("d") in SPOT_DIRS or t < start or t > end:
            continue
        first = t if first is None else first
        while j < len(av) and av[j][0] <= t:
            value = av[j][1]
            j += 1
        coin = f["c"]
        size = float(f["sz"])
        pos[coin] = float(f.get("sp") or 0) + (size if f["s"] == "B" else -size)
        px[coin] = float(f["px"])
        h = "a" if t < cut else "b"
        if value and value >= floor:
            gross = sum(abs(p) * px[c] for c, p in pos.items())
            lev[h].append(gross / value)
        pnl = float(f.get("pnl") or 0)
        if f.get("d") in LONG_CLOSE:
            longs[h] += pnl
        elif f.get("d") in SHORT_CLOSE:
            shorts[h] += pnl
        if f.get("liq"):
            liq[h] += 1
        count[h] += 1
        if f.get("x") is False:
            passive[h] += 1
    if len(lev["a"]) < 20 or len(lev["b"]) < 20:
        return None
    caps = {
        "a": [v for t, v in av if start <= t < cut],
        "b": [v for t, v in av if cut <= t <= end],
    }
    if not caps["a"] or not caps["b"]:
        return None
    cap_a, cap_b = fmean(caps["a"]), fmean(caps["b"])
    return {
        "lev_a": median(lev["a"]),
        "lev_b": median(lev["b"]),
        "lev90_a": quantiles(lev["a"], n=10)[-1],
        "long_a": longs["a"] / cap_a * 100,
        "long_b": longs["b"] / cap_b * 100,
        "short_a": shorts["a"] / cap_a * 100,
        "short_b": shorts["b"] / cap_b * 100,
        "liq_a": liq["a"],
        "maker_a": passive["a"] / count["a"] if count["a"] else 0.0,
        # Покрыта ли первая половина целиком: у самых активных биржа отдаёт только хвост.
        "covered": first is not None and first <= start + 30 * DAY_MS,
    }


def _line(label: str, xs: list[float], ys: list[float]) -> None:
    n = len(xs)
    if n < 20:
        print(f"  {label:<38} мало счетов ({n})")
        return
    rho = spearman(xs, ys)
    order = sorted(range(n), key=lambda i: -xs[i])
    k = max(3, n // 4)
    top = median(ys[i] for i in order[:k])
    rest = median(ys[i] for i in order[k:])
    noise = 2 / sqrt(n)
    flag = "  <-- за шумом" if abs(rho) > noise else ""
    print(f"  {label:<38} {rho:>+6.2f}  (±{noise:.2f})  {top:>+9.3f}  {rest:>+9.3f}{flag}")


def partial(xs: list[float], ys: list[float], zs: list[float]) -> float:
    """Ранговая связь x и y при равном z: снимает общую зависимость обоих от z."""
    rxy, rxz, ryz = spearman(xs, ys), spearman(xs, zs), spearman(ys, zs)
    den = sqrt((1 - rxz**2) * (1 - ryz**2))
    return (rxy - rxz * ryz) / den if den else 0.0


def _partial_line(label: str, xs: list[float], ys: list[float], zs: list[float]) -> None:
    n = len(xs)
    if n < 20:
        print(f"  {label:<38} мало счетов ({n})")
        return
    rho = partial(xs, ys, zs)
    noise = 2 / sqrt(n)
    flag = "  <-- за шумом" if abs(rho) > noise else ""
    print(f"  {label:<38} {rho:>+6.2f}  (±{noise:.2f})  {'—':>9}  {'—':>9}{flag}")


def _header(label: str) -> None:
    print(f"  {label:<38} {'связь':>6}  {'шум':>7}  {'верх 1/4':>9}  {'остальные':>9}")


def _pairs(rows: list[tuple[dict, dict]], side: int, key_a: str, key_b: str) -> tuple[list, list]:
    return [r[side][key_a] for r in rows], [r[side][key_b] for r in rows]


def persistence(title: str, rows: list[tuple[dict, dict]]) -> None:
    print(f"\n{title}: {len(rows)} счетов")
    _header("что переносится")
    _line("доход на риск, вся половина", *_pairs(rows, 0, "sr_a", "sr_b"))
    _line("сверх рынка, вся половина", *_pairs(rows, 0, "alpha_a", "alpha_b"))
    # Контроль масштаба: если «сверх рынка» переносится, а на единицу риска и при
    # равной раскачке — нет, то переносится раскачка счёта, а не умение.
    _line("  раскачка счёта (разброс за период)", *_pairs(rows, 0, "sd_a", "sd_b"))
    _line("  сверх рынка на единицу риска", *_pairs(rows, 0, "ir_a", "ir_b"))
    _partial_line(
        "  сверх рынка при равной раскачке",
        *_pairs(rows, 0, "alpha_a", "alpha_b"),
        [a["sd_a"] for a, _ in rows],
    )
    _partial_line(
        "  на единицу риска при равном шаге",
        *_pairs(rows, 0, "ir_a", "ir_b"),
        [a["step"] for a, _ in rows],
    )
    if len(rows) >= 20:
        pos_a = sum(1 for a, _ in rows if a["alpha_a"] > 0) / len(rows)
        pos_b = sum(1 for a, _ in rows if a["alpha_b"] > 0) / len(rows)
        tie = spearman(*_pairs(rows, 0, "alpha_a", "sd_a"))
        print(
            f"    (с плюсом сверх рынка: {pos_a:.0%} в 1-й, {pos_b:.0%} во 2-й; "
            f"связь «сверх рынка» с раскачкой в 1-й: {tie:+.2f})"
        )
    for reg in REGIMES:
        _line(
            f"сверх рынка в режиме «{reg}»",
            [a["a"][reg][2] for a, _ in rows],
            [a["b"][reg][2] for a, _ in rows],
        )
        _partial_line(
            "  то же на риск, при равном шаге",
            [a["a"][reg][2] / a["sd_a"] for a, _ in rows],
            [a["b"][reg][2] / a["sd_b"] for a, _ in rows],
            [a["step"] for a, _ in rows],
        )
    _line("прибыль по лонгам, % капитала", *_pairs(rows, 1, "long_a", "long_b"))
    _line("прибыль по шортам, % капитала", *_pairs(rows, 1, "short_a", "short_b"))


def stage_link(root: Path, accounts: dict[str, dict], floor: float) -> int:
    folder = root / DIR / "fills"
    rows: list[tuple[dict, dict]] = []
    files = 0
    for address, acc in accounts.items():
        path = folder / f"{address}.jsonl.gz"
        if not path.exists():
            continue
        files += 1
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            fills = [json.loads(ln) for ln in fh if ln.strip()]
        feats = features(fills, acc, floor)
        if feats:
            rows.append((acc, feats))
    covered = sum(1 for _, f in rows if f["covered"])
    print(
        f"счетов в выборке {len(accounts)}, со сделками {files}, "
        f"с плечом в обеих половинах {len(rows)}"
    )
    print(f"  первая половина покрыта сделками целиком: {covered} из {len(rows)}")
    if len(rows) < 30:
        print("слишком мало для вывода")
        return 0

    q = quantiles(sorted(f["lev_a"] for _, f in rows), n=10)
    print(
        f"\nплечо в первой половине (медиана по сделкам): "
        f"10% {q[0]:.2f} · 50% {q[4]:.2f} · 90% {q[8]:.2f}"
    )
    sq = quantiles(sorted(a["step"] for a, _ in rows), n=10)
    print(f"  шаг кривой, суток: 10% {sq[0]:g} · 50% {sq[4]:g} · 90% {sq[8]:g}")
    # Группа «до ×3» включает и счета, где позиции — пятая часть капитала. Буква
    # условия владельца — «торгует с плечом ×2–3», это полоса, а не потолок.
    band = [r for r in rows if 1.0 < r[1]["lev_a"] <= CALM]
    print(f"  в полосе ×1–{CALM:g} (плечо действительно берётся): {len(band)}")
    buckets = {
        f"до ×{CALM:g}": [r for r in rows if r[1]["lev_a"] <= CALM],
        f"×{CALM:g}–{HOT:g}": [r for r in rows if CALM < r[1]["lev_a"] <= HOT],
        f"больше ×{HOT:g}": [r for r in rows if r[1]["lev_a"] > HOT],
    }
    print(
        f"\n{'группа':<12} {'счетов':>6} {'дох/риск во 2-й':>16} "
        f"{'сверх рынка во 2-й':>19} {'с ликвидациями в 1-й':>21}"
    )
    for name, grp in buckets.items():
        if not grp:
            continue
        print(
            f"{name:<12} {len(grp):>6} {median(a['sr_b'] for a, _ in grp):>+16.3f} "
            f"{median(a['alpha_b'] for a, _ in grp):>+19.3f} "
            f"{sum(1 for _, f in grp if f['liq_a']) / len(grp):>20.0%}"
        )

    lev_a = [f["lev_a"] for _, f in rows]
    print("\nпривычка и плечо как признак (все счета):")
    _header("что с чем")
    _line("плечо 1-й → плечо 2-й половины", lev_a, [f["lev_b"] for _, f in rows])
    _line("плечо 1-й → доход на риск во 2-й", lev_a, [a["sr_b"] for a, _ in rows])
    _line("плечо 1-й → сверх рынка во 2-й", lev_a, [a["alpha_b"] for a, _ in rows])
    _partial_line(
        "  → на единицу риска, при равном шаге",
        lev_a,
        [a["ir_b"] for a, _ in rows],
        [a["step"] for a, _ in rows],
    )

    persistence(f"ПЕРЕНОС ВНУТРИ ГРУППЫ «до ×{CALM:g}»", buckets[f"до ×{CALM:g}"])
    persistence(f"ВНУТРИ ПОЛОСЫ ×1–{CALM:g}", band)

    # Кто несёт остаток переноса: если он живёт у пассивных счетов, это бизнес
    # маркет-мейкера, и направленному боту учиться у него нечему.
    mq = quantiles(sorted(f["maker_a"] for _, f in rows), n=10)
    print(
        f"\nдоля пассивных сделок в 1-й половине: "
        f"10% {mq[0]:.2f} · 50% {mq[4]:.2f} · 90% {mq[8]:.2f}"
    )
    makers = [r for r in rows if r[1]["maker_a"] >= MAKER]
    takers = [r for r in rows if r[1]["maker_a"] < MAKER]
    for name, grp in (("пассивные", makers), ("агрессивные", takers)):
        if grp:
            print(
                f"  {name:<12} счетов {len(grp):>4}, плечо (медиана) "
                f"{median(f['lev_a'] for _, f in grp):.2f}, "
                f"первая половина покрыта {sum(1 for _, f in grp if f['covered'])}"
            )
    persistence(f"ПАССИВНЫЕ (мейкер ≥ {MAKER:.0%} сделок)", makers)
    persistence(f"АГРЕССИВНЫЕ (мейкер < {MAKER:.0%} сделок)", takers)
    persistence(
        f"для сравнения — плечо больше ×{CALM:g}",
        [r for r in rows if r[1]["lev_a"] > CALM],
    )
    print(
        "\nЧитать так: условие «до ×3» спасает идею, только если в группе «до ×3» перенос\n"
        "умения (строки «сверх рынка») заметно сильнее, чем у остальных, и выходит за шум.\n"
        "Прибыль по лонгам и шортам — это во многом стиль и фаза рынка, а не умение.\n"
        "Группа с большим плечом — выжившие (разорившихся нет в списке), её числа завышены."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", required=True, choices=("fills", "link"))
    ap.add_argument("--root", default="data")
    ap.add_argument("--floor", type=float, default=50_000, help="капитал, ниже которого не считаем")
    ap.add_argument("--band", type=float, default=2.0, help="граница режима, %% хода BTC за неделю")
    ap.add_argument("--min-periods", type=int, default=6, help="минимум периодов режима в половине")
    ap.add_argument("--pause", type=float, default=1.2, help="пауза между запросами сделок, с")
    ap.add_argument("--limit", type=int, default=5000, help="сколько счетов качать за прогон")
    args = ap.parse_args()

    root = Path(args.root)
    accounts = sample(root, args.floor, args.band, args.min_periods)
    if args.stage == "fills":
        return stage_fills(root, accounts, args.pause, args.limit)
    return stage_link(root, accounts, args.floor)


if __name__ == "__main__":
    raise SystemExit(main())
