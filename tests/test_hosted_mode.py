"""Hosted (Atoms) deployment mode — fully offline.

No test here opens a network connection: Postgres is replaced by a recording
fake pool, and the HTTP behaviour runs against an isolated SQLite store passed
in through ``store_factory``. TEST_PG is never consulted.
"""

import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service import pg
from service.api import create_app
from service.db import Store, hash_password
from service.pg_store import PostgresStore

WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|UPSERT|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|BEGIN)\b", re.I)
PLACEHOLDER_PASSWORD = "offline-placeholder-not-a-secret"
OWNER_PASSWORD = "pemilik-rahasia-9"
ADMIN_PASSWORD = "admin-rahasia-9"


# ---- recording fake Postgres -------------------------------------------------

class FakeCursor:
    def __init__(self, log, schema):
        self.log, self.schema = log, schema
        self._rows, self.description = [], None

    def execute(self, sql, params=()):
        self.log.append(sql)
        if "information_schema.columns" in sql:
            self._rows = [(table, column) for table, columns in self.schema.items() for column in columns]
            self.description = [("table_name",), ("column_name",)]
        elif sql.lstrip().upper().startswith("SELECT"):
            self._rows = []
            self.description = [(name,) for name in ("slug", "id", "name", "unit", "reorder_threshold")]
        else:
            self._rows, self.description = [], None

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    autocommit = False

    def __init__(self, log, schema):
        self.log, self.schema = log, schema

    def cursor(self):
        return FakeCursor(self.log, self.schema)

    def commit(self):
        pass

    def rollback(self):
        pass


class FakePool:
    def __init__(self, schema):
        self.log = []
        self.schema = schema

    def getconn(self):
        return FakeConn(self.log, self.schema)

    def putconn(self, conn, close=False):
        pass


@pytest.fixture
def hosted_env(monkeypatch, tmp_path):
    """Hosted env with placeholder values; .env loading points at a decoy file."""
    decoy = tmp_path / ".env"
    decoy.write_text(f"SUPABASE_DB_PASSWORD={PLACEHOLDER_PASSWORD}-from-dotenv\n")
    monkeypatch.setattr(pg, "ENV_PATH", decoy)
    monkeypatch.setenv("RESTOPILOT_HOSTED", "1")
    monkeypatch.setenv("SUPABASE_URL", "https://exampleref.supabase.co")
    for key in ("SUPABASE_DB_PASSWORD", "SUPABASE_DB_HOST", "SUPABASE_DB_PORT", "SUPABASE_DB_USER"):
        monkeypatch.delenv(key, raising=False)
    pg.reset_pool()
    yield monkeypatch
    pg.reset_pool()


def full_schema():
    return {table: set(columns) for table, columns in PostgresStore.REQUIRED_SCHEMA.items()}


def install_fake_pool(monkeypatch, schema):
    pool = FakePool(schema)
    monkeypatch.setattr(pg, "_get_pool", lambda: pool)
    return pool


def anon(app):
    return TestClient(app, base_url="https://restopilot.test", raise_server_exceptions=False)


# ---- configuration -------------------------------------------------------------

def test_direct_connection_is_the_default(hosted_env):
    hosted_env.setenv("SUPABASE_DB_PASSWORD", PLACEHOLDER_PASSWORD)
    config = pg.db_config()
    assert config["host"] == "db.exampleref.supabase.co"
    assert (config["port"], config["user"], config["dbname"]) == (5432, "postgres", "postgres")


def test_pooler_fallback_settings_are_honoured(hosted_env):
    hosted_env.setenv("SUPABASE_DB_PASSWORD", PLACEHOLDER_PASSWORD)
    hosted_env.setenv("SUPABASE_DB_HOST", "aws-0-ap-southeast-1.pooler.supabase.com")
    hosted_env.setenv("SUPABASE_DB_PORT", "6543")
    hosted_env.setenv("SUPABASE_DB_USER", "postgres.exampleref")
    config = pg.db_config()
    assert (config["host"], config["port"], config["user"]) == (
        "aws-0-ap-southeast-1.pooler.supabase.com", 6543, "postgres.exampleref")


def test_invalid_port_is_rejected(hosted_env):
    hosted_env.setenv("SUPABASE_DB_PASSWORD", PLACEHOLDER_PASSWORD)
    hosted_env.setenv("SUPABASE_DB_PORT", "not-a-port")
    with pytest.raises(RuntimeError):
        pg.db_config()


