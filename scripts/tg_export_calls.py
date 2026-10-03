"""Выгрузка закрытого Telegram-канала → лента и журнал сигналов для calls_check.py.

У закрытого канала (вход по приглашению) нет веб-превью t.me/s/, и tme_archive.py его не
читает. Выгрузку делает участник канала: Telegram Desktop → канал → ⋮ → «Экспорт истории
чата» → «Машиночитаемый JSON». Дальше всё здесь.

Выход в --out:
  posts.json        — лента в духе tme_archive (id, date в UTC, text, reply_to, photos, video);
  claims.json       — ответы-отчёты на сигналы («цель достигнута», «стоп», «фиксируем»);
  calls/calls.json  — журнал для calls_check: сигналы «#ТИКЕР — вход — стоп — цели». Отдельной
                      папкой: calls_check читает ВСЕ *.json в каталоге и принял бы ленту за журнал.

Залповые дни. В канал переносят старую историю: сотни постов за часы, ответ «цель
достигнута» через минуты после «сигнала». Дата такого поста — не дата прогноза, поэтому
сигналы этих дней получают kind=call_backfilled, и calls_check их пропускает. Признак —
день, где постов не меньше --burst-min и медиана «сигнал → ответ» меньше часа.

Чего разборщик не делает: стоп без числа («под линией тренда») остаётся пустым; пары к BTC
(цены в BTC) и посты до --crypto-from (акции) получают kind=call_btc_quoted / call_stock —
свечей Binance к ним нет; цели в процентах — call_pct_targets; сигнал без тикера или без
целей — call_unparsed (смотреть глазами). «Интрадей» получает горизонт 3 суток.
«Стоп закреплением» (по закрытию) помечается stop_close=true, а calls_check считает стоп
касанием — строже автора, но так же строго и для базы, поэтому разница «прогноз − база» честная.

    python scripts/tg_export_calls.py <result.json> --channel Cryptos_Mx --out <dir>
        [--crypto-from 2021-07-01] [--burst-min 40]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

NUM = re.compile(r"\d+(?:\.\d+)?")
TAG = re.compile(r"#(?=[A-Za-z0-9]*[A-Za-z])([A-Za-z0-9]{2,15})")  # #1INCHUSDT тоже тикер
NAMED = {"BTC": r"битк|биткоин|bitcoin|\bbtc\b", "ETH": r"эфир|ethereum|\beth\b"}
QUOTES = ("USDT", "BUSD", "USDC", "PERP", "USD")
LONG_RX = re.compile(r"покуп|лонг|\blong\b|\bbuy\b|набор|подбор", re.I)
SHORT_RX = re.compile(r"шорт|\bshort\b|продаж|\bsell\b", re.I)
STOP_RX = re.compile(r"стоп|\bstop|закреплени\w*\s+(?:ниже|под|выше)", re.I)
INTRADAY_DAYS = 3  # «Интрадей»: цель, не пришедшая за пару дней, — не та сделка, что обещана
TARGET_RX = re.compile(r"цел[ьиея]|тейк|\btake|\btarget|\btp\d?\b", re.I)
MARKET_RX = re.compile(r"текущ|по рынку|\bmarket\b", re.I)
# нумерация целей: «Цель 1 -», «1-я цель», «| 2 - 4650», «TP1»
ENUM_RX = [
    re.compile(r"цел[ьи]\s*\d{1,2}\s*[.)]?\s*[-–:]", re.I),
    re.compile(r"\b\d{1,2}\s*[-–]?\s*(?:я\s*)?цел[ьи]", re.I),
    re.compile(r"(?:^|\|)\s*\d{1,2}\s*[-–)]\s+(?=\d)"),
    re.compile(r"\btp\s*\d\b", re.I),
]


def text_of(m: dict) -> str:
    t = m.get("text", "")
    if isinstance(t, str):
        return t
    return "".join(x if isinstance(x, str) else x.get("text", "") for x in t)


def utc_date(m: dict) -> str:
    if m.get("date_unixtime"):
        return datetime.fromtimestamp(int(m["date_unixtime"]), UTC).isoformat()
    return m["date"]  # без пояса — как выгрузил Telegram


PCT_RX = re.compile(r"[+-]?\d+(?:\.\d+)?\s*(?:[-–]\s*\d+(?:\.\d+)?\s*)?%")  # «+3-4%», «5%-15%»


def clean(t: str) -> str:
    t = t.replace("\n", " | ")
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)  # 0,95 → 0.95
    t = re.sub(r"(\d)\.\.(\d)", r"\1.\2", t)  # опечатка «18..70» → 18.70
    return PCT_RX.sub(" ", t)  # доли депозита и проценты — не цены


def numbers(seg: str) -> list[float]:
    return [float(x) for x in NUM.findall(seg) if float(x) > 0]


def asset_of(tag: str) -> tuple[str, str]:
    a = tag.upper()
    for q in QUOTES:
        if a.endswith(q) and len(a) > len(q):
            return a[: -len(q)], "USDT"
    if a.endswith("BTC") and a not in ("BTC", "WBTC"):
        return a[:-3], "BTC"
    return a, "USDT"


def parse_signal(t: str) -> dict:
    """Сигнал из текста поста: тикер, сторона, вход, стоп, цели (что нашлось)."""
    tag = TAG.search(t)
    body = TAG.sub(" ", clean(t))
    i_stop = (STOP_RX.search(body) or re.search(r"$", body)).start()
    i_tgt = (TARGET_RX.search(body) or re.search(r"$", body)).start()
    head = body[: min(i_stop, i_tgt)]
    if i_stop < i_tgt:
        stop_seg, tgt_seg = body[i_stop:i_tgt], body[i_tgt:]
    else:
        stop_seg, tgt_seg = body[i_stop:], body[i_tgt:i_stop]
    for rx in ENUM_RX:
        tgt_seg = rx.sub(" ", tgt_seg)
    lo, sh = LONG_RX.search(body), SHORT_RX.search(body)
    if lo and sh:
        direction = "long" if lo.start() < sh.start() else "short"
    else:
        direction = "long" if lo else "short" if sh else None
    head_nums = numbers(head)
    entry, zone = None, None
    if MARKET_RX.search(head):
        entry = "market"
    elif len(head_nums) >= 2:
        zone = sorted(head_nums[:2])
    elif head_nums:
        entry = head_nums[0]
    stop_nums = numbers(stop_seg)
    if tag:
        asset, quote = asset_of(tag.group(1))
    else:  # «зона на подбор в лонг по битку» — без решётки
        asset = next((a for a, rx in NAMED.items() if re.search(rx, t, re.I)), None)
        quote = "USDT" if asset else None
    targets, from_pct = numbers(tgt_seg), False
    m_tgt = TARGET_RX.search(t)
    pcts = sorted({float(x.replace(",", ".")) for x in re.findall(
        r"(\d+(?:[.,]\d+)?)\s*(?=[-–]\s*\d+(?:[.,]\d+)?\s*%|%)", t[m_tgt.start():] if m_tgt else ""
    )})
    if not targets and pcts and zone and direction:
        # «Тейки +3-4%» от зоны: вход там же, где его возьмёт calls_check (край зоны)
        ref, sign = (zone[1], 1) if direction == "long" else (zone[0], -1)
        targets, from_pct = [round(ref * (1 + sign * p / 100), 10) for p in pcts], True
    return {
        "asset": asset, "quote": quote, "direction": direction, "entry": entry,
        "entry_zone": zone, "targets": targets, "stop": stop_nums[0] if stop_nums else None,
        "stop_close": bool(re.search(r"закреп", stop_seg, re.I)),
        "targets_from_pct": from_pct, "pct_targets": not targets and bool(pcts),
        "horizon_days": INTRADAY_DAYS if re.search(r"интрадей|intraday", t, re.I) else None,
    }


LOSS_RX = re.compile(r"выбил|сработал\w*\s+стоп|стоп\w*\s+(?:сработ|задет|выбил)|по\s+стопу"
                     r"|(?<!без)убыт|в\s+минус|(?<![\d.%])-\s*\d+(?:[.,]\d+)?\s*%")
# «(?<!без)» — не «безубыток»; «(?<!%)» — «+24%-33%» это диапазон прибыли, а не минус
WIN_RX = re.compile(r"цел\w*\s+(?:\w+\s+)?(?:достигнут|взят|отработ)|(?:достигнут|взят)\w*\s+"
                    r"(?:\w+\s+)?цел|тейк\w*\s+(?:взят|забран|сработ)|отработал|профит|✅"
                    r"|\+\s*\d+(?:[.,]\d+)?\s*%")


def claim_of(t: str) -> tuple[str, int | None]:
    """Вид отчёта. Убыток проверяется первым: «стоп в безубыток» — не убыток, а «+10%,
    часть фиксируем» — прибыль, даже если рядом слово «стоп»."""
    tl = t.lower()
    if LOSS_RX.search(tl):
        kind = "loss"
    elif re.search(r"все\s+(?:цели|тейки)|полностью\s+отработ", tl):
        kind = "all_targets"
    elif WIN_RX.search(tl):
        kind = "win"
    elif re.search(r"безубыт|\bб/у\b", tl):
        kind = "breakeven"
    elif re.search(r"фикс|закры", tl):
        kind = "fix"
    else:
        kind = "other"
    order = None
    for n, rx in ((1, r"перв|\b1\s*-?\s*я\b|\b1\s+цел"), (2, r"втор|\b2\s*-?\s*я\b|\b2\s+цел"),
                  (3, r"трет|\b3\s*-?\s*я\b|\b3\s+цел"), (4, r"четв|\b4\s*-?\s*я\b|\b4\s+цел")):
        if re.search(rx, tl):
            order = n
            break
    return kind, order


def burst_days(posts: list[dict], min_posts: int) -> dict[str, float | None]:
    """{день: медиана часов «сигнал → ответ» в этот день} для залповых дней."""
    by_id = {p["id"]: p for p in posts}
    per_day: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        per_day[p["date"][:10]].append(p)
    out = {}
    for day, ps in per_day.items():
        if len(ps) < min_posts:
            continue
        gaps = []
        for p in ps:
            parent = by_id.get(p["reply_to"])
            if parent and parent["date"][:10] == day:
                a = datetime.fromisoformat(parent["date"])
                gaps.append((datetime.fromisoformat(p["date"]) - a).total_seconds() / 3600)
        med = statistics.median(gaps) if gaps else None
        if med is None or med < 1:
            out[day] = med
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("export", help="result.json из Telegram Desktop")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--crypto-from", default="2000-01-01", help="раньше этой даты — акции")
    ap.add_argument("--burst-min", type=int, default=40)
    args = ap.parse_args()

    raw = json.loads(Path(args.export).read_text(encoding="utf-8"))
    posts = []
    for m in raw["messages"]:
        if m.get("type") != "message":
            continue
        posts.append({
            "id": m["id"], "date": utc_date(m), "text": text_of(m),
            "reply_to": m.get("reply_to_message_id"), "edited": bool(m.get("edited")),
            "photos": [m["photo"]] if m.get("photo") else [],
            "video": m.get("media_type") == "video_file",
        })
    bursts = burst_days(posts, args.burst_min)

    calls, claims = [], []
    by_id = {p["id"]: p for p in posts}
    for p in posts:
        t = p["text"]
        if p["reply_to"] in by_id:
            kind, order = claim_of(t)
            pct = re.search(r"([+-]?\d+(?:[.,]\d+)?)\s*%", t)
            claims.append({
                "channel": args.channel, "post_id": p["id"], "reply_to": p["reply_to"],
                "posted_at": p["date"], "claim": kind, "order": order,
                "pct": float(pct.group(1).replace(",", ".")) if pct else None,
                "backfilled": p["date"][:10] in bursts, "quote": t[:200],
            })
            continue
        if not (TARGET_RX.search(t) and (STOP_RX.search(t) or LONG_RX.search(t)
                                          or SHORT_RX.search(t)) and NUM.search(t)):
            continue
        s = parse_signal(t)
        if p["date"][:10] < args.crypto_from:
            kind = "call_stock"
        elif p["date"][:10] in bursts:
            kind = "call_backfilled"
        elif s["quote"] == "BTC":
            kind = "call_btc_quoted"
        elif s["pct_targets"]:
            kind = "call_pct_targets"  # цели в процентах от входа — в цены не переводятся
        elif not s["asset"] or not s["targets"]:
            kind = "call_unparsed"
        else:
            kind = "call"
        calls.append({
            "channel": args.channel, "post_id": p["id"], "posted_at": p["date"],
            "edited": p["edited"], "kind": kind, **s,
            "has_photo": bool(p["photos"]), "quote": t.replace("\n", " | ")[:240],
        })

    out = Path(args.out)
    (out / "calls").mkdir(parents=True, exist_ok=True)
    for name, data in (("posts", posts), ("calls/calls", calls), ("claims", claims)):
        (out / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print(f"{args.channel}: постов {len(posts)}, "
          f"{posts[0]['date'][:10]} .. {posts[-1]['date'][:10]}")
    for day, med in sorted(bursts.items()):
        n = sum(1 for p in posts if p["date"][:10] == day)
        print(f"  залповый день {day}: {n} постов, медиана «сигнал → ответ» {med} ч")
    print("сигналы:", dict(Counter(c["kind"] for c in calls)))
    print("ответы:", dict(Counter(c["claim"] for c in claims)),
          "| из них в залповые дни:", sum(c["backfilled"] for c in claims))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
