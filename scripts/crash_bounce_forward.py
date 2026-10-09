#!/usr/bin/env python
"""Замер вперёд по архиву (слой А) автомата CryptosMX — раз в сутки, тем же кодом, что 2021–2026.

Объявлено владельцем 09.10.2026 до старта: `docs/research/crash-bounce-paper-2026-10-09.md`
(окно с 10.10.2026, критерии К1–К5, правило замороженное — `limit5|side`, D0, 1% × 3).

    python scripts/crash_bounce_forward.py once --out D          # одни сутки, руками
    python scripts/crash_bounce_forward.py loop --out D          # сервис: раз в сутки, 12:30 UTC

Сутки D разбираются на D+2: лента сделок за D выкладывается ≈07:00 UTC следующего дня, минутки —
до ≈11:40, а сделке на стыке суток нужна ещё и лента D+1. Разбери D на D+1 — сделки последних
минут суток тихо пропали бы. Шаги:

1. `crash_bounce_check.py events --daily` — с `SEED_FROM` (30 суток до окна: счёт обвалов для
   выбора 100 монет бумажного бота) по D;
2. `simulate --min-drop 0.05 --side --vars limit5 --all-events --days DRY_FROM:D
   --strict-since D−7` — невыложенная лента не «нет данных», а повтор следующим запуском;
3. `crash_bounce_portfolio.py --forward START --period START:D --json` — К1, К2, недели;
4. `crash_counts.json` — минут обвала ≥5% по монетам за 30 суток по D (бот берёт оттуда 100);
5. снимок сделок CryptosMX на Binance (слой В): Binance отдаёт скользящее окно ≈4 месяцев,
   снимки склеиваются в `cryptosmx-positions.json`;
6. итог суток — в бот лаборатории (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`; только
   `sendMessage`, токен не печатается). Сообщение — ещё и внешняя метка времени.

Дни 01–09.10.2026 (`DRY_FROM`…) разыгрываются как проверка конвейера и в счёт окна не идут.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
START = date(2026, 10, 10)       # окно (объявлено 09.10.2026)
DRY_FROM = date(2026, 10, 1)     # проверка конвейера: после заморозки, до окна, не в счёт
SEED_FROM = date(2026, 9, 8)     # события за 30 суток до окна — для выбора 100 монет
LAG_DAYS = 2                     # сутки D разбираются на D+2
STRICT_DAYS = 7                  # моложе — невыложенная лента ставится на повтор
RUN_AFTER_UTC = (12, 30)
COUNT_WINDOW = 30
PORTFOLIO_ID = "5064581361618552064"  # CryptosMX, копитрейдинг Binance
ARCHIVE_KLINES = "https://data.binance.vision/data/futures/um/daily/klines"


def log(msg: str) -> None:
    print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _run(args: list[str]) -> int:
    """Шаг — отдельным процессом того же Python: так же, как его запускали руками в 2021–2026."""
    log("$ " + " ".join(args[1:]))
    return subprocess.call(args, env={**os.environ, "PYTHONUTF8": "1"})


def archive_ready(day: date) -> bool:
    """Выложены ли минутки суток: по BTCUSDT — его архив появляется вместе с остальными."""
    d = day.isoformat()
    url = f"{ARCHIVE_KLINES}/BTCUSDT/1m/BTCUSDT-1m-{d}.zip"
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception as e:  # noqa: BLE001 — сеть: попробуем позже
        log(f"архив недоступен: {type(e).__name__}")
        return False


def crash_counts(out: Path, last: date) -> dict:
    """Минут обвала ≥5% по монетам за `COUNT_WINDOW` суток по `last` включительно."""
    lo = datetime(last.year, last.month, last.day, tzinfo=UTC) - timedelta(days=COUNT_WINDOW - 1)
    lo_ms = int(lo.timestamp() * 1000)
    hi_ms = lo_ms + COUNT_WINDOW * 86_400_000
    cnt: Counter[str] = Counter()
    with (out / "events.jsonl").open() as fh:
        for line in fh:
            e = json.loads(line)
            if lo_ms <= e["t"] < hi_ms and e["drop"] <= -0.05:
                cnt[e["sym"]] += 1
    return {"asof": last.isoformat(), "window_days": COUNT_WINDOW,
            "from": lo.date().isoformat(), "counts": dict(cnt.most_common())}


def snapshot_trader(out: Path) -> str:
    """Слой В: снимок закрытых позиций CryptosMX, склейка с прежними. → строка для отчёта."""
    snap = out / "cryptosmx-snapshot.json"
    rc = _run([sys.executable, str(HERE / "binance_lead_history.py"), "fetch",
               "--portfolio", PORTFOLIO_ID, "--out", str(snap)])
    if rc != 0 or not snap.exists():
        return "CryptosMX: снимок не снят"
    new = json.loads(snap.read_text(encoding="utf-8"))["positions"]
    acc_p = out / "cryptosmx-positions.json"
    acc = json.loads(acc_p.read_text(encoding="utf-8")) if acc_p.exists() else []
    key = lambda p: (p["symbol"], p["opened"], str(p.get("positionId")))  # noqa: E731
    known = {key(p): i for i, p in enumerate(acc)}
    added = 0
    for p in new:
        if key(p) in known:
            acc[known[key(p)]] = p  # частично закрытая позиция дописывается
        else:
            acc.append(p)
            added += 1
    acc.sort(key=lambda p: p["opened"])
    acc_p.write_text(json.dumps(acc, ensure_ascii=False), encoding="utf-8")
    return f"CryptosMX: в выдаче {len(new)}, новых {added}, всего собрано {len(acc)}"


def telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_ADMIN_ID", "")
    if not token.strip() or not chat.strip():
        log("Telegram не настроен — сообщение только в лог")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data),
            timeout=30).read()
    except urllib.error.HTTPError as e:
        log(f"Telegram: отказ {e.code}")  # без URL: в нём токен
    except Exception as e:  # noqa: BLE001
        log(f"Telegram: {type(e).__name__}")


def report(s: dict | None, last: date, trader: str) -> str:
    head = f"Автомат CryptosMX, замер вперёд по архиву (слой А), сутки {last:%d.%m.%Y} UTC"
    if s is None:
        return f"{head}\nпроверка конвейера (до окна {START:%d.%m}) — в счёт не идёт\n{trader}"
    lines = [head, f"окно с {START:%d.%m}: {s['days']} сут. из 84, сделок {s['trades']} из 300"]
    # до 09.10 в JSON была одна клетка без `cells` — читается как главная
    for c in s.get("cells") or [{**s, "size": 0.01, "cap": 3, "max_drop": 0.05}]:
        ld = c["last_day"] or {"pnl": 0.0, "trades": 0}
        lines.append(
            f"{c['size'] * 100:g}% × {c['cap']}: сутки {ld['trades']} сд. "
            f"{ld['pnl'] * 100:+.2f}%; окно {c['total'] * 100:+.2f}% D0, t {c['t']:+.2f}; "
            f"ниже D0 {c['drop'] * 100:.2f}% (порог {c['max_drop'] * 100:g}%) — "
            f"{'ок' if c['k2'] else 'ПРЕВЫШЕН'}; худший день {c['worst_day'] * 100:+.2f}%")
    lines += ["вердикт: " + ("можно выносить" if s["ready"] else "рано"), trader]
    return "\n".join(lines)


def once(out: Path, today: date, send: bool = True) -> bool:
    """→ True, если сутки D = today − LAG_DAYS разобраны (или уже были); False — повторить позже."""
    out.mkdir(parents=True, exist_ok=True)
    last = today - timedelta(days=LAG_DAYS)
    state_p = out / "forward_state.json"
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    if state.get("reported") == last.isoformat():
        return True
    if not archive_ready(last):
        log(f"минуток за {last} в архиве ещё нет — позже")
        return False
    py, chk = sys.executable, str(HERE / "crash_bounce_check.py")
    if _run([py, chk, "events", "--daily", "--out", str(out), "--from", SEED_FROM.isoformat(),
             "--to", last.isoformat(), "--workers", "16"]) != 0:
        return False
    sim_rc = _run([py, chk, "simulate", "--out", str(out), "--min-drop", "0.05", "--side",
                   "--vars", "limit5", "--all-events", "--workers", "2",
                   "--days", f"{DRY_FROM}:{last}",
                   "--strict-since", (today - timedelta(days=STRICT_DAYS)).isoformat()])
    if sim_rc != 0:
        return False
    # не помеченные готовыми дни-символы (лента не выложена) — повтор следующим запуском
    need = {(e["sym"], datetime.fromtimestamp(e["t"] / 1000, UTC).date().isoformat())
            for e in map(json.loads, (out / "events.jsonl").read_text().splitlines())
            if e["drop"] <= -0.05}
    done = set()
    if (out / "sim_done.txt").exists():
        done = {tuple(k.rsplit(":", 1)) for k in (out / "sim_done.txt").read_text().split()}
    left = sorted(k for k in need - done if DRY_FROM.isoformat() <= k[1] <= last.isoformat())
    if left:
        log(f"не разыграно {len(left)} дней-символов (лента не выложена?): {left[:5]} — позже")
        return False
    (out / "crash_counts.json").write_text(json.dumps(crash_counts(out, last), indent=1))
    summary = None
    if last >= START:
        js = out / "forward_summary.json"
        with (out / "portfolio.txt").open("w", encoding="utf-8") as fh:
            rc = subprocess.call(
                [py, str(HERE / "crash_bounce_portfolio.py"), "--out", str(out),
                 "--forward", START.isoformat(), "--period", f"{START}:{last}", "--json", str(js)],
                stdout=fh, env={**os.environ, "PYTHONUTF8": "1"})
        if rc != 0:
            return False
        summary = json.loads(js.read_text(encoding="utf-8"))
    trader = snapshot_trader(out)
    text = report(summary, last, trader)
    log(text.replace("\n", " | "))
    if send:
        telegram(text)
    state_p.write_text(json.dumps({"reported": last.isoformat(),
                                   "at": datetime.now(UTC).isoformat(timespec="seconds")}))
    return True


def loop(out: Path) -> int:
    log(f"слой А: окно с {START}, сутки D разбираются на D+{LAG_DAYS} после "
        f"{RUN_AFTER_UTC[0]:02d}:{RUN_AFTER_UTC[1]:02d} UTC")
    while True:
        now = datetime.now(UTC)
        if (now.hour, now.minute) >= RUN_AFTER_UTC:
            try:
                ok = once(out, now.date())
            except Exception as e:  # noqa: BLE001 — сервис не падает, сутки повторятся
                log(f"ошибка: {type(e).__name__}: {e}")
                ok = False
            if not ok:
                log("повтор через час")
        time.sleep(3600 if (now.hour, now.minute) >= RUN_AFTER_UTC else 600)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("once", "loop"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--today", help="как будто сегодня ГГГГ-ММ-ДД (разбор D = сегодня − 2)")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()
    if a.cmd == "loop":
        return loop(a.out)
    today = date.fromisoformat(a.today) if a.today else datetime.now(UTC).date()
    return 0 if once(a.out, today, send=not a.no_telegram) else 1


if __name__ == "__main__":
    raise SystemExit(main())