def test_hosted_mode_ignores_dotenv_files(hosted_env):
    with pytest.raises(RuntimeError):
        pg.db_config()
    import os
    assert "SUPABASE_DB_PASSWORD" not in os.environ


# ---- read-only startup ---------------------------------------------------------

def test_hosted_startup_only_reads(hosted_env, tmp_path):
    hosted_env.setenv("SUPABASE_DB_PASSWORD", PLACEHOLDER_PASSWORD)
    pool = install_fake_pool(hosted_env, full_schema())
    app = create_app(tmp_path / "never.sqlite3", seed=True, hosted=True, public_demo=True)
    assert app.state.storage == "supabase"
    assert pool.log, "startup should have validated the schema"
    writes = [sql for sql in pool.log if WRITE_SQL.match(sql)]
    assert writes == []
    assert all(sql.lstrip().upper().startswith(("SELECT", "SET TRANSACTION READ ONLY")) for sql in pool.log)
    assert not (tmp_path / "never.sqlite3").exists()
    with anon(app) as client:
        assert client.get("/health/live").json()["data"]["storage"] == "supabase"


def test_schema_mismatch_fails_closed_without_writes(hosted_env, tmp_path):
    hosted_env.setenv("SUPABASE_DB_PASSWORD", PLACEHOLDER_PASSWORD)
    schema = full_schema()
    del schema["app_sessions"]
    schema["drafts"].discard("version")
    pool = install_fake_pool(hosted_env, schema)
    app = create_app(tmp_path / "never.sqlite3", hosted=True, public_demo=True)
    assert app.state.storage == "unavailable"
    assert [sql for sql in pool.log if WRITE_SQL.match(sql)] == []
    with anon(app) as client:
        body = client.get("/v1/overview").json()
    assert body["error"]["code"] == "storage_unavailable"
    assert "app_sessions" in body["error"]["message"] and "version" in body["error"]["message"]


# ---- fail closed ---------------------------------------------------------------

def assert_fail_closed(app, tmp_path):
    assert app.state.storage == "unavailable"
    assert not (tmp_path / "never.sqlite3").exists(), "must never fall back to SQLite"
    with anon(app) as client:
        live = client.get("/health/live")
        assert live.status_code == 200 and live.json()["data"]["storage"] == "unavailable"
        for method, path in (("GET", "/health/ready"), ("GET", "/v1/overview"), ("GET", "/v1/auth/me"),
                             ("POST", "/v1/auth/login"), ("GET", "/"), ("GET", "/app")):
            kwargs = {"json": {"username": "someone", "password": "whatever-1"}} if method == "POST" else {}
            response = client.request(method, path, **kwargs)
            assert response.status_code == 500, (path, response.text)
            assert response.headers["content-type"].startswith("application/json")
            assert response.json()["error"]["code"] == "storage_unavailable"
            assert PLACEHOLDER_PASSWORD not in response.text


def test_missing_password_fails_closed(hosted_env, tmp_path):
    app = create_app(tmp_path / "never.sqlite3", hosted=True, public_demo=True)
    assert_fail_closed(app, tmp_path)


def test_unreachable_database_fails_closed(hosted_env, tmp_path):
    hosted_env.setenv("SUPABASE_DB_PASSWORD", PLACEHOLDER_PASSWORD)

    def unreachable():
        raise OSError(f"could not connect (password={PLACEHOLDER_PASSWORD})")

    hosted_env.setattr(pg, "_get_pool", unreachable)
    app = create_app(tmp_path / "never.sqlite3", hosted=True, public_demo=True)
    assert_fail_closed(app, tmp_path)


# ---- hosted HTTP behaviour on an isolated SQLite store ----------------------------

@pytest.fixture
def hosted(tmp_path):
    store = Store(tmp_path / "hosted.sqlite3", seed=True)
    app = create_app(tmp_path / "unused.sqlite3", hosted=True, public_demo=True, store_factory=lambda: store)
    return app, store


def set_password(store, username, password):
    with store.transaction() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE username=?", (hash_password(password), username))


def password_hash(store, username):
    with store.transaction() as conn:
        return conn.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()[0]


