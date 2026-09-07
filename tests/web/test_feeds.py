"""`/feeds` читает `status()`/`budget()` реестра источников (R13.3, A01); реализации нет —
фейк в тестах, без источника — честное «не подключён»."""

from contextlib import nullcontext

from fastapi.testclient import TestClient

from lab.web import create_app
from tests.web.conftest import AUTH, basic


def test_feeds_screen_shows_quota_health_forecast_and_budget(client):
    r = client.get("/feeds")
    assert r.status_code == 200
    body = r.text
    assert "Helius" in body and "429 за последний час" in body
    assert "800000 / 1000000" in body and "(80 %)" in body
    assert "20.09.2026" in body  # прогноз исчерпания квоты
    assert "61.00 USD" in body and "близко к лимиту" in body  # бюджетомер A01
    assert "не подключён" not in body


def test_feeds_without_registry_says_so_honestly(session):
    app = create_app(lambda: nullcontext(session), auth=AUTH)
    r = TestClient(app, headers=basic(*AUTH)).get("/feeds")
    assert r.status_code == 200
    assert "не подключён" in r.text
    assert "ops.feeds_registry" in r.text
