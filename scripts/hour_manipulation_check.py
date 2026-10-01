"""«Манипуляция на часе» Pifagor — прямая проверка правила на часовых свечах BTC.

Правило словами автора (посты @pifagortrade 367, 403, 404, 671 за 2021 и 3071 за 30.10.2025):
на часовом графике после импульса вверх от минимума L до максимума H цена «прошивает все
уровни фибы, не дав им отработать» и уходит ниже уровня 1 (ниже L). Это «манипуляция»:
покупка ниже уровня 1, короткий стоп, цель — возврат к 0.618 или 0.5 сетки.
Заявления автора: «100 из 100», «не возвращались за всю историю всего пару раз на пике
2017 и 2021».

Формализация (параметры — сетка, ищется плато):
  окно W часов: H = максимум за [t-W, t), L = минимум ДО момента H внутри окна,
  импульс (H-L)/L >= m; событие — первое часовое закрытие ниже L после H;
  вход — открытие следующего часа; цели T618 = H - 0.618(H-L), T50 = (H+L)/2;
  стоп — минимум часа пробоя минус 1%; тайм-аут 30 суток; круг издержек 0.10%.
Контроль: вход в случайный час (каждые 6 часов истории) с ТАКИМ ЖЕ ходом цены за W часов
(±3 п.) и теми же расстояниями до цели и стопа в процентах. Разница «событие − контроль» —
то, что добавляет само правило сверх «цена упала».

    python scripts/hour_manipulation_check.py --csv <bitstamp_btcusd_1h.csv>

CSV: индекс времени UTC, колонки open, high, low, close (так пишет screenshot_signals_check).
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

ROUND_COST = 0.0010
TIMEOUT_H = 24 * 30
STOP_PAD = 0.01
MOVE_BAND = 0.03


def first_touch(hi: np.ndarray, lo: np.ndarray, cl: np.ndarray, i0: int, entry: float,
                target: float, stop: float, timeout: int) -> tuple[str, float, int]:
    """Кто первым: цель или стоп, начиная с бара i0 (вход по его открытию). Оба в одном
    баре — считаем стоп (консервативно). Тайм-аут — выход по закрытию."""
    end = min(len(hi), i0 + timeout)
    s = lo[i0:end] <= stop
    g = hi[i0:end] >= target
    js = int(s.argmax()) if s.any() else end
    jg = int(g.argmax()) if g.any() else end
    if js == end and jg == end:
        return "timeout", cl[end - 1] / entry - 1, end - i0
    if js <= jg:
        return "stop", stop / entry - 1, js
    return "target", target / entry - 1, jg


def reached(hi: np.ndarray, i0: int, target: float, horizon: int) -> bool:
    return bool((hi[i0: i0 + horizon] >= target).any())


def events(df: pd.DataFrame, w: int, m: float) -> list[dict]:
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    out, last_swing = [], None
    for t in range(w, len(df) - 1):
        seg_hi = hi[t - w: t]
        k = int(seg_hi.argmax())
        if k == 0:
            continue
        H = seg_hi[k]
        L = lo[t - w: t - w + k].min()
        if (H - L) / L < m or cl[t] >= L:
            continue
        # первое закрытие ниже L после максимума
        if (cl[t - w + k: t] < L).any():
            continue
        swing = (t - w + k, round(L, 2))
        if swing == last_swing:
            continue
        last_swing = swing
        out.append({"t": t, "H": H, "L": L, "break_low": lo[t]})
    # не больше одного события на окно: иначе одна просадка считается много раз
    thinned, last_t = [], -10**9
    for e in out:
        if e["t"] - last_t >= w:
            thinned.append(e)
            last_t = e["t"]
    return thinned


def evaluate(df: pd.DataFrame, w: int, m: float, level: float, ctrl_idx: np.ndarray) -> dict:
    hi, lo, op, cl = (df[c].to_numpy() for c in ("high", "low", "open", "close"))
    move = cl / np.roll(cl, w) - 1
    ev = events(df, w, m)
    rows = []
    for e in ev:
        i0 = e["t"] + 1
        entry = op[i0]
        target = e["H"] - level * (e["H"] - e["L"])
        stop = e["break_low"] * (1 - STOP_PAD)
        if target <= entry or stop >= entry:
            continue
        res, ret, _ = first_touch(hi, lo, cl, i0, entry, target, stop, TIMEOUT_H)
        up, dn = target / entry - 1, 1 - stop / entry
        # контроль: те же проценты до цели и стопа, тот же ход за W часов
        mv = move[e["t"]]
        pool = ctrl_idx[(np.abs(move[ctrl_idx] - mv) <= MOVE_BAND) & (ctrl_idx < e["t"] - TIMEOUT_H)]
        c_rets, c_hit = [], []
        for c in pool[:: max(1, len(pool) // 400)]:
            ce = op[c + 1]
            r, cr, _ = first_touch(hi, lo, cl, c + 1, ce, ce * (1 + up), ce * (1 - dn), TIMEOUT_H)
            c_rets.append(cr)
            c_hit.append(reached(hi, c + 1, ce * (1 + up), 24 * 90))
        rows.append({
            "time": df.index[e["t"]], "entry": entry, "target": target, "up": up, "dn": dn,
            "res": res, "ret": ret - ROUND_COST,
            "hit90": reached(hi, i0, target, 24 * 90),
            "ctrl_ret": (np.mean(c_rets) - ROUND_COST) if c_rets else np.nan,
            "ctrl_hit90": np.mean(c_hit) if c_hit else np.nan, "n_ctrl": len(c_rets),
        })
    return {"w": w, "m": m, "level": level, "rows": pd.DataFrame(rows)}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--windows", default="48,96,168,336")
    ap.add_argument("--impulse", default="0.03,0.05,0.08")
    ap.add_argument("--levels", default="0.618,0.5")
    ap.add_argument("--detail", default="", help="W,m,level — печать событий одной клетки")
    args = ap.parse_args()

    df = pd.read_csv(args.csv, index_col=0, parse_dates=True)
    ctrl_idx = np.arange(400, len(df) - TIMEOUT_H - 2, 6)
    print(f"BTC 1h {df.index[0]} .. {df.index[-1]}, баров {len(df)}; издержки {ROUND_COST:.2%} на круг\n")
    print("  W   имп.  цель  | событий | дошло до цели за 90д: правило / контроль | сделка со стопом: "
          "средняя / контроль / разница ± шум | цель/стоп/тайм-аут")
    cells = []
    for w in map(int, args.windows.split(",")):
        for m in map(float, args.impulse.split(",")):
            for lv in map(float, args.levels.split(",")):
                r = evaluate(df, w, m, lv, ctrl_idx)
                d = r["rows"].dropna()
                if d.empty:
                    continue
                diff = d["ret"] - d["ctrl_ret"]
                se = diff.std(ddof=1) / np.sqrt(len(d)) if len(d) > 1 else np.nan
                outc = d["res"].value_counts()
                cells.append((w, m, lv, diff.mean(), se))
                print(f"{w:>4} {m:>5.0%} {lv:>5} | {len(d):>7} | {d['hit90'].mean():>6.0%} / {d['ctrl_hit90'].mean():>6.0%}"
                      f"                    | {d['ret'].mean():>+6.2%} / {d['ctrl_ret'].mean():>+6.2%} / "
                      f"{diff.mean():>+6.2%} ± {2 * se:.2%} | "
                      f"{outc.get('target', 0)}/{outc.get('stop', 0)}/{outc.get('timeout', 0)}")
    if args.detail:
        w, m, lv = args.detail.split(",")
        r = evaluate(df, int(w), float(m), float(lv), ctrl_idx)["rows"]
        r["year"] = r["time"].dt.year
        print(f"\nСобытия клетки W={w} m={m} цель {lv}:")
        print(r[["time", "entry", "target", "up", "dn", "res", "ret", "ctrl_ret"]].to_string(
            float_format=lambda x: f"{x:,.3f}"))
        g = r.assign(diff=r["ret"] - r["ctrl_ret"]).groupby("year")["diff"].agg(["count", "mean"])
        print("\nПо годам (разница с контролем):\n", g.to_string(float_format=lambda x: f"{x:+.3f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