def logged_in(app, username, password):
    client = anon(app)
    response = client.post("/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def key():
    return {"Idempotency-Key": uuid4().hex}


def test_default_credentials_are_rejected_without_touching_hashes(hosted, tmp_path):
    app, store = hosted
    before = {name: password_hash(store, name) for name in ("admin", "owner")}
    with anon(app) as client:
        for username, password in (("admin", "admin123"), ("owner", "owner123"), ("OWNER", "owner123")):
            response = client.post("/v1/auth/login", json={"username": username, "password": password})
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "default_credentials_rejected"
            assert "set-cookie" not in response.headers
    assert {name: password_hash(store, name) for name in ("admin", "owner")} == before
    # The same hashes still verify outside hosted mode: nothing was rewritten.
    assert store.verify_login("owner", "owner123")["role"] == "owner"


def hosted_app(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("ALLOW_DEMO_OWNER_LOGIN", raising=False)
    else:
        monkeypatch.setenv("ALLOW_DEMO_OWNER_LOGIN", flag)
    store = Store(tmp_path / "flag.sqlite3", seed=True)
    return create_app(tmp_path / "unused.sqlite3", hosted=True, public_demo=True, store_factory=lambda: store), store


def sessions(store):
    with store.transaction() as conn:
        return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def test_demo_owner_flag_off_rejects_owner_default(tmp_path, monkeypatch):
    app, store = hosted_app(tmp_path, monkeypatch, None)
    with anon(app) as client:
        response = client.post("/v1/auth/login", json={"username": "owner", "password": "owner123"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "default_credentials_rejected"
    assert "set-cookie" not in response.headers
    assert sessions(store) == 0


@pytest.mark.parametrize("value", ["", "0", "true", "yes", " 1", "2"])
def test_demo_owner_flag_values_other_than_1_are_off(tmp_path, monkeypatch, value):
    app, store = hosted_app(tmp_path, monkeypatch, value)
    with anon(app) as client:
        response = client.post("/v1/auth/login", json={"username": "owner", "password": "owner123"})
    assert response.status_code == 401 and "set-cookie" not in response.headers
    assert sessions(store) == 0


def test_demo_owner_flag_on_accepts_owner_via_verify_login(tmp_path, monkeypatch):
    app, store = hosted_app(tmp_path, monkeypatch, "1")
    before = password_hash(store, "owner")
    calls = []
    original = store.verify_login

    def spy(username, password):
        calls.append(username)
        return original(username, password)

    monkeypatch.setattr(store, "verify_login", spy)
    with anon(app) as client:
        response = client.post("/v1/auth/login", json={"username": "owner", "password": "owner123"})
        assert response.status_code == 200, response.text
        assert calls == ["owner"]
        cookie = response.headers["set-cookie"].lower()
        for attribute in ("restopilot_session=", "secure", "httponly", "samesite=lax"):
            assert attribute in cookie
        me = client.get("/v1/auth/me").json()["data"]
        assert (me["username"], me["role"], me["restaurant_id"]) == ("owner", "owner", "demo-resto")
        assert client.get("/v1/overview").json()["data"]["restaurant"]["id"] == "demo-resto"
        assert client.get("/v1/admin/restaurants").status_code == 403
    assert password_hash(store, "owner") == before


def test_demo_owner_flag_on_rejects_wrong_owner_password(tmp_path, monkeypatch):
    app, store = hosted_app(tmp_path, monkeypatch, "1")
    with anon(app) as client:
        response = client.post("/v1/auth/login", json={"username": "owner", "password": "not-the-password"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert "set-cookie" not in response.headers
    assert sessions(store) == 0


def test_demo_owner_flag_on_still_rejects_admin_default(tmp_path, monkeypatch):
    app, store = hosted_app(tmp_path, monkeypatch, "1")
    with anon(app) as client:
        for username in ("admin", "Admin", "OWNER"):
            password = "admin123" if username.lower() == "admin" else "owner123"
            response = client.post("/v1/auth/login", json={"username": username, "password": password})
            assert response.status_code == 401, username
            assert response.json()["error"]["code"] == "default_credentials_rejected"
            assert "set-cookie" not in response.headers
    assert sessions(store) == 0


def test_session_cookie_is_secure_httponly_lax(hosted):
    app, store = hosted
    set_password(store, "owner", OWNER_PASSWORD)
    with anon(app) as client:
        response = client.post("/v1/auth/login", json={"username": "owner", "password": OWNER_PASSWORD})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        for attribute in ("restopilot_session=", "secure", "httponly", "samesite=lax", "path=/"):
            assert attribute in cookie
        assert client.get("/v1/auth/me").json()["data"]["username"] == "owner"
        logout = client.post("/v1/auth/logout")
        assert "secure" in logout.headers["set-cookie"].lower()
        assert client.get("/v1/auth/me").status_code == 401


def test_unauthenticated_requests_get_401(hosted):
    app, _ = hosted
    with anon(app) as client:
        assert client.get("/v1/auth/me").status_code == 401
        assert client.get("/v1/overview").status_code == 401
        assert client.post("/v1/intake", headers=key(), json={"text": "Cabai 2 kg 100rb"}).status_code == 401
        assert client.get("/v1/admin/restaurants").status_code == 401
        assert client.get("/v1/overview").json()["error"]["code"] == "unauthorized"


def test_cross_tenant_access_is_denied(hosted):
    app, store = hosted
    set_password(store, "owner", OWNER_PASSWORD)
    set_password(store, "admin", ADMIN_PASSWORD)
    admin = logged_in(app, "admin", ADMIN_PASSWORD)
    created = admin.post("/v1/admin/restaurants", headers=key(), json={
        "name": "Warung Lain", "owner_username": "lainku", "owner_password": "rahasia456", "whatsapp_number": "628999000777",
    })
    assert created.status_code == 201, created.text
    other = logged_in(app, "lainku", "rahasia456")
    owner = logged_in(app, "owner", OWNER_PASSWORD)
    draft = other.post("/v1/intake", headers=key(), json={"text": "Beli telur 2 kg 60000"}).json()["data"]
    assert owner.get(f"/v1/drafts/{draft['id']}").status_code == 404
    assert owner.post(f"/v1/drafts/{draft['id']}/decision", headers=key(), json={
        "expected_version": draft["version"], "decision": "approve", "received": True}).status_code == 404
    assert all(row["id"] != draft["id"] for row in owner.get("/v1/overview").json()["data"]["drafts"])
    simulated = owner.post("/v1/whatsapp/simulate", headers=key(), json={"text": "ya", "sender": "628999000777"})
    assert simulated.status_code == 403
    # Owners never reach the admin surface.
    assert owner.get("/v1/admin/restaurants").status_code == 403


def test_owner_workflow_guards_and_idempotency(hosted):
    app, store = hosted
    set_password(store, "owner", OWNER_PASSWORD)
    set_password(store, "admin", ADMIN_PASSWORD)
    owner = logged_in(app, "owner", OWNER_PASSWORD)
    admin = logged_in(app, "admin", ADMIN_PASSWORD)

    def decide(client, draft, idem=None, version=None):
        return client.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": idem or uuid4().hex},
                           json={"expected_version": version or draft["version"], "decision": "approve", "received": True})

    # 422 clarification_required: an ambiguous draft cannot be approved.
    unclear = owner.post("/v1/intake", headers=key(), json={"text": "Telur 150rb"}).json()["data"]
    response = decide(owner, unclear)
    assert response.status_code == 422 and response.json()["error"]["code"] == "clarification_required"

    # 409 stale version.
    draft = owner.post("/v1/intake", headers=key(), json={"text": "Cabai 2 kg 100rb", "supplier": "Pasar Pagi"}).json()["data"]
    fields = ("ingredient_id", "raw_name", "quantity", "unit", "line_total_idr")
    payload = {"expected_version": draft["version"], "supplier": draft["supplier"],
               "items": [{field: item.get(field) for field in fields} for item in draft["items"]]}
    assert owner.patch(f"/v1/drafts/{draft['id']}", headers=key(), json=payload).status_code == 200
    stale = owner.patch(f"/v1/drafts/{draft['id']}", headers=key(), json=payload)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    assert decide(owner, draft).status_code == 409
    draft = owner.get(f"/v1/drafts/{draft['id']}").json()["data"]

    # 403 for the non-owner (platform admin) on mutating draft routes.
    assert admin.post("/v1/intake", headers=key(), json={"text": "Cabai 2 kg 100rb"}).status_code == 403
    forbidden = decide(admin, draft)
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "owner_required"

    # Idempotent replay: same key returns the same response and posts once.
    before = len(owner.get("/v1/overview").json()["data"]["purchases"])
    idem = uuid4().hex
    first = decide(owner, draft, idem)
    replay = decide(owner, draft, idem)
    assert first.status_code == 200 and replay.json() == first.json()
    assert len(owner.get("/v1/overview").json()["data"]["purchases"]) == before + 1
