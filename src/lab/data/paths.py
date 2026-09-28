"""Имя инструмента, площадки или актива → часть пути в хранилищах данных.

До 28.09.2026 каждое хранилище (свечи, фандинг, позиционирование, суточные ряды) держало
свою копию замены «всё вне `[A-Za-z0-9._-]` → `_`». Для имён по ccxt это безвредно
(`BTC/USDT:USDT` → `BTC_USDT_USDT`), но с 2025 года у Binance есть перпы с именами из
иероглифов, и замена их СКЛЕИВАЛА: `龙虾/USDT:USDT` и `牛来/USDT:USDT` давали одну папку
`___USDT_USDT`. Свечи двух разных монет легли бы в один ряд, и понять это по ряду нельзя —
цены правдоподобные, дубли по времени молча отбрасываются при слиянии.

Теперь не-ASCII символ кодируется процентами по UTF-8 (`牛` → `%E7%89%9B`). ASCII-имена не
меняются вовсе, поэтому существующие папки остаются на месте. А знака `%` прежняя замена не
выдавала никогда, так что новое имя не совпадёт ни с одним старым: отображение взаимно
однозначно для любых не-ASCII имён.
"""

from __future__ import annotations

from urllib.parse import quote

_KEEP = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def safe_part(part: str) -> str:
    out: list[str] = []
    for ch in part:
        if ch in _KEEP:
            out.append(ch)
        elif ch.isascii():
            out.append("_")
        else:
            out.append(quote(ch, safe=""))
    return "".join(out)


__all__ = ["safe_part"]
