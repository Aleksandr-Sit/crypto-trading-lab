"""Проверка публичных прогнозов автора по реальным ценам (журнал «сказал ДО — что было ПОСЛЕ»).

Вход — JSON-массивы извлечённых из постов объектов (схема: channel, post_id, posted_at, kind,
asset, direction, entry | "market", entry_zone, targets, stop, horizon_days, claimed_result …).
Цены — часовые свечи Binance <ASSET>USDT (публичный API, кэш в --cache).

Что считается для kind in {call, indicator_signal}:
  * исполнение: «market» — открытие следующего часа после поста; лимитка — касание цены
    за FILL_DAYS суток (если цена на момент поста уже по нужную сторону — вход сразу);
  * исход: что раньше — ближайшая цель или стоп (оба в одном часе — стоп); без стопа —
    дошла ли цель за горизонт (horizon_days или 90) и худший ход против позиции до этого;
  * базовая частота: та же цель и тот же стоп в процентах от входа в случайные часы истории
    актива (вне окна сделки) — доля, где цель пришла первой. Разница «прогноз − база» и есть
    то, что прогноз добавил сверх «цена иногда ходит на столько-то».
Прогноз «только направление» (по рынку, без цели): ход в сторону прогноза за horizon_days
(или NO_TARGET_DAYS) против среднего такого же хода из случайных часов истории — status
direction_only. Мала выборка — смотреть по каждому, а не по среднему.
Для kind == result_claim с ценой: касалась ли цена заявленной цены за 72 часа ДО поста.

    python scripts/calls_check.py --calls <dir с *.json> --cache <dir> --out <csv>
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

API = "https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=1000&startTime={start}"
FILL_DAYS = 30
DEFAULT_HORIZON = 90
NO_TARGET_DAYS = 30  # горизонт для прогноза «только направление», если автор срока не назвал
LONG = {"long", "buy", "up"}
SHORT = {"short", "sell", "down"}
ALIASES = {"BITCOIN": "BTC", "БИТКОИН": "BTC", "ЭФИР": "ETH", "ETHEREUM": "ETH"}


def candles(asset: str, cache: Path, since: pd.Timestamp) -> pd.DataFrame | None:
    sym = f"{asset}USDT"
    path = cache / f"binance_{sym}_1h.csv"
    if path.exists() and time.time() - path.stat().st_mtime < 86400:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    rows, start = [], int(max(since, pd.Timestamp("2017-08-01")).timestamp() * 1000)
    while True:
        try:
            with urllib.request.urlopen(API.format(sym=sym, start=start), timeout=30) as r:
                chunk = json.load(r)
        except Exception:  # noqa: BLE001 — нет такой пары на Binance
            break
        if not chunk:
            break
        rows += chunk
        start = chunk[-1][0] + 3_600_000
        if len(chunk) < 1000:
            break
        time.sleep(0.15)
    if not rows:
        return None
    df = pd.DataFrame(rows).iloc[:, :5]
    df.columns = ["t", "open", "high", "low", "close"]
    df.index = pd.to_datetime(df.pop("t"), unit="ms")
    df = df.astype(float)
    cache.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    return df


def norm_asset(a: str | None) -> str | None:
    if not a:
        return None
    a = a.upper().replace("/USDT", "").replace("USDT", "").strip()
    return ALIASES.get(a, a)


def first(mask: np.ndarray) -> int:
    return int(mask.argmax()) if mask.any() else -1


def outcome(hi, lo, cl, i0, entry, sign, target, stop, horizon):
    """-> (итог, часов до итога, худший ход против позиции до итога, доходность на горизонте)."""
    end = min(len(hi), i0 + horizon)
    if end <= i0:
        return "no_data", None, None, None
    h, l_ = hi[i0:end], lo[i0:end]
    if sign > 0:
        jt = first(h >= target)
        js = first(l_ <= stop) if stop else -1
        adverse = l_
    else:
        jt = first(l_ <= target)
        js = first(h >= stop) if stop else -1
        adverse = h
    ret_h = sign * (cl[end - 1] / entry - 1)
    if js >= 0 and (jt < 0 or js <= jt):
        res, j = "stop", js
    elif jt >= 0:
        res, j = "target", jt
    else:
        res, j = "timeout", end - i0 - 1
    mae = sign * (adverse[: j + 1].min() / entry - 1) if sign > 0 else sign * (adverse[: j + 1].max() / entry - 1)
    return res, j, mae, ret_h


_BASE: dict = {}


def base_rate(hi, lo, cl, sign, up, dn, horizon, skip: tuple[int, int], rng, key=None) -> float:
    """Доля случайных часов, где цель (+up) пришла раньше стопа (-dn) за горизонт.
    Кэш по (актив, сторона, расстояния с точностью 0.5 п., горизонт): окно сделки при этом
    не исключается — на сотне тысяч часов его вклад мал, а считать заново дорого."""
    if key is not None:
        k = (key, sign, round(up * 200), round((dn or 0) * 200), horizon)
        if k in _BASE:
            return _BASE[k]
    n = len(hi) - horizon - 1
    if n < 500:
        return float("nan")
    idx = rng.choice(np.arange(n), size=min(200, n), replace=False)
    idx = idx[(idx < skip[0]) | (idx > skip[1])]
    wins = 0
    for i in idx:
        e = cl[i]
        t = e * (1 + sign * up)
        s = e * (1 - sign * dn) if dn else None
        res, *_ = outcome(hi, lo, cl, i + 1, e, sign, t, s, horizon)
        wins += res == "target"
    val = wins / len(idx) if len(idx) else float("nan")
    if key is not None:
        _BASE[k] = val
    return val


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calls", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    items = []
    for f in sorted(glob.glob(str(Path(args.calls) / "*.json"))):
        for x in json.load(open(f, encoding="utf-8")):
            x["_file"] = Path(f).stem
            items.append(x)
    first_seen: dict[str, pd.Timestamp] = {}
    for x in items:
        a = norm_asset(x.get("asset"))
        if a and x.get("posted_at"):
            t = pd.Timestamp(x["posted_at"]).tz_localize(None) if pd.Timestamp(x["posted_at"]).tzinfo is None else pd.Timestamp(x["posted_at"]).tz_convert(None)
            first_seen[a] = min(first_seen.get(a, t), t)
    rng = np.random.default_rng(7)
    cache, data, rows = Path(args.cache), {}, []
    for n_item, x in enumerate(items):
        if n_item % 100 == 0:
            print(f"  {n_item}/{len(items)}", flush=True)
        asset = norm_asset(x.get("asset"))
        kind = x.get("kind")
        row = {k: x.get(k) for k in ("_file", "channel", "post_id", "posted_at", "kind", "asset", "direction",
                                     "entry", "targets", "stop", "indicator", "edited", "quote")}
        if kind not in ("call", "indicator_signal", "result_claim") or not asset or asset == "PORTFOLIO":
            rows.append(row | {"status": "skip"})
            continue
        if asset not in data:
            print(f"  свечи {asset}", flush=True)
            data[asset] = candles(asset, cache, first_seen[asset] - pd.Timedelta(days=730))
        df = data[asset]
        if df is None:
            rows.append(row | {"status": "no_prices"})
            continue
        hi, lo, cl, op = (df[c].to_numpy() for c in ("high", "low", "close", "open"))
        ts = pd.Timestamp(x["posted_at"]).tz_convert(None) if pd.Timestamp(x["posted_at"]).tzinfo else pd.Timestamp(x["posted_at"])
        i_post = int(df.index.searchsorted(ts.ceil("h")))
        if i_post >= len(df) - 1:
            rows.append(row | {"status": "too_recent"})
            continue
        px = cl[i_post - 1] if i_post > 0 else op[i_post]
        row["price_at_post"] = px

        if kind == "result_claim":
            cr = x.get("claimed_result") or {}
            p = cr.get("price")
            if p:
                w0 = max(0, i_post - 72)
                row["claim_price"] = p
                row["claim_price_seen_72h"] = bool(lo[w0:i_post].min() <= p <= hi[w0:i_post].max()) if i_post > w0 else None
                row["status"] = "claim_checked"
            else:
                row["status"] = "claim_no_price"
            rows.append(row)
            continue

        targets = [t for t in (x.get("targets") or []) if isinstance(t, (int, float))]
        d = (x.get("direction") or "").lower()
        sign = 1 if d in LONG else -1 if d in SHORT else 0
        if not sign and targets:
            sign = 1 if targets[0] > px else -1
        if not sign or not targets:
            # направление без цели: ход в сторону прогноза против того же хода из случайных часов
            if sign and not (x.get("entry") not in (None, "market") or x.get("entry_zone")):
                h = int((x.get("horizon_days") or NO_TARGET_DAYS) * 24)
                if i_post + h < len(df):
                    n = len(cl) - h - 1
                    idx = rng.choice(np.arange(n), size=min(2000, n), replace=False)
                    row |= {"status": "direction_only", "dir_horizon_days": h // 24,
                            "dir_ret": sign * (cl[i_post + h] / op[i_post] - 1),
                            "dir_base": float(np.mean(sign * (cl[idx + h] / cl[idx] - 1)))}
                    rows.append(row)
                    continue
            rows.append(row | {"status": "no_target"})
            continue
        horizon = int((x.get("horizon_days") or DEFAULT_HORIZON) * 24)
        entry = x.get("entry")
        zone = x.get("entry_zone")
        if isinstance(zone, list) and len(zone) == 2 and all(isinstance(z, (int, float)) for z in zone):
            entry = max(zone) if sign > 0 else min(zone)
        if entry in (None, "market") or not isinstance(entry, (int, float)):
            i_fill, e = i_post, op[i_post]
        else:
            e = float(entry)
            if (sign > 0 and e >= px) or (sign < 0 and e <= px):
                i_fill, e = i_post, op[i_post]
            else:
                end = min(len(df), i_post + FILL_DAYS * 24)
                j = first(lo[i_post:end] <= e) if sign > 0 else first(hi[i_post:end] >= e)
                if j < 0:
                    rows.append(row | {"status": "not_filled"})
                    continue
                i_fill = i_post + j
        tgt = sorted(targets, reverse=sign < 0)
        tgt = [t for t in tgt if sign * (t - e) > 0]
        if not tgt:
            rows.append(row | {"status": "target_behind_entry"})
            continue
        stop = x.get("stop") if isinstance(x.get("stop"), (int, float)) else None
        if stop and sign * (e - stop) <= 0:
            stop = None
        res, j, mae, ret_h = outcome(hi, lo, cl, i_fill + (1 if i_fill == i_post else 0), e, sign, tgt[0], stop, horizon)
        if res == "timeout" and i_fill + horizon >= len(df):
            # срок не истёк — не промах; до 01.10.2026 такие шли в счёт как «цель не пришла»
            rows.append(row | {"status": "open", "fill_price": e, "t1": tgt[0]})
            continue
        up = sign * (tgt[0] / e - 1)
        dn = sign * (e - stop) / e if stop else None
        row |= {
            "status": "evaluated", "fill_price": e, "fill_time": str(df.index[i_fill]),
            "t1": tgt[0], "t1_pct": up, "stop_pct": dn, "result": res,
            "days_to_result": (j / 24) if j is not None else None, "mae": mae, "ret_horizon": ret_h,
            "last_target_hit": bool(outcome(hi, lo, cl, i_fill + 1, e, sign, tgt[-1], None, horizon)[0] == "target"),
            "base_rate": base_rate(hi, lo, cl, sign, up, dn, horizon, (i_fill - horizon, i_fill + horizon), rng, key=asset),
        }
        rows.append(row)

    out = pd.DataFrame(rows)
    out.to_csv(args.out, index=False, encoding="utf-8")
    ev = out[out["status"] == "evaluated"].copy()
    print(f"объектов {len(out)}; статусы: {out['status'].value_counts().to_dict()}")
    if not ev.empty:
        ev["win"] = ev["result"] == "target"
        ev["year"] = ev["posted_at"].str[:4]
        g = ev.groupby(["kind", "year"]).agg(n=("win", "size"), hit=("win", "mean"), base=("base_rate", "mean"),
                                             stops=("result", lambda s: (s == "stop").mean()),
                                             mae=("mae", "median"), ret=("ret_horizon", "median"))
        print("\nЦель раньше стопа: прогноз против базы (случайный вход, те же проценты)")
        print(g.to_string(float_format=lambda v: f"{v:.2f}"))
        print(f"\nВСЕГО: {len(ev)} прогнозов, цель первой {ev['win'].mean():.0%}, база {ev['base_rate'].mean():.0%}")
    do = out[out["status"] == "direction_only"]
    if not do.empty:
        print(f"\nТолько направление ({len(do)}): "
              f"ход в сторону прогноза {do['dir_ret'].mean():+.1%} "
              f"(медиана {do['dir_ret'].median():+.1%}), база {do['dir_base'].mean():+.1%}, "
              f"угадан знак {(do['dir_ret'] > 0).mean():.0%}")
    cl_ = out[out["status"] == "claim_checked"]
    if not cl_.empty:
        bad = cl_[cl_["claim_price_seen_72h"] == False]  # noqa: E712
        print(f"\nОтчёты с ценой: {len(cl_)}, цена НЕ встречалась за 72 ч до поста: {len(bad)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
