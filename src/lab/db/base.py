"""Declarative base и общие типы колонок. Деньги — Numeric(30, 12), время — UTC-aware."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from sqlalchemy import DateTime, MetaData, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Money = Annotated[Decimal, mapped_column(Numeric(30, 12))]
MoneyOpt = Annotated[Decimal | None, mapped_column(Numeric(30, 12))]
Json = Annotated[dict[str, Any], mapped_column(JSONB)]
JsonList = Annotated[list[Any], mapped_column(JSONB)]
Ts = Annotated[datetime, mapped_column(DateTime(timezone=True))]
TsOpt = Annotated[datetime | None, mapped_column(DateTime(timezone=True))]
Code = Annotated[str, mapped_column(String(32))]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {
        Decimal: Numeric(30, 12),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        datetime: DateTime(timezone=True),
    }
