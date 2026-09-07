"""Маршруты: `/`, `/strategies`, `/strategies/{id}`, `/feeds`, `/queue`, `/graveyard`, CSV.

HTMX: запрос с заголовком `HX-Request` получает только фрагмент (таблицу), обычный —
целую страницу. Так фильтры и сортировка работают без перезагрузки и без JS-сборки.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from lab.contracts import Branch, CandidateDecision, Rung, Status
from lab.core.journal import Journal
from lab.core.registry import Registry, StrategyNotFound
from lab.web import views
from lab.web.feeds_source import NoFeedsSource
from lab.web.svg import equity_svg

router = APIRouter()


def _db(request: Request):
    from lab.web import db_session

    yield from db_session(request)


DB = Annotated[Session, Depends(_db)]


def make_templates(directory: Path) -> Jinja2Templates:
    t = Jinja2Templates(directory=str(directory))
    t.env.filters["fmt"] = views.fmt
    t.env.filters["samara"] = views.samara
    t.env.filters["label"] = lambda value, kind: views.label(kind, value)
    t.env.globals.update(
        branches=[b.value for b in Branch],
        rungs=[r.value for r in Rung],
        statuses=[s.value for s in Status],
        decisions=[d.value for d in CandidateDecision],
        metric_columns=views.METRIC_COLUMNS,
        branch_metrics=views.BRANCH_METRICS,
    )
    return t


def _render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    t: Jinja2Templates = request.app.state.templates
    return t.TemplateResponse(request, name, ctx)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: DB):
    return _render(request, "index.html", {"page": "index", "s": views.summary(session)})


@router.get("/strategies", response_class=HTMLResponse)
def strategies(
    request: Request,
    session: DB,
    branch: str = "",
    rung: str = "",
    status: str = "",
    sort: str = "id",
    dir: str = "asc",  # noqa: A002 — имя параметра из URL
):
    lines = views.strategy_lines(
        session, branch=branch, rung=rung, status=status, sort=sort, desc=dir == "desc"
    )
    registry_empty = not lines and not Registry(session).list()
    ctx = {
        "page": "strategies",
        "lines": lines,
        "registry_empty": registry_empty,
        "f": {"branch": branch, "rung": rung, "status": status, "sort": sort, "dir": dir},
    }
    name = "_strategies_table.html" if _is_htmx(request) else "strategies.html"
    return _render(request, name, ctx)


@router.get("/strategies/{strategy_id}", response_class=HTMLResponse)
def strategy_card(request: Request, session: DB, strategy_id: str):
    try:
        c = views.card(session, strategy_id)
    except StrategyNotFound as err:
        raise HTTPException(404, f"Стратегия {strategy_id} не найдена") from err
    return _render(
        request,
        "strategy.html",
        {"page": "strategies", "c": c, "curve_svg": equity_svg(c.curve)},
    )


@router.get("/strategies/{strategy_id}/trades.csv")
def strategy_trades_csv(session: DB, strategy_id: str):
    try:
        Registry(session).get(strategy_id)
    except StrategyNotFound as err:
        raise HTTPException(404, f"Стратегия {strategy_id} не найдена") from err
    body = Journal(session).export_csv(strategy_id=strategy_id)
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{strategy_id}-trades.csv"'},
    )


@router.get("/trades.csv")
def all_trades_csv(session: DB):
    body = Journal(session).export_csv()
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="trades.csv"'},
    )


@router.get("/feeds", response_class=HTMLResponse)
def feeds(request: Request):
    src = request.app.state.feeds
    return _render(
        request,
        "feeds.html",
        {
            "page": "feeds",
            "feeds": src.status(),
            "budget": src.budget(),
            "connected": not isinstance(src, NoFeedsSource),
        },
    )


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request, session: DB, decision: str = "pending"):
    reg = Registry(session)
    known = {d.value for d in CandidateDecision}
    items = reg.candidates(decision or None) if decision in known or decision == "" else []
    return _render(
        request,
        "queue.html",
        {"page": "queue", "items": items, "decision": decision, "total": len(reg.candidates())},
    )


@router.get("/graveyard", response_class=HTMLResponse)
def graveyard(request: Request, session: DB, reason: Annotated[str, Query()] = ""):
    lines, reasons = views.graveyard(session, reason=reason)
    ctx = {"page": "graveyard", "lines": lines, "reasons": reasons, "reason": reason}
    name = "_graveyard_table.html" if _is_htmx(request) else "graveyard.html"
    return _render(request, name, ctx)
