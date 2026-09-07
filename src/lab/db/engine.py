"""Engine и фабрика сессий. URL — только из DATABASE_URL: окружение, иначе .env.

Тот же порядок, что в migrations/env.py: CLI, сервисы и Alembic смотрят в одну базу.
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from lab.config.env import load_dotenv


class DatabaseUrlMissing(RuntimeError):
    pass


def database_url(*, environ: Mapping[str, str] | None = None, dotenv: Path | str = ".env") -> str:
    """DATABASE_URL из окружения, иначе из .env; нет нигде — ошибка, не localhost."""
    source = os.environ if environ is None else environ
    url = source.get("DATABASE_URL", "").strip() or load_dotenv(dotenv).get("DATABASE_URL", "")
    url = url.strip()
    if not url:
        raise DatabaseUrlMissing(
            f"DATABASE_URL не задан ни в окружении, ни в {Path(dotenv).resolve()} "
            "— впиши его по образцу из .env.example"
        )
    return url


def alembic_url(
    *,
    config_url: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv: Path | str = ".env",
) -> str:
    """База для миграций: заданная программно → `DATABASE_URL` → `.env` → `alembic.ini`.

    Программный вызов (`command.upgrade(Config(...))` в тестах и скриптах) выбирает базу
    сам и должен быть сильнее окружения: иначе непустой `DATABASE_URL` в `.env` уводит
    миграции тестов на боевую базу.
    """
    forced = str((attributes or {}).get("sqlalchemy.url") or "").strip()
    if forced:
        return forced
    source = os.environ if environ is None else environ
    url = source.get("DATABASE_URL", "").strip() or load_dotenv(dotenv).get("DATABASE_URL", "")
    return url.strip() or (config_url or "")


def make_engine(url: str | None = None, **kwargs) -> Engine:
    return create_engine(url or database_url(), future=True, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
