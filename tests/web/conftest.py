"""Фикстуры веб-тестов: приложение на тестовой сессии, клиент с Basic-auth, фейк источников."""

import base64
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from lab.web import create_app
from lab.web.feeds_source import Budget, FeedStatus

AUTH = ("lab", "secret")


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class FakeFeeds:
    """Фейк `FeedsStatusSource` — реализацию даст ops.feeds_registry (таск 14)."""

    def __init__(self, feeds: list[FeedStatus] | None = None, budget: Budget | None = None):
        self._feeds = feeds or []
        self._budget = budget or Budget(
            month_limit_usd=Decimal(50), spent_usd=Decimal(0), forecast_usd=Decimal(0)
        )

    def status(self) -> list[FeedStatus]:
        return list(self._feeds)

    def budget(self) -> Budget:
        return self._budget


HELIUS = FeedStatus(
    id="helius",
    name="Helius",
    kind="rpc",
    health="degraded",
    health_detail="429 за последний час",
    quota_used=800_000,
    quota_limit=1_000_000,
    quota_period="month",
    exhausted_at=datetime(2026, 9, 20, tzinfo=UTC),
    cost_month=Decimal(49),
)


@pytest.fixture
def feeds() -> FakeFeeds:
    return FakeFeeds(
        [HELIUS],
        Budget(month_limit_usd=Decimal(50), spent_usd=Decimal(49), forecast_usd=Decimal(61)),
    )


@pytest.fixture
def app(session, feeds):
    return create_app(lambda: nullcontext(session), feeds=feeds, auth=AUTH)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, headers=basic(*AUTH))


@pytest.fixture
def anon(app) -> TestClient:
    return TestClient(app)
