"""Очередь allowlist-возможностей (История 74b, R23.3).

Участие в розыгрышах и заданиях — ручное: система не притворяется, что умеет выигрывать
раффлы. Она держит очередь с дедлайнами, шлёт карточку `bot.send_card("allowlist", …)`,
принимает отметку «место получено» и после неё ставит минт на момент открытия.

Кнопки общей карточки читаются здесь так: «Разрешить» = место получено, «Запретить» =
пропускаем. Поле `address` карточки занимает коллекция — карточка одна на все ветки.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

STATUSES = ("open", "seat", "declined", "expired", "minted")
KINDS = ("raffle", "task", "presale", "public")


@dataclass
class AllowlistOpportunity:
    """Возможность получить место: розыгрыш, задание, пресейл."""

    collection: str
    chain: str = ""
    market: str = ""
    kind: str = "raffle"
    deadline: datetime | None = None
    mint_at: datetime | None = None
    url: str = ""
    note: str = ""
    price: Decimal | None = None
    id: str = ""
    status: str = "open"
    seat_at: datetime | None = None
    decided_at: datetime | None = None
    meta: dict = field(default_factory=dict)

    @property
    def has_seat(self) -> bool:
        return self.status == "seat"

    def expired(self, now: datetime) -> bool:
        return self.deadline is not None and now > self.deadline and self.status == "open"

    def card_payload(self) -> dict:
        deadline = self.deadline.isoformat() if self.deadline else "без дедлайна"
        reason = (
            f"{self.kind}: {self.note or 'участие ручное'} — дедлайн {deadline}"
            + (f", ссылка {self.url}" if self.url else "")
        )
        return {
            "request_id": self.id,
            "address": self.collection,  # общая карточка: место коллекции — поле адреса
            "chain": self.chain or self.market,
            "reason": reason,
            "collection": self.collection,
            "market": self.market,
            "kind": self.kind,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "mint_at": self.mint_at.isoformat() if self.mint_at else None,
            "url": self.url,
        }


class AllowlistQueue:
    """Очередь в памяти; `session` — необязательное зеркало в базе для перезапусков."""

    def __init__(self, *, bot=None, session=None, clock=None) -> None:
        self.bot = bot
        self.session = session
        self._clock = clock or (lambda: datetime.now(UTC))
        self._items: dict[str, AllowlistOpportunity] = {}
        self._ids = count(1)
        self._announced: set[str] = set()
        if session is not None:
            self._load()

    def _now(self) -> datetime:
        return self._clock()

    # -- очередь ------------------------------------------------------------------------

    def add(self, opportunity: AllowlistOpportunity) -> AllowlistOpportunity:
        if not opportunity.id:
            opportunity.id = f"al-{next(self._ids)}"
        self._items[opportunity.id] = opportunity
        self._save(opportunity)
        return opportunity

    def get(self, request_id: str) -> AllowlistOpportunity | None:
        return self._items.get(request_id)

    def all(self) -> list[AllowlistOpportunity]:
        return sorted(
            self._items.values(),
            key=lambda o: (o.deadline or datetime.max.replace(tzinfo=UTC), o.collection),
        )

    def open(self, *, now: datetime | None = None) -> list[AllowlistOpportunity]:
        at = now or self._now()
        self.expire(now=at)
        return [o for o in self.all() if o.status == "open"]

    def expire(self, *, now: datetime | None = None) -> list[AllowlistOpportunity]:
        at = now or self._now()
        gone = []
        for item in self._items.values():
            if item.expired(at):
                item.status = "expired"
                item.decided_at = at
                self._save(item)
                gone.append(item)
        return gone

    # -- карточки -----------------------------------------------------------------------

    def cards(self, *, now: datetime | None = None) -> list[tuple[str, dict]]:
        """Что нужно показать оператору — по одной карточке на возможность."""
        return [("allowlist", o.card_payload()) for o in self.open(now=now)]

    async def announce(self, *, now: datetime | None = None) -> list[str]:
        """Отправка карточек в Telegram. Повторно одну и ту же карточку не шлём."""
        if self.bot is None:
            return []
        sent: list[str] = []
        for item in self.open(now=now):
            if item.id in self._announced:
                continue
            msg_id = await self.bot.send_card("allowlist", item.card_payload())
            self._announced.add(item.id)
            sent.append(str(msg_id))
        return sent

    # -- решения оператора ---------------------------------------------------------------

    def decide(self, request_id: str, decision: str, *, now: datetime | None = None):
        """Обработчик `on_allowlist` бота: `allow` — место получено, `deny` — пропускаем."""
        granted = decision in ("allow", "seat", "granted", "yes")
        return self.mark_seat(request_id, granted=granted, now=now)

    def mark_seat(
        self, request_id: str, *, granted: bool = True, now: datetime | None = None
    ) -> AllowlistOpportunity:
        item = self._items.get(request_id)
        if item is None:
            raise KeyError(f"allowlist: возможность {request_id} неизвестна")
        at = now or self._now()
        item.status = "seat" if granted else "declined"
        item.seat_at = at if granted else None
        item.decided_at = at
        self._save(item)
        return item

    # -- расписание минта ------------------------------------------------------------------

    def due(
        self, *, now: datetime | None = None, within: timedelta = timedelta(0)
    ) -> list[AllowlistOpportunity]:
        """Места, по которым пора минтить: место получено и момент открытия наступил."""
        at = (now or self._now()) + within
        return [
            o
            for o in self.all()
            if o.status == "seat" and o.mint_at is not None and o.mint_at <= at
        ]

    def mark_minted(self, request_id: str, *, now: datetime | None = None) -> AllowlistOpportunity:
        item = self._items[request_id]
        item.status = "minted"
        item.decided_at = now or self._now()
        self._save(item)
        return item

    def schedule(
        self, *, now: datetime | None = None
    ) -> list[tuple[datetime, AllowlistOpportunity]]:
        """Расписание будущих минтов по полученным местам — для `ops.scheduler`."""
        at = now or self._now()
        return sorted(
            (
                (o.mint_at, o)
                for o in self.all()
                if o.status == "seat" and o.mint_at is not None and o.mint_at > at
            ),
            key=lambda pair: pair[0],
        )

    # -- зеркало в базе ----------------------------------------------------------------------

    def _save(self, item: AllowlistOpportunity) -> None:
        if self.session is None:
            return
        from lab.nft.models import NftAllowlistRow

        row = self.session.get(NftAllowlistRow, item.id)
        if row is None:
            row = NftAllowlistRow(id=item.id)
            self.session.add(row)
        row.collection = item.collection
        row.chain = item.chain
        row.market = item.market
        row.kind = item.kind
        row.status = item.status
        row.url = item.url
        row.note = item.note
        row.deadline = item.deadline
        row.mint_at = item.mint_at
        row.seat_at = item.seat_at
        row.decided_at = item.decided_at
        self.session.flush()

    def _load(self) -> None:
        from sqlalchemy import select

        from lab.nft.models import NftAllowlistRow

        rows = self.session.execute(select(NftAllowlistRow)).scalars().all()
        for row in rows:
            self._items[row.id] = AllowlistOpportunity(
                id=row.id,
                collection=row.collection,
                chain=row.chain or "",
                market=row.market or "",
                kind=row.kind or "raffle",
                status=row.status or "open",
                url=row.url or "",
                note=row.note or "",
                deadline=row.deadline,
                mint_at=row.mint_at,
                seat_at=row.seat_at,
                decided_at=row.decided_at,
            )


def opportunities_from_upcoming(
    candidates: Sequence, *, kind: str = "raffle", lead: timedelta = timedelta(days=1)
) -> list[AllowlistOpportunity]:
    """Из ленты минтов — возможности: дедлайн участия ставим за `lead` до открытия."""
    out: list[AllowlistOpportunity] = []
    for candidate in candidates:
        mint = candidate.mint
        starts = mint.starts_at
        out.append(
            AllowlistOpportunity(
                collection=mint.collection,
                chain=mint.chain,
                market=mint.market,
                kind=kind,
                deadline=(starts - lead) if starts else None,
                mint_at=starts,
                price=mint.price,
                url=str(mint.meta.get("url", "")),
                note=f"индекс внимания {candidate.attention.value}",
            )
        )
    return out


