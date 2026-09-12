#!/usr/bin/env python
"""Приток стейблкоинов (кандидат A6): предсказывает ли эмиссия долларов цену.

Единственный из девяти кандидатов, чей источник не коррелирует с премией за плечо. Ставка
фандинга, базис квартального контракта и премиум-индекс — это одно и то же явление в трёх
видах (связь 0.91). Эмиссия стейблкоинов — деньги, входящие в систему извне, и её связь
с ценой заранее неизвестна.

Источник — открытая история DefiLlama по всем сетям сразу (Ethereum, Tron, Solana, BSC…),
дневная, с ноября 2017. Ключ не нужен. Прямой сбор через Etherscan дал бы только текущую
эмиссию: истории у бесплатного тарифа нет, её пришлось бы накапливать годами вперёд.

Две предосторожности, без которых замер соврал бы:

* **непересекающиеся окна.** На горизонте в десять дней соседние дни перекрываются
  на девяносто процентов; если считать их независимыми, коридор шума окажется втрое уже
  настоящего. Поэтому наблюдения берутся с шагом в горизонт.
* **связь с ПРОШЛОЙ ценой.** Стейблкоины печатают, когда рынок уже вырос. Если сигнал
  сильнее связан с прошлым движением, чем с будущим, это эхо, а не предсказание.

    python scripts/stablecoin_flow.py --root /app/data
    python scripts/stablecoin_flow.py --refresh --window 14 --root /app/data
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

URL = "https://stablecoins.llama.fi/stablecoincharts/all"
CACHE = "stablecoins.json"


def fetch(path: Path) -> list[dict]:
    import httpx

    r = httpx.get(URL, timeout=120)
    r.raise_for_status()
    path.write_text(r.text, encoding="utf-8")
    return json.loads(r.text)


def supply(rows: list[dict]) -> dict[date, float]:
    """Суточная эмиссия в долларах. Нулевые хвосты — незакрытый день, их отбрасываем."""
    out: dict[date, float] = {}
    for r in rows:
        val = (r.get("totalCirculatingUSD") or {}).get("peggedUSD") or 0
        if val <= 0:
            continue
        out[datetime.fromtimestamp(int(r["date"]), UTC).date()] = float(val)
    return out


def corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return 0.0
    mx, my = fmean(xs), fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = sqrt(sum((x - mx) ** 2 for x in xs))
    dy = sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else 0.0


def stats(vals: list[float]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    return fmean(vals), (2 * stdev(vals) / sqrt(len(vals)) if len(vals) > 1 else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instrument", default="BTC/USDT")
    ap.add_argument("--window", type=int, default=7, help="за сколько дней считать приток")
    ap.add_argument("--horizons", default="7,14,30")
    ap.add_argument("--tail-pct", type=float, default=25.0, help="размер края, %%")
    ap.add_argument("--refresh", action="store_true", help="перекачать историю")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    cache = root / CACHE
    rows = fetch(cache) if args.refresh or not cache.exists() else json.loads(cache.read_text())
    sup = supply(rows)
    if len(sup) < 400:
        print("истории эмиссии слишком мало")
        return 1

    cs = CandleStore(root)
    first, last = min(sup), max(sup)
    bars = cs.query(
        "select ts, close::DOUBLE as close from {candles} where ts >= ? and ts < ? order by ts",
        "binance",
        args.instrument,
        "1d",
        params=[
            datetime(first.year, first.month, first.day),
            datetime(last.year, last.month, last.day) + timedelta(days=1),
        ],
    )
    px = {r["ts"].date(): float(r["close"]) for r in bars if r["close"]}
    days = sorted(d for d in px if d in sup)
    print(f"эмиссия: {len(sup)} дней ({first} … {last}) | цена {args.instrument}: {len(days)} дней")
    print(f"сейчас в обращении: ${sup[last] / 1e9:.1f} млрд\n")

    w = args.window
    for h in [int(x) for x in args.horizons.split(",") if x.strip()]:
        # Шаг = горизонт: соседние окна не должны пересекаться, иначе шум занижен.
        obs: list[tuple[date, float, float, float]] = []
        for i in range(w, len(days) - h, h):
            d0, d1 = days[i - w], days[i]
            if sup[d0] <= 0 or px[d0] <= 0 or px[d1] <= 0:
                continue
            flow = (sup[d1] / sup[d0] - 1) * 100
            past = (px[d1] / px[d0] - 1) * 100
            fwd = (px[days[i + h]] / px[d1] - 1) * 100
            obs.append((d1, flow, past, fwd))
        if len(obs) < 40:
            print(f"горизонт {h}: наблюдений мало ({len(obs)})")
            continue

        flows = sorted(o[1] for o in obs)
        k = max(1, int(len(flows) * args.tail_pct / 100))
        lo_edge, hi_edge = flows[k], flows[-k]
        low = [o[3] for o in obs if o[1] <= lo_edge]
        high = [o[3] for o in obs if o[1] >= hi_edge]
        m_all, e_all = stats([o[3] for o in obs])
        m_lo, e_lo = stats(low)
        m_hi, e_hi = stats(high)
        spread = m_hi - m_lo
        noise = sqrt(e_hi**2 + e_lo**2)
        print(
            f"горизонт {h} дн | непересекающихся окон {len(obs)} "
            f"| приток за {w} дн: край ≤{lo_edge:.2f}% / ≥{hi_edge:.2f}%"
        )
        print(f"  слабый приток  {m_lo:>8.2f}%  ±{e_lo:.2f}")
        print(f"  сильный приток {m_hi:>8.2f}%  ±{e_hi:.2f}")
        print(f"  обычное окно   {m_all:>8.2f}%  ±{e_all:.2f}")
        verdict = "в пределах шума" if abs(spread) < noise else "ВЫХОДИТ ЗА ШУМ"
        print(f"  разброс        {spread:>8.2f}   шум ±{noise:.2f}   {verdict}")
        print(
            f"  связь притока с ПРОШЛОЙ ценой {corr([o[1] for o in obs], [o[2] for o in obs]):+.2f}"
            f"   с будущей {corr([o[1] for o in obs], [o[3] for o in obs]):+.2f}"
        )
        years: dict[int, tuple[list[float], list[float]]] = {}
        for d, flow, _, fwd in obs:
            hi_vals, lo_vals = years.setdefault(d.year, ([], []))
            if flow >= hi_edge:
                hi_vals.append(fwd)
            elif flow <= lo_edge:
                lo_vals.append(fwd)
        print("  разброс по годам:", end=" ")
        for year in sorted(years):
            hi_vals, lo_vals = years[year]
            ok = hi_vals and lo_vals
            print(f"{year}:{fmean(hi_vals) - fmean(lo_vals):+.1f}" if ok else f"{year}:—", end="  ")
        print("\n")
    print(
        "Читать так: связь с прошлой ценой выше, чем с будущей, — это эхо уже случившегося\n"
        "роста, а не предсказание. Разброс меньше шума — предсказания нет вовсе."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
