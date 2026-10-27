"""Session login: credentials, cookies, logout, expiry, restart persistence."""

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN, OWNER, login, login_admin, login_owner
from service.api import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "auth.sqlite3")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        yield value


def test_login_returns_the_user_and_sets_an_httponly_session_cookie(client):
    response = client.post("/v1/auth/login", json={"username": OWNER["username"], "password": OWNER["password"]})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "id": response.json()["data"]["id"],
        "username": "owner",
        "display_name": "Bu Sari",
        "role": "owner",
        "restaurant_id": "demo-resto",
        "restaurant_name": "Warung Bu Sari",
    }
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("restopilot_session=")
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "path=/" in cookie.lower()


def test_me_reports_the_session_user_after_login(client):
    login_owner(client)
    me = client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["data"]["role"] == "owner"


def test_wrong_password_and_unknown_user_are_indistinguishable_401s(client):
    wrong = client.post("/v1/auth/login", json={"username": "owner", "password": "not-the-password"})
    unknown = client.post("/v1/auth/login", json={"username": "nobody", "password": "whatever123"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"
    assert client.get("/v1/auth/me").status_code == 401


def test_malformed_login_bodies_are_rejected(client):
    assert client.post("/v1/auth/login", json={"username": "ab", "password": "x"}).status_code == 422
    assert client.post("/v1/auth/login", json={"username": "owner"}).status_code == 422
    assert client.post("/v1/auth/login", json={"username": "owner", "password": "x", "extra": 1}).status_code == 422


def test_logout_deletes_the_session_so_the_token_stops_working(client):
    login_owner(client)
    assert client.get("/v1/auth/me").status_code == 200
    assert client.post("/v1/auth/logout").status_code == 200
    assert client.get("/v1/auth/me").status_code == 401
    # A second logout with the same (now dead) cookie is still fine.
    assert client.post("/v1/auth/logout").status_code == 200


def test_expired_sessions_are_rejected_and_pruned(client):
    login_owner(client)
    with client.app.state.store.transaction() as conn:
        conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
    assert client.get("/v1/auth/me").status_code == 401
    with client.app.state.store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_sessions_survive_a_service_restart(tmp_path):
    path = tmp_path / "restart.sqlite3"
    first = create_app(path)
    jar = TestClient(first, base_url="http://127.0.0.1")
    login_owner(jar)
    assert jar.get("/v1/overview").status_code == 200
    second = create_app(path)
    restarted = TestClient(second, base_url="http://127.0.0.1")
    # Same cookie jar contents, a new Store instance over the same database file.
    restarted.cookies = jar.cookies
    assert restarted.get("/v1/overview").status_code == 200


def test_admin_login_has_no_restaurant(client):
    login_admin(client)
    me = client.get("/v1/auth/me").json()["data"]
    assert me["role"] == "admin"
    assert me["restaurant_id"] is None
    assert me["restaurant_name"] is None


def test_seeded_logins_match_the_documented_credentials(app):
    store = app.state.store
    for user in (OWNER, ADMIN):
        found = store.verify_login(user["username"], user["password"])
        assert found["role"] == user["role"]
