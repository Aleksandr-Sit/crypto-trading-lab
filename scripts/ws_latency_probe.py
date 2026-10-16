#!/usr/bin/env python3
"""Второй наблюдатель потока Binance Futures: только задержки, без торговли (16.10.2026).

Зачем. 14–15.10.2026 бумажный бот `crash_bounce_paper.py` (Hostkey) часами получал сделки
с опозданием 13–49 с, и все три его соединения рвались поровну (`ws CLOSED`). Разбор
(`docs/research/crash-bounce-paper-2026-10-09.md`, «Причина слепоты») исключил сам бот и
сервер, но с одной точки наблюдения не различить: опаздывает поток Binance целиком или
маршрут Hostkey → Binance. Этот процесс слушает ТОТ ЖЕ поток с другого сервера (VPS Senko,
другой провайдер) и пишет по строке в минуту. Совпали отставания — Binance; только у бота —
маршрут или сам бот.

Подписка повторяет бота: все торгуемые USDT-перпы, `<монета>@aggTrade`, адрес
`/market/ws`, 3 соединения (монеты `syms[i::3]`), ротация раз в 23 ч. Задержка — как у бота:
время получения с поправкой часов к бирже минус `E` события.

Свою занятость наблюдатель меряет сам: `loop_ms` — худшее опоздание собственного цикла за
минуту, `cpu_ms` — процессор процесса. На слабом сервере в пик рынка он может не успевать
сам, и такая минута — не довод против биржи.

    python3 scripts/ws_latency_probe.py run --out /opt/ws-probe/data
    python3 scripts/ws_latency_probe.py compare --probe DIR --bot-journal DIR [--from Д] [--to Д]

Строка минуты (`<день UTC>.jsonl`): `t` (начало минуты, мс), `n` сделок, `p50/p99/max`
задержки (мс), `open` живых соединений на конец минуты, `closes` обрывов, `jumps`/`missing`
скачков номеров сделок и пропущенных номеров, `loop_ms`, `cpu_ms`, `off` поправка часов.
Обрывы — ещё и отдельными строками `{"k": "conn", ...}`. Ключей не нужно, ничего не шлёт.
Зависимости: python 3.12 и aiohttp (на Ubuntu — пакет `python3-aiohttp`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from array import array
from datetime import UTC, datetime
from pathlib import Path

FAPI = "fapi.binance.com"
WS_URL = "wss://fstream.binance.com/market/ws"
CONNS = 3
SUB_CHUNK = 200           # подписок в одном сообщении: не больше 10 входящих сообщений/с
ROTATE_S = 23 * 3600      # Binance рвёт соединение раз в 24 ч — переподключаемся сами
SILENCE_S = 60
KEEP_DAYS = 60            # дневные файлы старше удаляются (диск Senko маленький)

_OFF = [0]  # поправка часов к бирже, мс


def now_ms() -> int:
    return int(time.time() * 1000) + _OFF[0]


def day_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def perps() -> list[str]:
    with urllib.request.urlopen(f"https://{FAPI}/fapi/v1/exchangeInfo", timeout=30) as r:
        d = json.load(r)
    return sorted(x["symbol"] for x in d["symbols"] if x["contractType"] == "PERPETUAL"
                  and x["quoteAsset"] == "USDT" and x["status"] == "TRADING")


def sync_clock(samples: int = 5) -> tuple[int, int]:
    """→ (поправка, мс; полупуть, мс) — как у бота: прогретое соединение, лучший из замеров."""
    import http.client
    conn = http.client.HTTPSConnection(FAPI, timeout=10)
    try:
        best = None
        for i in range(samples + 1):
            t0 = time.time() * 1000
            conn.request("GET", "/fapi/v1/time", headers={"User-Agent": "lab-probe"})
            server = json.loads(conn.getresponse().read())["serverTime"]
            t1 = time.time() * 1000
            if i and (best is None or t1 - t0 < best[0]):
                best = (t1 - t0, server - (t0 + t1) / 2)
    finally:
        conn.close()
    _OFF[0] = round(best[1])
    return _OFF[0], round(best[0] / 2)


class Out:
    """Дневные файлы jsonl; каждая строка сразу на диск (строк ~1500 в сутки)."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.day, self.fh = None, None

    def write(self, rec: dict) -> None:
        rec.setdefault("recv", now_ms())
        d = day_of(rec["recv"])
        if d != self.day:
            if self.fh:
                self.fh.close()
            self.day, self.fh = d, (self.root / f"{d}.jsonl").open("a", encoding="utf-8")
            self.prune()
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()

    def prune(self) -> None:
        files = sorted(self.root.glob("????-??-??.jsonl"))
        for f in files[:-KEEP_DAYS]:
            f.unlink(missing_ok=True)


