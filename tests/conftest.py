"""Общие фикстуры. База — Postgres по TEST_DATABASE_URL (по умолчанию локальный lab_test).

Схема накатывается Alembic-миграциями один раз на сессию; каждый тест идёт в транзакции,
которая откатывается — тесты не видят данных друг друга.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
# Исполнители CEX (таск 04): в тестах — фейковый транспорт ccxt; живые API — за LAB_LIVE_TESTS=1.
if os.environ.get("LAB_LIVE_TESTS") != "1":
    os.environ.setdefault("LAB_CEX_TRANSPORT", "fake")
DEFAULT_TEST_URL = "postgresql+psycopg://lab:lab@localhost:5432/lab_test"


def _test_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def db_url() -> str:
    url = _test_url()
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        engine.dispose()
    except Exception as err:  # noqa: BLE001 — любая ошибка соединения = нет базы
        pytest.skip(f"Postgres недоступен по {url}: {err}")
    return url


@pytest.fixture(scope="session")
def migrated_engine(db_url: str) -> Iterator[Engine]:
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    connection = migrated_engine.connect()
    outer = connection.begin()
    sess = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        sess.close()
        outer.rollback()
        connection.close()


def table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())
