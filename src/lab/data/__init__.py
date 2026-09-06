"""Данные: parquet-хранилище свечей (DuckDB на чтение) и бэкфилл с возобновлением (решение §2)."""

from lab.data.backfill import BackfillInterrupted, BackfillResult, backfill
from lab.data.store import SCHEMA, CandleStore, WriteResult

__all__ = [
    "SCHEMA",
    "BackfillInterrupted",
    "BackfillResult",
    "CandleStore",
    "WriteResult",
    "backfill",
]
