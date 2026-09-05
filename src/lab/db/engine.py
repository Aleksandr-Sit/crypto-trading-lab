"""Engine и фабрика сессий. URL — только из DATABASE_URL: окружение, иначе .env.

Тот же порядок, что в migrations/env.py: CLI, сервисы и Alembic смотрят в одну базу.
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

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