class Probe:
    def __init__(self, out: Path):
        self.out = Out(out)
        self.lat = array("i")
        self.minute = 0
        self.closes = self.jumps = self.missing = 0
        self.loop_ms = 0.0
        self.open: set[int] = set()
        self.last_a: dict[str, int] = {}
        self.cpu0 = time.process_time()
        self.last_msg = time.time()

    async def conn(self, idx: int, syms: list[str], session) -> None:
        import aiohttp
        while True:
            started = time.time()
            try:
                async with session.ws_connect(WS_URL, heartbeat=30, max_msg_size=0) as ws:
                    for i in range(0, len(syms), SUB_CHUNK):
                        await ws.send_json({"method": "SUBSCRIBE", "id": idx * 1000 + i,
                                            "params": [f"{s.lower()}@aggTrade"
                                                       for s in syms[i:i + SUB_CHUNK]]})
                        await asyncio.sleep(0.3)
                    self.open.add(idx)
                    self.out.write({"k": "conn", "event": "open", "conn": idx, "syms": len(syms)})
                    while True:
                        if time.time() - started > ROTATE_S:
                            asyncio.get_running_loop().create_task(self.conn(idx, syms, session))
                            await asyncio.sleep(10)  # внахлёст, как у бота
                            self.out.write({"k": "conn", "event": "rotate", "conn": idx})
                            return
                        m = await ws.receive(timeout=SILENCE_S)
                        if m.type != aiohttp.WSMsgType.TEXT:
                            raise ConnectionError(f"ws {m.type.name}")
                        recv = now_ms()
                        d = json.loads(m.data)
                        if d.get("e") != "aggTrade":
                            continue
                        self.last_msg = time.time()
                        self.lat.append(max(-2**31, min(2**31 - 1, recv - d["E"])))
                        s, a = d["s"], d["a"]
                        p = self.last_a.get(s)
                        if p is not None and a > p + 1:
                            self.jumps += 1
                            self.missing += a - p - 1
                        if p is None or a > p:
                            self.last_a[s] = a
            except Exception as e:  # noqa: BLE001 — связь: переподключиться
                self.open.discard(idx)
                self.closes += 1
                self.out.write({"k": "conn", "event": "close", "conn": idx,
                                "err": f"{type(e).__name__}: {e}"[:200]})
                await asyncio.sleep(3)

    async def clock(self) -> None:
        """Раз в 0.5 с: опоздание своего цикла; на смене минуты — строка минуты."""
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(0.5)
            self.loop_ms = max(self.loop_ms, (time.monotonic() - t0 - 0.5) * 1000)
            m = now_ms() // 60_000 * 60_000
            if m != self.minute:
                if self.minute:
                    self.flush_minute()
                self.minute = m

    def flush_minute(self) -> None:
        s = sorted(self.lat)
        cpu = time.process_time()
        rec = {"k": "min", "t": self.minute, "n": len(s),
               "p50": s[len(s) // 2] if s else None,
               "p99": s[int(len(s) * 0.99)] if s else None,
               "max": s[-1] if s else None,
               "open": len(self.open), "closes": self.closes,
               "jumps": self.jumps, "missing": self.missing,
               "loop_ms": round(self.loop_ms), "cpu_ms": round((cpu - self.cpu0) * 1000),
               "off": _OFF[0]}
        self.out.write(rec)
        self.lat = array("i")
        self.closes = self.jumps = self.missing = 0
        self.loop_ms = 0.0
        self.cpu0 = cpu

    async def hourly(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                off, half = await asyncio.to_thread(sync_clock)
                self.out.write({"k": "clock", "off": off, "half_rtt": half})
            except Exception as e:  # noqa: BLE001
                self.out.write({"k": "clock", "err": f"{type(e).__name__}: {e}"[:200]})

    async def main(self) -> None:
        import aiohttp
        syms = await asyncio.to_thread(perps)
        off, half = await asyncio.to_thread(sync_clock)
        self.out.write({"k": "start", "syms": len(syms), "conns": CONNS, "url": WS_URL,
                        "clock_offset_ms": off, "clock_half_rtt_ms": half})
        print(f"старт: монет {len(syms)}, поправка часов {off} мс (±{half})", flush=True)
        parts = [syms[i::CONNS] for i in range(CONNS)]
        async with aiohttp.ClientSession() as session:
            tasks = [asyncio.create_task(self.conn(i, p, session)) for i, p in enumerate(parts)]
            tasks += [asyncio.create_task(self.clock()), asyncio.create_task(self.hourly())]
            await asyncio.gather(*tasks)


# --- сравнение с журналом бота -------------------------------------------------------------

def _read(root: Path, kind: str, d1: str | None, d2: str | None) -> dict[int, dict]:
    out = {}
    for f in sorted(root.glob("????-??-??.jsonl")):
        if (d1 and f.stem < d1) or (d2 and f.stem > d2):
            continue
        for line in f.open(encoding="utf-8"):
            r = json.loads(line)
            if r.get("k") == kind:
                out[r["t"]] = r
    return out


def compare(probe: Path, bot: Path, d1: str | None, d2: str | None, slow_ms: int) -> int:
    """Минуты, где отстаёт хоть один из двух (p50 > slow_ms), — рядом; и итог по сутки."""
    pr, bt = _read(probe, "min", d1, d2), _read(bot, "lat", d1, d2)
    common = sorted(set(pr) & set(bt))
    if not common:
        print("общих минут нет — проверь каталоги и даты")
        return 1
    cnt = {"оба": 0, "только бот": 0, "только наблюдатель": 0}
    rows = []
    for t in common:
        a, b = pr[t], bt[t]
        sa, sb = (a["p50"] or 0) > slow_ms, (b["p50"] or 0) > slow_ms
        if sa or sb:
            key = "оба" if sa and sb else ("только бот" if sb else "только наблюдатель")
            busy = a["loop_ms"] > 1000
            cnt[key] += 1
            rows.append((t, b, a, key, busy))
    print(f"общих минут {len(common)} ({day_of(common[0])} … {day_of(common[-1])}); "
          f"порог отставания p50 > {slow_ms} мс")
    print(" | ".join(f"{k}: {v}" for k, v in cnt.items()))
    print("\n| минута UTC | бот n / p50 / p99 | наблюдатель n / p50 / p99 | обрывы набл. | "
          "цикл набл., мс | кто |")
    print("|---|---|---|---|---|---|")
    for t, b, a, key, busy in rows[:200]:
        ts = datetime.fromtimestamp(t / 1000, UTC).strftime("%d.%m %H:%M")
        print(f"| {ts} | {b['n']} / {b['p50']} / {b['p99']} | {a['n']} / {a['p50']} / {a['p99']} "
              f"| {a['closes']} | {a['loop_ms']}{' (занят сам)' if busy else ''} | {key} |")
    if len(rows) > 200:
        print(f"… и ещё {len(rows) - 200} минут")
    print("\nоба — отстаёт поток Binance; только бот — маршрут Hostkey → Binance или сам бот;"
          "\n«занят сам» — наблюдатель не успевал за потоком, его минута не довод.")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="слушать поток и писать строки минут")
    r.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare", help="минуты наблюдателя против журнала бота")
    c.add_argument("--probe", type=Path, required=True)
    c.add_argument("--bot-journal", type=Path, required=True)
    c.add_argument("--from", dest="d1")
    c.add_argument("--to", dest="d2")
    c.add_argument("--slow-ms", type=int, default=1000)
    a = ap.parse_args()
    if a.cmd == "run":
        asyncio.run(Probe(a.out).main())
        return 0
    return compare(a.probe, a.bot_journal, a.d1, a.d2, a.slow_ms)


if __name__ == "__main__":
    raise SystemExit(main())
