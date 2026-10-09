#!/usr/bin/env python
"""Бумажный бот автомата CryptosMX (слой Б): живой поток сделок Binance Futures, без ордеров.

Объявлено владельцем 09.10.2026 до старта: `docs/research/crash-bounce-paper-2026-10-09.md`.
Правило заморожено (`limit5|side`): лимитная покупка на −5% от закрытия прошлой минуты,
исполнение — сделкой строго ниже уровня; цель +1.5% (сделка строго выше); иначе через 7 мин
продажа первой сделкой по bid; мейкер 0.02%, тейкер 0.05%.

    python scripts/crash_bounce_paper.py run --out D [--counts F]   # сервис
    python scripts/crash_bounce_paper.py report --out D --archive A --from Д1 --to Д2

Ключей нет: только публичный поток `aggTrade` (`wss://fstream.binance.com/market/ws` —
старый адрес `/ws` принимает подписку и молча НЕ присылает сделок, проверено 09.10.2026).

Два счёта на одном потоке:

* **зеркало (`m`)** — ровно как архив: заявка на каждой монете, уровень действует с начала
  минуты, каждое событие независимо (`simulate --all-events`). Счёт по нему считает
  `crash_bounce_portfolio.run_book` тем же кодом, что в замере; сделки сверяются с розыгрышем
  архива тех же суток (К3);
* **×1 (`x1`)** — что исполнимо на субсчёте ×1 при 1% D0 на заявку: заявки только на 100
  монетах (по частоте обвалов ≥5% за 30 суток — `crash_counts.json` слоя А, при равенстве —
  по обороту за сутки) минус монеты в позиции и не больше `100 − открытые позиции`;
  закрытие минуты бот узнаёт в `WAIT_MS` после её конца, новый уровень встаёт через
  `SWITCH_MS` после этого — до того стоит заявка прошлой минуты и исполняется по СВОЕМУ
  уровню; заполнен третий слот — заявки снимаются за `CANCEL_MS` (исполненное в это окно —
  сверх предела, как в замере); слот освободился — заявки встают через `SWITCH_MS`; не
  больше одного исполнения на монету за минуту. Счёт ведётся на ходу (`LiveBook`): D0,
  1% × min(баланс, D0), в 00:00 UTC всё выше D0 выводится.

Журнал (`D/journal/ГГГГ-ММ-ДД.jsonl`, сутки по времени получения): исполнение пишется с
`fsync` В МОМЕНТ получения сделки — до исхода; исход — отдельной строкой потом. Плюс обрывы
связи, скачки номеров сделок (номера `a` у монеты идут подряд — пропуск виден точно; если у
монеты открыта позиция, пропущенное докачивается REST `aggTrades`), задержки по минутам,
выбор 100 монет.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAPI = "https://fapi.binance.com"
WS_URL = "wss://fstream.binance.com/market/ws"

# Правило (заморожено 04–08.10.2026) — те же числа, что в crash_bounce_check.py
DROP, TP, HOLD_MS = 0.05, 0.015, 7 * 60_000
MAKER, TAKER = 0.0002, 0.0005
# Счёт (заморожен 08.10.2026)
SIZE, SLOTS, CANCEL_MS = 0.01, 3, 1000
# Счёт ×1 (объявлен 09.10.2026)
ORDERS = 100        # заявок на субсчёте ×1 при 1% D0 на заявку, включая открытые позиции
WAIT_MS = 250       # ждать после конца минуты, пока долетят её сделки (задержка p99 ≈ 164 мс)
SWITCH_MS = 300     # перестановка заявки
# Связь
CONNS = 3                 # соединений; монеты делятся между ними
ROTATE_S = 23 * 3600      # Binance рвёт соединение раз в 24 ч — переподключаемся сами, внахлёст
SILENCE_S = 60            # нет ни одной сделки дольше — тревога
SUB_CHUNK = 200


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Часы бота — БИРЖЕВЫЕ: местное время плюс поправка, замеренная по /fapi/v1/time. Бот сравнивает
# своё время со временем сделок (перестановка заявок ×1, задержки), и часы машины, отстающие на
# 260 мс (рабочая машина 09.10.2026), сдвигали бы всё на эту величину.
_CLOCK_OFF = [0]


def now_ms() -> int:
    return int(time.time() * 1000) + _CLOCK_OFF[0]


def sync_clock(samples: int = 5) -> tuple[int, int]:
    """→ (поправка, мс; полупуть запроса, мс). Одно соединение, прогретое первым запросом:
    иначе в каждый замер входит рукопожатие TLS и середина запроса уезжает (было ±335 мс).
    Из замеров берётся самый быстрый — у него середина ближе всего к моменту ответа."""
    import http.client
    conn = http.client.HTTPSConnection(urllib.parse.urlparse(FAPI).netloc, timeout=10)
    try:
        best = None
        for i in range(samples + 1):
            t0 = time.time() * 1000
            conn.request("GET", "/fapi/v1/time", headers={"User-Agent": "lab"})
            server = json.loads(conn.getresponse().read())["serverTime"]
            t1 = time.time() * 1000
            if i and (best is None or t1 - t0 < best[0]):
                best = (t1 - t0, server - (t0 + t1) / 2)
    finally:
        conn.close()
    _CLOCK_OFF[0] = round(best[1])
    return _CLOCK_OFF[0], round(best[0] / 2)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def level_of(prev_close: float) -> float:
    return prev_close * (1 - DROP)  # та же формула, что в `_play`: иначе разойдутся края


@dataclass
class Trade:
    sym: str
    a: int          # номер агрегированной сделки — у монеты идут подряд
    px: float
    T: int          # время сделки на бирже, мс
    sell: bool      # агрессор — продавец (сделка по bid)
    recv: int = 0   # получено ботом, мс


@dataclass
class Pos:
    """Сделка счёта от входа до выхода — та же механика, что `crash_bounce_check._play`."""
    acct: str
    sym: str
    minute: int
    entry: float
    t_in: int
    recv_in: int
    low: float
    amt: float = 0.0
    over: bool = False

    @property
    def tp(self) -> float:
        return self.entry * (1 + TP)

    @property
    def end(self) -> int:
        return self.t_in + HOLD_MS

    def on_trade(self, tr: Trade) -> dict | None:
        """Сделка ленты ПОСЛЕ входа → итог или None. Цель — сделка строго выше; после 7 мин —
        первая сделка продавца (по bid). Минимум — как в `_play`: до таймера все сделки,
        после — только цена выхода."""
        if tr.T < self.end:
            if tr.px > self.tp:
                return self._exit(tr, self.tp, "tp", TP - MAKER - MAKER)
            self.low = min(self.low, tr.px)
            return None
        if tr.sell:
            self.low = min(self.low, tr.px)
            return self._exit(tr, tr.px, "time", tr.px / self.entry - 1 - MAKER - TAKER)
        return None

    def _exit(self, tr: Trade, px: float, how: str, net: float) -> dict:
        return {"acct": self.acct, "sym": self.sym, "t": self.minute, "entry": self.entry,
                "exit": px, "t_in": self.t_in, "t_out": tr.T, "how": how, "net": net,
                "low": self.low, "recv_in": self.recv_in, "recv_out": tr.recv,
                "amt": self.amt, "over": self.over}


@dataclass
class LiveBook:
    """Счёт ×1 на ходу — правила `crash_bounce_portfolio.run_book(fixed=True)`, клетка 1% × 3:
    D0 = 1.0, размер 1% × депозит на 00:00 после вывода, в 00:00 UTC всё выше D0 выводится."""
    equity: float = 1.0
    withdrawn: float = 0.0
    day: str | None = None
    day_start: float = 1.0
    min_bal: float = 1.0
    open: dict[str, Pos] = field(default_factory=dict)
    full_at: int | None = None
    day_pnl: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    day_taken: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    day_over: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def roll(self, t: int) -> None:
        d = _day(t)
        if d != self.day:
            if self.day is not None and self.equity > 1.0:
                self.withdrawn += self.equity - 1.0
                self.equity = 1.0
            self.day, self.day_start = d, self.equity

    def can_fill(self, t: int) -> tuple[bool, bool]:
        """→ (стоит ли заявка, будет ли это сверх предела)."""
        if len(self.open) < SLOTS:
            return True, False
        return self.full_at is not None and t <= self.full_at + CANCEL_MS, True

    def take(self, pos: Pos, over: bool) -> None:
        self.roll(pos.t_in)
        pos.amt, pos.over = SIZE * self.day_start, over
        self.open[pos.sym] = pos
        self.day_taken[self.day] += 1
        self.day_over[self.day] += over
        if len(self.open) == SLOTS:
            self.full_at = pos.t_in

    def close(self, res: dict) -> None:
        self.roll(res["t_out"])
        self.open.pop(res["sym"], None)
        pnl = res["amt"] * res["net"]
        self.equity += pnl
        self.day_pnl[self.day] += pnl
        self.min_bal = min(self.min_bal, self.equity)
        res["pnl"], res["bal"] = pnl, self.equity


@dataclass
class SymState:
    last_a: int | None = None
    last_px: float | None = None
    last_t: int = 0
    minute: int | None = None
    close: float | None = None        # закрытие прошлой минуты (для зеркала)
    valid_from: int = 0               # минута, с которой закрытию можно верить (после дыры)
    fired_m: int | None = None        # зеркало: минута, где событие уже было
    # ×1: снимок закрытия, сделанный в WAIT_MS после конца минуты, и уровни
    snap: dict[int, float] = field(default_factory=dict)   # минута → закрытие прошлой
    x1_filled_m: int | None = None
    ready_at: int = 0                 # ×1: заявка на монете встаёт не раньше (выход, вход в 100)


class Engine:
    """Логика бота без сети: сделки → исполнения зеркала и ×1, выходы, журнал (callback)."""

    def __init__(self, emit, x1_rank: list[str] | None = None):
        self.emit = emit
        self.st: dict[str, SymState] = defaultdict(SymState)
        self.mirror: dict[str, list[Pos]] = defaultdict(list)
        self.x1: dict[str, Pos] = {}
        self.book = LiveBook()
        self.rank: list[str] = list(x1_rank or [])
        self.eligible: set[str] = set()
        self.reopen_at = 0            # ×1: заявки снова встают после освобождения слота
        self.backfill_need: set[str] = set()
        self._recalc_eligible(0)

    # --- выбор монет ×1 -------------------------------------------------------------------
    def set_rank(self, rank: list[str], now: int) -> None:
        self.rank = list(rank)
        self._recalc_eligible(now)

    def _recalc_eligible(self, now: int) -> None:
        room = ORDERS - len(self.book.open)
        new = set([s for s in self.rank if s not in self.book.open][:max(room, 0)])
        for s in new - self.eligible:
            self.st[s].ready_at = max(self.st[s].ready_at, now + SWITCH_MS)
        self.eligible = new

    # --- минутный такт ×1 -----------------------------------------------------------------
    def tick(self, now: int) -> None:
        """Зовётся часто; в WAIT_MS после начала минуты m снимает закрытие m−1 по тому, что
        бот успел получить, — так поступил бы настоящий бот, переставляя заявки."""
        m = (now - WAIT_MS) // 60_000 * 60_000
        for s in self.eligible:
            st = self.st[s]
            if m in st.snap or st.last_px is None or (st.minute or 0) > m:
                continue
            # первая сделка минуты m уже пришла — закрытие m−1 лежит в `close`; нет — это
            # последняя полученная цена (минута без сделок закрывается прошлой ценой, как свеча)
            c = st.close if st.minute == m else st.last_px
            ok = c is not None and st.valid_from <= m
            st.snap[m] = level_of(c) if ok else float("nan")
            for k in [k for k in st.snap if k < m - 120_000]:
                del st.snap[k]

    def x1_level(self, st: SymState, t: int) -> float | None:
        m = t // 60_000 * 60_000
        switch = m + WAIT_MS + SWITCH_MS
        lv = st.snap.get(m) if t >= switch else st.snap.get(m - 60_000)
        if lv is None or lv != lv:  # нет снимка или он NaN (бот не знал закрытия)
            return None
        return lv

    # --- сделка ---------------------------------------------------------------------------
    def on_trade(self, tr: Trade) -> None:
        st = self.st[tr.sym]
        if st.last_a is not None and tr.a <= st.last_a:
            return  # дубль: соединения внахлёст при переподключении
        if st.last_a is not None and tr.a != st.last_a + 1:
            self.emit({"k": "gap", "sym": tr.sym, "from_a": st.last_a + 1, "to_a": tr.a - 1,
                       "T0": st.last_t, "T1": tr.T, "recv": tr.recv})
            # закрытию минуты, задетой дырой, верить нельзя — со следующей минуты
            st.valid_from = tr.T // 60_000 * 60_000 + 60_000
        st.last_a = tr.a
        m = tr.T // 60_000 * 60_000
        if st.minute is None or m > st.minute:
            st.close = st.last_px if st.minute is not None else None
            st.minute = m
        # выходы — сделкой ПОСЛЕ входа (вход ниже, на этой же сделке выхода нет)
        for pos in list(self.mirror.get(tr.sym, ())):
            res = pos.on_trade(tr)
            if res:
                self.mirror[tr.sym].remove(pos)
                self.emit({"k": "exit", **res})
        pos = self.x1.get(tr.sym)
        if pos is not None:
            res = pos.on_trade(tr)
            if res:
                del self.x1[tr.sym]
                self.book.close(res)
                self.emit({"k": "exit", **res})
                st.ready_at = tr.recv + SWITCH_MS
                if len(self.book.open) == SLOTS - 1:
                    self.reopen_at = tr.recv + SWITCH_MS  # был полон: заявки сняты, ставим снова
                self._recalc_eligible(tr.recv)
        # зеркало: уровень с начала минуты, одно событие на минуту
        if st.close is not None and st.valid_from <= m and st.fired_m != m:
            lvl = level_of(st.close)
            if tr.px < lvl:
                st.fired_m = m
                p = Pos("m", tr.sym, m, lvl, tr.T, tr.recv, min(lvl, tr.px))
                self.mirror[tr.sym].append(p)
                self.emit({"k": "fill", "acct": "m", "sym": tr.sym, "t": m, "entry": lvl,
                           "t_in": tr.T, "recv": tr.recv, "a": tr.a})
        # ×1
        if (tr.sym in self.eligible and tr.sym not in self.x1 and st.x1_filled_m != m
                and tr.T >= st.ready_at):
            lvl = self.x1_level(st, tr.T)
            if lvl is not None and tr.px < lvl:
                ok, over = self.book.can_fill(tr.T)
                if ok and (over or tr.T >= self.reopen_at):
                    st.x1_filled_m = m
                    p = Pos("x1", tr.sym, m, lvl, tr.T, tr.recv, min(lvl, tr.px))
                    self.book.take(p, over)
                    self.x1[tr.sym] = p
                    self.emit({"k": "fill", "acct": "x1", "sym": tr.sym, "t": m, "entry": lvl,
                               "t_in": tr.T, "recv": tr.recv, "a": tr.a, "amt": p.amt,
                               "over": over})
                    self._recalc_eligible(tr.recv)
        st.last_px, st.last_t = tr.px, tr.T

    def has_open(self, sym: str) -> bool:
        return bool(self.mirror.get(sym)) or sym in self.x1


# --- ввод-вывод ------------------------------------------------------------------------------

class Journal:
    """Строка — событие. Исполнения и выходы — с fsync: решение должно лежать на диске до исхода."""

    def __init__(self, out: Path):
        self.dir = out / "journal"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.day, self.fh = None, None

    def write(self, rec: dict) -> None:
        now = now_ms()
        rec.setdefault("recv", now)
        d = _day(now)
        if d != self.day:
            if self.fh:
                self.fh.close()
            self.day, self.fh = d, (self.dir / f"{d}.jsonl").open("a", encoding="utf-8")
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # Сброс на диск — только решения. После обрыва связи пишутся сотни записей о пропусках, и
        # fsync каждой (десятки мс на Windows) останавливал чтение потока: биржа рвала медленного
        # читателя, обрыв давал новые пропуски — за час 63 обрыва и задержка 20 с (09.10.2026).
        if rec.get("k") in ("fill", "exit"):
            self.fh.flush()
            os.fsync(self.fh.fileno())
        elif rec.get("k") != "gap":  # редкие записи (связь, задержки раз в минуту) — сразу видны
            self.fh.flush()


def _get_json(path: str, params: dict | None = None) -> object:
    url = FAPI + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "lab"}),
                                timeout=20) as r:
        return json.load(r)


def perps() -> list[str]:
    d = _get_json("/fapi/v1/exchangeInfo")
    return sorted(x["symbol"] for x in d["symbols"] if x["contractType"] == "PERPETUAL"
                  and x["quoteAsset"] == "USDT" and x["status"] == "TRADING")


def rank_x1(counts_path: Path, trading: list[str]) -> tuple[list[str], str | None]:
    """100 монет ×1: минут обвала ≥5% за 30 суток (слой А), при равенстве — оборот за сутки."""
    if not counts_path.exists():
        return [], None
    c = json.loads(counts_path.read_text())
    counts = c["counts"]
    vol = {x["symbol"]: float(x["quoteVolume"]) for x in _get_json("/fapi/v1/ticker/24hr")}
    live = [s for s in trading if s in vol]
    ranked = sorted(live, key=lambda s: (-counts.get(s, 0), -vol.get(s, 0.0), s))
    return ranked[:ORDERS], c.get("asof")


def telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_ADMIN_ID", "")
    if not token.strip() or not chat.strip():
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=30).read()
    except urllib.error.HTTPError as e:
        print(f"Telegram: отказ {e.code}", flush=True)  # без URL: в нём токен
    except Exception as e:  # noqa: BLE001
        print(f"Telegram: {type(e).__name__}", flush=True)


class Runner:
    def __init__(self, out: Path, counts: Path):
        self.out, self.counts = out, counts
        self.journal = Journal(out)
        self.engine = Engine(self.journal.write)
        self.syms: list[str] = []
        self.conns: dict[int, set[str]] = {}
        self.last_msg = time.time()
        self.lat: list[int] = []
        self.lat_minute = 0
        self.alarm_at = 0.0
        self.rank_day: str | None = None
        self.down: dict[int, float] = {}
        self.down_s = 0.0
        self.queue: asyncio.Queue = asyncio.Queue()

    def log(self, msg: str) -> None:
        print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} {msg}", flush=True)

    async def refresh_rank(self) -> None:
        try:
            rank, asof = await asyncio.to_thread(rank_x1, self.counts, self.syms)
        except Exception as e:  # noqa: BLE001
            self.log(f"выбор 100 монет не обновлён: {type(e).__name__}")
            return
        now = now_ms()
        self.engine.set_rank(rank, now)
        self.rank_day = _day(now)
        self.journal.write({"k": "top", "day": self.rank_day, "asof": asof, "syms": rank})
        self.log(f"×1: монет {len(rank)} (счёт обвалов по {asof})" if rank
                 else "×1 ВЫКЛЮЧЕН: нет crash_counts.json слоя А")

    async def conn(self, idx: int, syms: list[str], session) -> None:
        """Одно соединение; рвётся — переподключение. Через ROTATE_S — новое внахлёст, дубли
        отсекаются номером сделки."""
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
                    if idx in self.down:
                        self.down_s += time.time() - self.down.pop(idx)
                    self.journal.write({"k": "conn", "event": "open", "conn": idx,
                                        "syms": len(syms)})
                    while True:
                        if time.time() - started > ROTATE_S:
                            asyncio.get_running_loop().create_task(self.conn(idx, syms, session))
                            await asyncio.sleep(10)  # внахлёст: новое уже подписалось
                            self.journal.write({"k": "conn", "event": "rotate", "conn": idx})
                            return
                        m = await ws.receive(timeout=SILENCE_S)
                        if m.type != aiohttp.WSMsgType.TEXT:
                            raise ConnectionError(f"ws {m.type.name}")
                        recv = now_ms()
                        d = json.loads(m.data)
                        if d.get("e") != "aggTrade":
                            continue
                        await self.queue.put(Trade(d["s"], d["a"], float(d["p"]), d["T"],
                                                   d["m"], recv))
                        self.lat.append(recv - d["E"])
            except Exception as e:  # noqa: BLE001 — связь: переподключиться
                self.down.setdefault(idx, time.time())
                self.journal.write({"k": "conn", "event": "close", "conn": idx,
                                    "err": f"{type(e).__name__}: {e}"[:200]})
                self.log(f"соединение {idx}: {type(e).__name__} — переподключение через 3 с")
                await asyncio.sleep(3)

    async def backfill(self, sym: str, from_a: int, to_a: int) -> list[Trade]:
        """Пропущенные номера сделок монеты с открытой позицией — REST, по порядку."""
        out, a = [], from_a
        while a <= to_a:
            rows = await asyncio.to_thread(_get_json, "/fapi/v1/aggTrades",
                                           {"symbol": sym, "fromId": a, "limit": 1000})
            if not rows:
                break
            now = now_ms()
            for r in rows:
                if r["a"] > to_a:
                    break
                out.append(Trade(sym, r["a"], float(r["p"]), r["T"], r["m"], now))
            a = rows[-1]["a"] + 1
        return out

    async def consume(self) -> None:
        eng = self.engine
        while True:
            tr: Trade = await self.queue.get()
            self.last_msg = time.time()
            st = eng.st.get(tr.sym)
            if st and st.last_a is not None and tr.a > st.last_a + 1 and eng.has_open(tr.sym):
                try:
                    missed = await self.backfill(tr.sym, st.last_a + 1, tr.a - 1)
                    self.journal.write({"k": "backfill", "sym": tr.sym, "n": len(missed),
                                        "from_a": st.last_a + 1, "to_a": tr.a - 1})
                    for x in missed:
                        eng.on_trade(x)
                except Exception as e:  # noqa: BLE001
                    self.log(f"докачка {tr.sym}: {type(e).__name__}")
            eng.on_trade(tr)

    async def clock(self) -> None:
        """Такт: снимки уровней ×1, задержки по минутам, тишина, выбор монет в 00:00 UTC,
        итог суток в бот, пульс для healthcheck."""
        hb = Path("/tmp/lab/crash-paper.heartbeat")
        hb.parent.mkdir(parents=True, exist_ok=True)
        while True:
            now = now_ms()
            self.engine.tick(now)
            m = now // 60_000 * 60_000
            if m != self.lat_minute:
                if self.lat:
                    s = sorted(self.lat)
                    self.journal.write({"k": "lat", "t": self.lat_minute, "n": len(s),
                                        "p50": s[len(s) // 2], "p99": s[int(len(s) * 0.99)],
                                        "max": s[-1], "down_s": round(self.down_s, 1)})
                self.lat, self.lat_minute = [], m
            if time.time() - self.last_msg > SILENCE_S and time.time() - self.alarm_at > 1800:
                self.alarm_at = time.time()
                self.journal.write({"k": "silence", "s": round(time.time() - self.last_msg)})
                await asyncio.to_thread(telegram, "Бумажный бот CryptosMX: нет сделок "
                                        f"{SILENCE_S}+ с — проверить связь")
            if self.rank_day != _day(now) and now % 86_400_000 > 5 * 60_000:
                await self.day_report(_day(now - 86_400_000))
                await self.refresh_rank()
            if time.time() - self.last_msg < SILENCE_S:
                hb.write_text(str(int(time.time())))
            await asyncio.sleep(0.05)

    async def day_report(self, d: str) -> None:
        b = self.engine.book
        rec = {"k": "day", "day": d, "x1_pnl": b.day_pnl.get(d, 0.0),
               "x1_taken": b.day_taken.get(d, 0), "x1_over": b.day_over.get(d, 0),
               "equity": b.equity, "withdrawn": b.withdrawn, "min_bal": b.min_bal,
               "down_s": round(self.down_s, 1)}
        self.journal.write(rec)
        await asyncio.to_thread(telegram, "\n".join([
            f"Бумажный бот CryptosMX (слой Б), сутки {d} UTC",
            f"счёт ×1: сделок {rec['x1_taken']} (сверх предела {rec['x1_over']}), итог "
            f"{rec['x1_pnl'] * 100:+.2f}% D0; всего {(b.withdrawn + b.equity - 1) * 100:+.2f}% D0, "
            f"мин. баланс {b.min_bal * 100:.2f}%",
            f"без связи с запуска: {self.down_s:.0f} с",
        ]))

    async def main(self) -> None:
        import aiohttp
        self.syms = await asyncio.to_thread(perps)
        off, half = await asyncio.to_thread(sync_clock)
        self.journal.write({"k": "start", "syms": len(self.syms), "clock_offset_ms": off,
                            "clock_half_rtt_ms": half,
                            "rule": "limit5|side", "orders": ORDERS, "wait_ms": WAIT_MS,
                            "switch_ms": SWITCH_MS, "cancel_ms": CANCEL_MS})
        self.log(f"старт: монет {len(self.syms)}, поправка часов к бирже {off} мс "
                 f"(точность ±{half} мс)")
        await self.refresh_rank()
        parts = [self.syms[i::CONNS] for i in range(CONNS)]
        async with aiohttp.ClientSession() as session:
            tasks = [asyncio.create_task(self.conn(i, p, session)) for i, p in enumerate(parts)]
            tasks += [asyncio.create_task(self.consume()), asyncio.create_task(self.clock()),
                      asyncio.create_task(self.watch_listing(session))]
            await asyncio.gather(*tasks)

    async def watch_listing(self, session) -> None:
        """Раз в час — новые листинги: отдельным соединением (старые не трогаем)."""
        idx = CONNS
        while True:
            await asyncio.sleep(3600)
            try:
                off, half = await asyncio.to_thread(sync_clock)
                self.journal.write({"k": "clock", "off": off, "half_rtt": half})
            except Exception:  # noqa: BLE001
                pass
            try:
                now = await asyncio.to_thread(perps)
            except Exception:  # noqa: BLE001
                continue
            new = sorted(set(now) - set(self.syms))
            if new:
                self.syms += new
                self.journal.write({"k": "listing", "syms": new})
                asyncio.get_running_loop().create_task(self.conn(idx, new, session))
                idx += 1


# --- отчёт: сверка зеркала с архивом и счёт --------------------------------------------------

def read_journal(out: Path, d1: str, d2: str) -> list[dict]:
    rows = []
    for p in sorted((out / "journal").glob("*.jsonl")):
        if not (d1 <= p.stem <= (date.fromisoformat(d2) + timedelta(days=1)).isoformat()):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def report(out: Path, archive: Path, d1: str, d2: str) -> int:
    pf = _load("crash_bounce_portfolio")
    rows = read_journal(out, d1, d2)
    exits = {(r["acct"], r["sym"], r["t"], r["t_in"]): r for r in rows if r.get("k") == "exit"}
    fills = [r for r in rows if r.get("k") == "fill"]
    in_win = lambda r: d1 <= _day(r["t"]) <= d2  # noqa: E731
    mirror = [exits[k] for k in exits if k[0] == "m" and in_win(exits[k])]
    x1 = [exits[k] for k in exits if k[0] == "x1" and in_win(exits[k])]
    open_m = [f for f in fills if f["acct"] == "m" and in_win(f)
              and ("m", f["sym"], f["t"], f["t_in"]) not in exits]
    gaps = [r for r in rows if r.get("k") == "gap" and d1 <= _day(r["T1"]) <= d2]
    lat = [r for r in rows if r.get("k") == "lat" and d1 <= _day(r["t"]) <= d2]
    print(f"Бумажный бот, {d1}…{d2} UTC: зеркало — сделок {len(mirror)} (не закрыто "
          f"{len(open_m)}), ×1 — {len(x1)}; дыр в номерах {len(gaps)}")
    if lat:
        p50 = sorted(x["p50"] for x in lat)[len(lat) // 2]
        print(f"задержка биржа→бот, мс: медиана минутных p50 {p50}, худший p99 "
              f"{max(x['p99'] for x in lat)}, max {max(x['max'] for x in lat)}; "
              f"без связи всего {max(x['down_s'] for x in lat):.0f} с")
    # К3: зеркало против розыгрыша архива тех же суток
    arch = {}
    with (archive / "sim.jsonl").open() as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("var") == "limit5|side" and d1 <= r["day"] <= d2:
                arch[(r["sym"], r["t"])] = r
    days_done = {tuple(k.rsplit(":", 1))[1] for k in (archive / "sim_done.txt").read_text().split()}
    blind = defaultdict(list)
    for g in gaps:
        blind[g["sym"]].append((g["T0"], g["T1"]))
    def seen(sym: str, t: int) -> bool:  # noqa: E306
        return not any(a <= t + 60_000 + HOLD_MS and t <= b for a, b in blind.get(sym, ()))
    bot = {(r["sym"], r["t"]): r for r in mirror}
    keys = [k for k in arch if _day(k[1]) in days_done and seen(*k)]
    same = [k for k in keys if k in bot and bot[k]["how"] == arch[k]["how"]
            and abs(bot[k]["net"] - arch[k]["net"]) < 1e-9]
    found = [k for k in keys if k in bot]
    extra = [k for k in bot if k not in arch and _day(k[1]) in days_done]
    s_a = sum(arch[k]["net"] for k in found)
    s_b = sum(bot[k]["net"] for k in found)
    share = len(found) / len(keys) if keys else float("nan")
    print(f"К3 зеркало↔архив (сутки, разыгранные архивом; без дыр бота): архив {len(keys)}, "
          f"найдено ботом {len(found)} ({share:.1%}), из них тот же выход и итог {len(same)}; "
          f"у бота лишних {len(extra)}; сумма итогов найденных: архив {s_a * 100:+.2f}, "
          f"бот {s_b * 100:+.2f} п.")
    for k in [k for k in keys if k not in bot][:10]:
        print(f"  нет у бота: {k[0]} {datetime.fromtimestamp(k[1] / 1000, UTC):%m-%d %H:%M} "
              f"вход {arch[k]['entry']} {arch[k]['how']} {arch[k]['net'] * 100:+.2f}%")
    for k in extra[:10]:
        print(f"  лишнее у бота: {k[0]} {datetime.fromtimestamp(k[1] / 1000, UTC):%m-%d %H:%M} "
              f"{bot[k]['how']} {bot[k]['net'] * 100:+.2f}%")
    # счета
    period = (date.fromisoformat(d1), date.fromisoformat(d2))
    days = pf._period_days(period)
    for name, trades in (("зеркало", mirror), ("архив", [arch[k] for k in arch])):
        rows_b = sorted(({**r, "day": _day(r["t"])} for r in trades),
                        key=lambda r: (r["t_in"], r["sym"]))
        b = pf.run_book(rows_b, *pf.FIXED_CELL, True, fixed=True)
        pnl = [b.day_pnl.get(d, 0.0) for d in days]
        print(f"счёт {name} (run_book, D0, 1% × 3): сделок {b.taken}, итог "
              f"{(b.withdrawn + b.equity - 1) * 100:+.2f}% D0, t {pf._t(pnl):+.2f}, "
              f"падение ниже D0 {(1 - b.min_bal) * 100:.2f}%")
    tot = sum(r["pnl"] for r in x1)
    bal = min((r["bal"] for r in x1), default=1.0)
    print(f"счёт ×1 (на ходу): сделок {len(x1)} (сверх предела {sum(r['over'] for r in x1)}), "
          f"итог {tot * 100:+.2f}% D0, наименьший баланс {bal * 100:.2f}% "
          f"(К5 итог > 0 — {'да' if tot > 0 else 'нет'})")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--counts", type=Path, help="crash_counts.json слоя А (по умолчанию рядом)")
    p = sub.add_parser("report")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True, help="каталог слоя А (sim.jsonl)")
    p.add_argument("--from", dest="d1", required=True)
    p.add_argument("--to", dest="d2", required=True)
    a = ap.parse_args()
    if a.cmd == "report":
        return report(a.out, a.archive, a.d1, a.d2)
    counts = a.counts or a.out.parent / "crash-bounce-forward" / "crash_counts.json"
    asyncio.run(Runner(a.out, counts).main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
