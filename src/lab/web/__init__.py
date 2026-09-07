"""Веб-экран исследователя: FastAPI + Jinja2 + HTMX за HTTP Basic (Решения §1, §15).

`create_app(session_scope, feeds=, auth=)` — фабрика приложения; `session_scope` — вызов,
возвращающий контекстный менеджер с `Session` (в проде — `lab.db.session_scope`, в тестах —
`nullcontext(session)`). `feeds` — любой объект с `status()`/`budget()` (`FeedsStatusSource`).
"""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from lab.web.feeds_source import Budget, FeedsStatusSource, FeedStatus, NoFeedsSource

__all__ = [
    "Budget",
    "FeedStatus",
    "FeedsStatusSource",
    "NoFeedsSource",
    "bind_address",
    "create_app",
    "credentials_from_env",
    "serve",
]

HERE = Path(__file__).parent
SessionScope = Callable[[], AbstractContextManager[Session]]
DEFAULT_BIND = ("127.0.0.1", 8080)

_basic = HTTPBasic(auto_error=False)
_BASIC_DEP = Depends(_basic)


def bind_address(env: dict[str, str] | None = None) -> tuple[str, int]:
    """`WEB_BIND` вида `host:port`; по умолчанию только localhost (§15)."""
    raw = (env if env is not None else os.environ).get("WEB_BIND", "").strip()
    if not raw:
        return DEFAULT_BIND
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"WEB_BIND должен быть вида host:port, получено {raw!r}")
    return host, int(port)


def credentials_from_env(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    e = env if env is not None else os.environ
    user, password = e.get("WEB_USER", ""), e.get("WEB_PASSWORD", "")
    return (user, password) if user and password else None


def create_app(
    session_scope: SessionScope,
    *,
    feeds: FeedsStatusSource | None = None,
    auth: tuple[str, str] | None,
) -> FastAPI:
    from lab.web import routes

    app = FastAPI(title="crypto-trading-lab", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session_scope = session_scope
    app.state.feeds = feeds or NoFeedsSource()
    app.state.auth = auth
    app.state.templates = routes.make_templates(HERE / "templates")
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.include_router(routes.router, dependencies=[Depends(require_auth)])
    return app


def serve(
    session_factory: Callable[[], Any],
    *,
    once: bool = False,
    env: dict[str, str] | None = None,
    run: Callable[..., None] | None = None,
) -> int:
    """`lab service web`: приложение на `WEB_BIND` за Basic-auth из `.env`.

    Без `WEB_USER`/`WEB_PASSWORD` не стартует — иначе экран был бы открыт всем (§15).
    `once` — собрать приложение и выйти (проверка конфигурации, без сервера).
    """
    from lab.db import session_scope

    auth = credentials_from_env(env)
    if auth is None:
        print("WEB_USER и WEB_PASSWORD не заданы в .env — веб не поднимаю", file=sys.stderr)
        return 2
    host, port = bind_address(env)
    factory = session_factory()
    app = create_app(lambda: session_scope(factory), auth=auth)
    print(f"Веб-экран: http://{host}:{port}/ (пользователь {auth[0]})")
    if once:
        return 0
    if run is None:
        import uvicorn

        run = uvicorn.run
    run(app, host=host, port=port, log_level="info")
    return 0


def require_auth(
    request: Request, credentials: HTTPBasicCredentials | None = _BASIC_DEP
) -> str:
    expected: tuple[str, str] | None = request.app.state.auth
    unauthorized = HTTPException(
        status_code=401,
        detail="Нужен пароль (WEB_USER / WEB_PASSWORD)",
        headers={"WWW-Authenticate": 'Basic realm="crypto-trading-lab"'},
    )
    if expected is None or credentials is None:
        raise unauthorized
    user_ok = secrets.compare_digest(credentials.username.encode(), expected[0].encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), expected[1].encode())
    if not (user_ok and pass_ok):
        raise unauthorized
    return credentials.username


def db_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_scope() as s:
        yield s


def templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates
