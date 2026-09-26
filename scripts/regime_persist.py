#!/usr/bin/env python
"""Кто зарабатывает в росте, в падении и в боковике — и повторяется ли это.

Гипотеза владельца (26.09.2026): найти трейдеров, которые стабильно зарабатывают в своём
режиме рынка, понять, почему, и собрать гибрид «лучший в росте + лучший в падении +
лучший в боковике». Прежняя проверка (`copytrade_screen.py --stage persist`) смотрела на
результат за всю историю разом и переноса не нашла. Но она могла его размазать: трейдер,
сильный в падении и слабый в росте, в сумме выглядит средним. Здесь каждый режим
меряется отдельно.

Режим — ход BTC за тот же период кривой, приведённый к неделе: ниже −2% падение, выше +2%
рост, между — боковик. Это разметка «задним числом», и для ЭТОГО вопроса она правильная:
спрашивается не «можно ли угадать режим», а «кто в нём зарабатывает». Угадывание режима —
отдельная задача гибрида, и она проверяется отдельно.

Три меры, потому что они отвечают на разные вопросы:

* **сырая доходность** в периодах режима — СТИЛЬ. Кто держит шорт, тот зарабатывает
  на падении в обеих половинах; перенос здесь ожидаем и умением не является;
* **доход на риск** — то же, делённое на разброс счёта за половину, чтобы плечо ×20
  не перевешивало спокойных;
* **сверх рынка** — доходность минус бета половины × ход рынка. Единственная мера
  УМЕНИЯ: зарабатывает ли трейдер в падении больше, чем даёт его постоянный перекос.

Шум связи — ±2/√n (2σ); мер и режимов девять, поэтому засчитывается только то, что
выходит за шум с запасом и держит знак по обеим мерам навыка.

    python scripts/regime_persist.py --root /app/data
"""

from __future__ import annotations

import argparse
import json
import sys
from math import sqrt
from pathlib import Path
from statistics import fmean, median, pstdev

sys.path.insert(0, str(Path(__file__).resolve().parent))

from copytrade_screen import DIR, beta_alpha, btc_weekly, returns  # noqa: E402

REGIMES = ("падение", "боковик", "рост")
MEASURES = ("сырая доходность (стиль)", "доход на риск", "сверх рынка (умение)")


def regime(market_pct: float, step_days: float, band: float) -> str:
    weekly = market_pct * 7 / step_days
    if weekly < -band:
        return "падение"
    if weekly > band:
        return "рост"
    return "боковик"


def half_scores(
    mine: list[float], market: list[float], step_days: float, band: float, min_periods: int
) -> dict[str, tuple[float, float, float]] | None:
    """По режиму: (сырая средняя, средняя/разброс половины, средняя сверх беты)."""
    sd = pstdev(mine)
    if sd <= 0:
        return None
    beta, _ = beta_alpha(mine, market)
    out: dict[str, tuple[float, float, float]] = {}
    for reg in REGIMES:
        idx = [i for i, m in enumerate(market) if regime(m, step_days, band) == reg]
        if len(idx) < min_periods:
            return None
        raw = fmean(mine[i] for i in idx)
        excess = fmean(mine[i] - beta * market[i] for i in idx)
        out[reg] = (raw, raw / sd, excess)
    return out


def ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(xs: list[float], ys: list[float]) -> float:
    rx, ry = ranks(xs), ranks(ys)
    mx, my = fmean(rx), fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data")
    ap.add_argument("--floor", type=float, default=50_000, help="капитал, ниже которого не считаем")
    ap.add_argument("--band", type=float, default=2.0, help="граница режима, %% хода BTC за неделю")
    ap.add_argument("--min-periods", type=int, default=6, help="минимум периодов режима в половине")
    args = ap.parse_args()

    root = Path(args.root)
    folder = root / DIR
    cands = json.loads((folder / "candidates.json").read_text())
    losers = sum(1 for c in cands if c["pnl_all"] <= 0)
    print(f"кандидатов в списке {len(cands)}, из них убыточных за всё время: {losers}")
    if not losers:
        print(
            "  ВНИМАНИЕ: в выборке только прибыльные счета. Отбор по сумме двух половин\n"
            "  сам создаёт ОТРИЦАТЕЛЬНУЮ связь между ними (плохая первая половина попала\n"
            "  в список, только если вторая была хорошей). Перенос этим занижается."
        )

    prices = btc_weekly(root)
    rows: list[tuple[dict, dict]] = []
    seen = 0
    for line in (folder / "portfolios.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        seen += 1
        rec = json.loads(line)
        mine, market, stamps = returns(rec["portfolio"], prices, args.floor)
        if len(mine) < 40:
            continue
        gaps = [(b - a).days for a, b in zip(stamps, stamps[1:], strict=False)]
        step = max(median(gaps), 1.0)
        half = len(mine) // 2
        a = half_scores(mine[:half], market[:half], step, args.band, args.min_periods)
        b = half_scores(mine[half:], market[half:], step, args.band, args.min_periods)
        if a and b:
            rows.append((a, b))

    n = len(rows)
    print(f"кривых {seen}, с историей на две половины и всеми тремя режимами в каждой: {n}")
    if n < 30:
        print("слишком мало для вывода")
        return 0
    noise = 2 / sqrt(n)
    print(f"шум связи (2σ): ±{noise:.2f}\n")
    print(
        f"{'режим':<9} {'мера':<26} {'связь A→B':>9}  "
        f"{'верх. 1/4 в B':>13}  {'остальные в B':>13}"
    )
    for reg in REGIMES:
        for m, label in enumerate(MEASURES):
            xs = [r[0][reg][m] for r in rows]
            ys = [r[1][reg][m] for r in rows]
            rho = spearman(xs, ys)
            order = sorted(range(n), key=lambda i: -xs[i])
            k = max(3, n // 4)
            top = median(ys[i] for i in order[:k])
            rest = median(ys[i] for i in order[k:])
            flag = "  <-- за шумом" if abs(rho) > noise else ""
            print(f"{reg:<9} {label:<26} {rho:>+9.2f}  {top:>+13.3f}  {rest:>+13.3f}{flag}")
        print()
    print(
        "Читать так: «стиль» переносится почти всегда — это направление позиции, а не навык.\n"
        "Идея «брать лучшего в своём режиме» жива, только если за шум выходит «сверх рынка»\n"
        "в том же режиме, и верхняя четверть первой половины обгоняет остальных во второй."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
