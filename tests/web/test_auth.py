"""Basic-auth и адрес привязки (Решения §15)."""

from lab.web import bind_address
from tests.web.conftest import basic


def test_without_password_every_screen_is_401(anon):
    for path in ("/", "/strategies", "/feeds", "/queue", "/graveyard"):
        r = anon.get(path)
        assert r.status_code == 401, path
        assert r.headers.get("www-authenticate", "").lower().startswith("basic")


def test_wrong_password_is_401(anon):
    r = anon.get("/", headers=basic("lab", "wrong"))
    assert r.status_code == 401


def test_default_bind_is_localhost(monkeypatch):
    monkeypatch.delenv("WEB_BIND", raising=False)
    assert bind_address() == ("127.0.0.1", 8080)
    monkeypatch.setenv("WEB_BIND", "0.0.0.0:9000")
    assert bind_address() == ("0.0.0.0", 9000)


def test_serve_refuses_without_credentials_and_binds_from_env(capsys):
    from lab.web import serve

    calls = []
    factory = lambda: object()  # noqa: E731 — фабрика сессий не нужна для проверки
    assert serve(factory, env={}, run=calls.append) == 2
    assert "WEB_USER" in capsys.readouterr().err
    env = {"WEB_USER": "lab", "WEB_PASSWORD": "pw", "WEB_BIND": "0.0.0.0:9000"}
    assert serve(factory, once=True, env=env, run=calls.append) == 0
    assert calls == []  # once — без сервера
    fake_run = lambda app, **kw: calls.append(kw)  # noqa: E731
    assert serve(factory, env=env, run=fake_run) == 0
    assert calls[0]["host"] == "0.0.0.0" and calls[0]["port"] == 9000
