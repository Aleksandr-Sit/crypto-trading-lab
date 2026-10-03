"""Сводка журнала прогнозов по группам: «цель первой» против базы (случайный вход), шум по месяцам.

Вход — CSV от `calls_check.py`. Среднее — по прогнозам; «±» — 95%-интервал (1.96 × ошибка
среднего по среднемесячным разницам «прогноз − база»): прогнозы одного месяца не независимы.
Так же считалась таблица разбора Pifagor 30.09.2026 — цифры сравнимы.
Плюс разбивка отчётов (result_claim) по исходу, если в журнале есть `claimed_result.outcome`.

    python scripts/calls_summary.py <calls_result.csv> [--calls <dir с *.json>]
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd


LONG = {"long", "buy", "up"}
SHORT = {"short", "sell", "down"}


def ci95(d: pd.Series, block: pd.Series) -> float:
    m = d.groupby(block).mean()
    return float(1.96 * m.std(ddof=1) / np.sqrt(len(m))) if len(m) > 1 else float("nan")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--calls", help="каталог журнала — для исходов отчётов")
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    print("статусы:", df["status"].value_counts().to_dict())
    ev = df[df["status"] == "evaluated"].copy()
    ev["win"] = (ev["result"] == "target").astype(float)
    ev["d"] = ev["win"] - ev["base_rate"]
    ev["month"] = ev["posted_at"].str[:7]
    ev["dir"] = ev["direction"].fillna("").str.lower()
    groups = {
        "все": ev,
        "собственные прогнозы (call)": ev[ev["kind"] == "call"],
        "— лонги": ev[(ev["kind"] == "call") & ev["dir"].isin(LONG)],
        "— шорты": ev[(ev["kind"] == "call") & ev["dir"].isin(SHORT)],
        "— BTC": ev[(ev["kind"] == "call") & (ev["asset"].str.upper() == "BTC")],
        "сигналы индикаторов": ev[ev["kind"] == "indicator_signal"],
        "с явным стопом": ev[ev["stop_pct"].notna()],
    }
    print("\n| Группа | n | цель первой | база | разница |\n|---|---|---|---|---|")
    for name, g in groups.items():
        g = g.dropna(subset=["d"])
        if g.empty:
            continue
        print(f"| {name} | {len(g)} | {g['win'].mean():.0%} | {g['base_rate'].mean():.0%} | "
              f"{100 * g['d'].mean():+.1f} ± {100 * ci95(g['d'], g['month']):.1f} п. |")
    c = ev[ev["kind"] == "call"].dropna(subset=["d"])
    by_year = c.groupby(c["posted_at"].str[:4])["d"].agg(["size", "mean"])
    print("\nпо годам (call):", ", ".join(f"{y} {100 * r['mean']:+.0f} п. (n={int(r['size'])})" for y, r in by_year.iterrows()))
    if "open" in set(df["status"]):
        print(f"открытых (срок не истёк): {(df['status'] == 'open').sum()}")
    if args.calls:
        oc = Counter()
        for f in glob.glob(str(Path(args.calls) / "*.json")):
            data = json.load(open(f, encoding="utf-8"))
            if not isinstance(data, list):
                continue
            for x in data:
                if x.get("kind") == "result_claim":
                    oc[(x.get("claimed_result") or {}).get("outcome", "не размечен")] += 1
        print("исходы отчётов (result_claim):", dict(oc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
