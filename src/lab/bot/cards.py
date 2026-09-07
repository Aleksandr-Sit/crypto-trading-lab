"""Карточки Telegram: шаблоны на русском + inline-кнопки (истории 4–7, 11, 95).

`render_card(kind, payload) -> Card(text, buttons)`. Данные callback'ов — короткие строки
(Telegram даёт 64 байта): `sig:<id>:executed|skipped|confirm|reject`, `cand:<id>:accept|reject`,
`rebal:<id>:apply|skip`, `allow:<id>:allow|deny`, `confirm:<token>`, `cancel:<token>`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from lab.ops.outbox import Button

SAMARA = ZoneInfo("Europe/Samara")

CARD_KINDS: tuple[str, ...] = (
    "signal",
    "confirm",
    "fill",
    "transition",
    "candidate",
    "rebalance",
    "allowlist",
    "alert",
)

SIDE_RU = {"buy": "покупка", "sell": "продажа", "long": "лонг", "short": "шорт"}
STATUS_RU = {
    "candidate": "кандидат",
    "measuring": "замер",
    "passed": "прошла",
    "failed": "не прошла",
    "degraded": "деградация",
    "retired": "в архиве",
}


class Card(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    text: str
    buttons: list[list[Button]] = Field(default_factory=list)
    ref: str | None = None


def _g(p: Mapping[str, Any], key: str, default: Any = "н/д") -> str:
    v = p.get(key)
    return escape(str(v)) if v not in (None, "") else str(default)


def _side(p: Mapping[str, Any]) -> str:
    s = str(p.get("side", "")).lower()
    return SIDE_RU.get(s, s or "н/д")


def _ts(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(SAMARA).strftime("%d.%m %H:%M")
    return _g({"v": value}, "v")


def _minutes(ttl_s: Any) -> str:
    try:
        secs = int(ttl_s)
    except (TypeError, ValueError):
        return "н/д"
    return f"{secs // 60} мин" if secs >= 60 else f"{secs} с"


def _signal(p: Mapping[str, Any]) -> Card:
    sid = str(p["signal_id"])
    rung = str(p.get("rung", "signal"))
    head = {"signal": "📣 Сигнал", "semi": "🟡 Сигнал — нужно подтверждение", "auto": "🤖 Сигнал"}
    text = (
        f"<b>{head.get(rung, '📣 Сигнал')}</b> · <code>{_g(p, 'strategy_id')}</code>\n"
        f"{_side(p)} <b>{_g(p, 'instrument')}</b> на {_g(p, 'venue')}\n"
        f"размер: {_g(p, 'size')} · цена: {_g(p, 'price_ref')}\n"
        f"ступень: {rung} · ждём ответа: {_minutes(p.get('ttl_s'))}"
    )
    if rung == "signal":
        buttons = [[Button(text="Исполнил", data=f"sig:{sid}:executed"),
                    Button(text="Пропустил", data=f"sig:{sid}:skipped")]]
    elif rung == "semi":
        buttons = [[Button(text="Подтвердить", data=f"sig:{sid}:confirm"),
                    Button(text="Отклонить", data=f"sig:{sid}:reject")]]
    else:
        buttons = []
    return Card(kind="signal", text=text, buttons=buttons, ref=sid)


def _confirm(p: Mapping[str, Any]) -> Card:
    token = str(p["token"])
    text = f"⚠️ <b>{_g(p, 'text', 'Подтвердить действие?')}</b>\nДействие: {_g(p, 'action')}"
    buttons = [[Button(text="Подтвердить", data=f"confirm:{token}"),
                Button(text="Отмена", data=f"cancel:{token}")]]
    return Card(kind="confirm", text=text, buttons=buttons, ref=f"confirm:{token}")


def _fill(p: Mapping[str, Any]) -> Card:
    text = (
        f"✅ <b>Исполнено</b> · <code>{_g(p, 'strategy_id')}</code>\n"
        f"{_side(p)} {_g(p, 'qty')} <b>{_g(p, 'instrument')}</b> по {_g(p, 'price')} "
        f"на {_g(p, 'venue')}\nиздержки: {_g(p, 'costs')} USD"
    )
    sid = p.get("signal_id")
    return Card(kind="fill", text=text, ref=f"fill:{sid}" if sid else None)


def _transition(p: Mapping[str, Any]) -> Card:
    status = STATUS_RU.get(str(p.get("status", "")), _g(p, "status"))
    arrow = "⬆️" if p.get("to_rung") != p.get("from_rung") else "⛔"
    text = (
        f"{arrow} <b>Лестница</b> · <code>{_g(p, 'strategy_id')}</code>\n"
        f"{_g(p, 'from_rung')} → {_g(p, 'to_rung')} · статус: {status}\n"
        f"причина: {_g(p, 'reason')} · кто: {_g(p, 'by', 'система')}"
    )
    return Card(kind="transition", text=text, ref=f"tr:{p.get('id', '')}" if p.get("id") else None)


def _candidate(p: Mapping[str, Any]) -> Card:
    cid = str(p["candidate_id"])
    text = (
        f"🔎 <b>Кандидат #{cid}</b>\nисточник: {_g(p, 'kind')} · {_g(p, 'ref')}\n"
        f"{_g(p, 'summary', 'Взять в замер?')}"
    )
    buttons = [[Button(text="В замер", data=f"cand:{cid}:accept"),
                Button(text="Отклонить", data=f"cand:{cid}:reject")]]
    return Card(kind="candidate", text=text, buttons=buttons, ref=f"cand:{cid}")


def _rebalance(p: Mapping[str, Any]) -> Card:
    pid = str(p["proposal_id"])
    text = (
        f"♻️ <b>Перелив #{pid}</b>\n{_g(p, 'from_branch')} → {_g(p, 'to_branch')}: "
        f"{_g(p, 'amount_usd')} USD\n{_g(p, 'detail', 'Излишек рисковой ветки (G05)')}"
    )
    buttons = [[Button(text="Перелить", data=f"rebal:{pid}:apply"),
                Button(text="Оставить", data=f"rebal:{pid}:skip")]]
    return Card(kind="rebalance", text=text, buttons=buttons, ref=f"rebal:{pid}")


def _allowlist(p: Mapping[str, Any]) -> Card:
    rid = str(p["request_id"])
    text = (
        f"🧾 <b>Allowlist #{rid}</b>\nадрес: <code>{_g(p, 'address')}</code> "
        f"({_g(p, 'chain')})\nпочему: {_g(p, 'reason')}"
    )
    buttons = [[Button(text="Разрешить", data=f"allow:{rid}:allow"),
                Button(text="Запретить", data=f"allow:{rid}:deny")]]
    return Card(kind="allowlist", text=text, buttons=buttons, ref=f"allow:{rid}")


def _alert(p: Mapping[str, Any]) -> Card:
    silent = p.get("silent_for_s")
    head = f"🚨 <b>Сервис {_g(p, 'service')} не отвечает"
    head += f" {_minutes(silent)}</b>" if silent else "</b>"
    if p.get("title"):
        head = f"🚨 <b>{_g(p, 'title')}</b>"
    text = f"{head}\n{_g(p, 'detail', '')}".rstrip()
    if p.get("at"):
        text += f"\nвремя: {_ts(p['at'])}"
    return Card(kind="alert", text=text)


_RENDERERS = {
    "signal": _signal,
    "confirm": _confirm,
    "fill": _fill,
    "transition": _transition,
    "candidate": _candidate,
    "rebalance": _rebalance,
    "allowlist": _allowlist,
    "alert": _alert,
}


def render_card(kind: str, payload: Mapping[str, Any]) -> Card:
    try:
        renderer = _RENDERERS[kind]
    except KeyError:
        raise ValueError(f"неизвестный вид карточки: {kind!r}; есть {CARD_KINDS}") from None
    return renderer(payload)


def strip_buttons(card: Card, suffix: str) -> Card:
    """Та же карточка без кнопок и с припиской (после ответа оператора или таймаута)."""
    return Card(kind=card.kind, text=f"{card.text}\n\n{suffix}", buttons=[], ref=card.ref)
