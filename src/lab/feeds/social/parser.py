"""Парсер публичных сигналов из текста поста: инструмент, направление, вход, цели, стоп.

Правила простые и прозрачные: тикер (BTC, ETH/USDT, #SOL), слово направления (long/лонг/покупка/buy,
short/шорт/продажа/sell), числа после «вход/entry/от/по», «цель/тейк/tp/→», «стоп/sl».
Что не разобрано — `None`; текст без тикера и направления — не сигнал (`None`)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

QUOTE = "USDT"
_NUM = r"(\d[\d\s]*(?:[.,]\d+)?)"
_TICKER = re.compile(r"(?<![A-Za-z])#?([A-Z]{2,6})(?:/([A-Z]{3,5}))?(?![a-z])")
_LONG = re.compile(r"\b(long|лонг|покупк\w*|покупа\w*|buy|куплю)\b", re.I)
_SHORT = re.compile(r"\b(short|шорт\w*|прода\w*|sell)\b", re.I)
_ENTRY = re.compile(r"(?:вход|entry|от|по)\s*[:=]?\s*" + _NUM, re.I)
_TARGETS = re.compile(
    r"(?:цел[ьи]|тейк\w*|tp|take|→|->)\s*[:=]?\s*((?:" + _NUM + r"[\s,;]*)+)", re.I
)
_STOP = re.compile(r"(?:стоп\w*|sl|stop\w*)\s*[:=]?\s*" + _NUM, re.I)
_STOPWORDS = {"USDT", "USD", "LONG", "SHORT", "TP", "SL", "BUY", "SELL", "RSI", "MA", "ATH", "ETF"}


@dataclass
class ParsedSignal:
    instrument: str
    side: str  # long | short
    entry: Decimal | None = None
    targets: list[Decimal] = field(default_factory=list)
    stop: Decimal | None = None
    raw: str = ""

    def as_dict(self) -> dict:
        return {
            "instrument": self.instrument,
            "side": self.side,
            "entry": None if self.entry is None else str(self.entry),
            "targets": [str(t) for t in self.targets],
            "stop": None if self.stop is None else str(self.stop),
        }


def _num(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(" ", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def parse_signal(text: str) -> ParsedSignal | None:
    ticker = None
    for m in _TICKER.finditer(text):
        base, quote = m.group(1), m.group(2)
        if base in _STOPWORDS:
            continue
        ticker = f"{base}/{quote or QUOTE}"
        break
    if ticker is None:
        return None
    is_long, is_short = bool(_LONG.search(text)), bool(_SHORT.search(text))
    if is_long == is_short:
        return None
    side = "long" if is_long else "short"
    entry = None
    if m := _ENTRY.search(text):
        entry = _num(m.group(1))
    elif m := re.search(re.escape(ticker.split("/")[0]) + r"(?:/[A-Z]+)?\s+" + _NUM, text):
        entry = _num(m.group(1))  # «Продаю LINK 14.2 → 13» — число сразу после тикера
    targets: list[Decimal] = []
    if m := _TARGETS.search(text):
        targets = [v for v in (_num(x) for x in re.findall(_NUM, m.group(1))) if v is not None]
    stop = None
    if m := _STOP.search(text):
        stop = _num(m.group(1))
    return ParsedSignal(
        instrument=ticker, side=side, entry=entry, targets=targets, stop=stop, raw=text
    )


__all__ = ["ParsedSignal", "parse_signal"]
