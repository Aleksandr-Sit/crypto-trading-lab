#!/usr/bin/env python
"""Отчёты автора канала против исхода его же сигналов по цене.

`tg_export_calls.py` даёт два списка: сигналы (`calls/calls.json`) и отчёты о результате
(`claims.json`: «цель достигнута», «выбило стоп»…; поле `reply_to` — на какой пост ответ).
`calls_check.py` даёт исход каждого сигнала по свечам (`calls_result.csv`). Здесь они
сводятся:

* у сигналов, ушедших в стоп по цене, — что написал автор: «цель» (ложный отчёт),
  «стоп» (честно) или ничего (умолчание);
* у сигналов с отчётом «цель» — что было по цене;
* доля сигналов, о которых автор отчитался, среди дошедших до цели и до стопа —
  витрина видна как разница этих долей.

    python scripts/claims_vs_calls.py <dir> [--include-backfilled]

<dir> — каталог с claims.json и calls_result.csv.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

WIN = {"win", "all_targets", "fix"}
LOSS = {"loss"}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", type=Path)
    ap.add_argument("--include-backfilled", action="store_true",
                    help="учитывать отчёты из перенесённого архива (по умолчанию — нет)")
    a = ap.parse_args()

    claims = json.loads((a.dir / "claims.json").read_text(encoding="utf-8"))
    if not a.include_backfilled:
        claims = [c for c in claims if not c["backfilled"]]
    res = pd.read_csv(a.dir / "calls_result.csv")
    by_call: dict[int, list[dict]] = defaultdict(list)
    for c in claims:
        if c["reply_to"] is not None:
            by_call[int(c["reply_to"])].append(c)

    def said(pid: int) -> str:
        kinds = {c["claim"] for c in by_call.get(pid, [])}
        if kinds & LOSS:
            return "loss"
        if kinds & WIN:
            return "win"
        if "breakeven" in kinds:
            return "breakeven"
        return "other" if kinds else "silent"

    res["said"] = res["post_id"].astype(int).map(said)
    ev = res[res["status"] == "evaluated"]
    print(f"сигналов в calls_result: {len(res)}, оценено по цене: {len(ev)}")
    print(f"статусы: {res['status'].value_counts().to_dict()}\n")

    tab = pd.crosstab(ev["result"], ev["said"], margins=True)
    print("исход по цене (строки) × что написал автор в ответ на сигнал (столбцы)")
    print(tab.to_string(), "\n")

    for r in ("target", "stop"):
        g = ev[ev["result"] == r]
        if len(g):
            print(f"{r:<7} n {len(g):>3}: отчёт о прибыли {(g['said'] == 'win').mean():.0%}, "
                  f"об убытке {(g['said'] == 'loss').mean():.0%}, молчание {(g['said'] == 'silent').mean():.0%}")

    lies = ev[(ev["result"] == "stop") & (ev["said"] == "win")]
    if len(lies):
        print(f"\n«цель» при стопе раньше цели по цене — {len(lies)}; первые 15:")
        for _, r in lies.head(15).iterrows():
            q = next(c for c in by_call[int(r["post_id"])] if c["claim"] in WIN)
            print(f"  {r['posted_at'][:10]} {r['asset']:<6} post {int(r['post_id'])} -> claim {q['post_id']} "
                  f"({q['posted_at'][:10]}): {q['quote'][:90]!r}")

    other = res[res["status"] != "evaluated"]
    wins_other = other[other["said"] == "win"]
    if len(wins_other):
        print(f"\nотчёты «цель» на сигналы без оценки по цене: "
              f"{Counter(wins_other['status']).most_common()}")

    linked = {int(p) for p in res["post_id"]}
    orphan = Counter(c["claim"] for c in claims if c["reply_to"] is not None and int(c["reply_to"]) not in linked)
    print(f"\nотчёты, ответившие НЕ на текстовый сигнал (картинки, другие посты): {dict(orphan)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
