"""Real disposable local PostgreSQL tests; never connects to Supabase.

Use TEST_LOCAL_PG_DSN as documented in test_inventory_postgres.py. Its imported
fixture rejects cloud hosts and creates/drops an isolated test database.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

from service.db import DomainError, hash_password
from service.pg_store import PostgresStore
from test_inventory_postgres import local_database
from test_phone_auth import PHONE, PASSWORD

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_LOCAL_PG_DSN"), reason="requires explicit local PostgreSQL DSN")
ADMIN = "00000000-0000-0000-0000-000000000001"
OWNER = "00000000-0000-0000-0000-000000000002"
BETA = "00000000-0000-0000-0000-000000000003"


@pytest.fixture(scope="module")
def auth_database(local_database):
    with local_database() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE restaurants (id uuid PRIMARY KEY,name text,phone_number text UNIQUE,address text,created_at timestamptz);
            INSERT INTO restaurants (id,name) VALUES ('10000000-0000-0000-0000-000000000001','Alpha Kitchen'),('10000000-0000-0000-0000-000000000002','Beta Kitchen');
            ALTER TABLE tenants ADD COLUMN restaurant_id uuid REFERENCES restaurants(id);
            UPDATE tenants SET restaurant_id='10000000-0000-0000-0000-000000000001' WHERE slug='alpha';
            UPDATE tenants SET restaurant_id='10000000-0000-0000-0000-000000000002' WHERE slug='beta';
            CREATE TABLE app_users (id uuid PRIMARY KEY,username text UNIQUE,password_hash text,display_name text,role text,slug text REFERENCES tenants(slug));
            CREATE TABLE app_sessions (token text PRIMARY KEY,user_id uuid REFERENCES app_users(id),created_at timestamptz,expires_at timestamptz);
        """)
        cur.execute((Path(__file__).parents[1] / "supabase/migrations/0006_phone_login.sql").read_text())
    return local_database


@pytest.fixture
def store(auth_database, monkeypatch):
    monkeypatch.setattr("service.pg_store.pg_transaction", auth_database)
    with auth_database() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE login_phone_identities,login_phone_limits,app_sessions,app_users,activity CASCADE")
        cur.execute("DELETE FROM tenants WHERE slug NOT IN ('alpha','beta')")
        cur.execute("DELETE FROM restaurants WHERE phone_number LIKE 'web+%'")
        for uid, username, role, slug in [(ADMIN, "admin", "admin", None), (OWNER, "alphaowner", "owner", "alpha"), (BETA, "betaowner", "owner", "beta")]:
            cur.execute("INSERT INTO app_users VALUES (%s,%s,%s,%s,%s,%s)", (uid, username, hash_password(PASSWORD), username, role, slug))
    return PostgresStore.__new__(PostgresStore)


def scalar(store, sql):
    with store.transaction() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def register(store, phone=PHONE):
    return store.register_phone_account(phone, PASSWORD, "New Kitchen", "Owner", "127.0.0.1")


def test_pg_registration_creates_only_new_isolated_tenant_and_session(store):
    user, token = register(store)
    assert user["role"] == "owner" and user["restaurant_id"] not in {"alpha", "beta"}
    assert store.session_user(token) == user
    assert scalar(store, "SELECT COUNT(*) FROM tenants") == 3
    assert scalar(store, "SELECT COUNT(*) FROM app_users") == 4
    assert scalar(store, "SELECT COUNT(*) FROM app_sessions") == 1
    assert not scalar(store, "SELECT verified_at IS NOT NULL FROM login_phone_identities")
    assert scalar(store, "SELECT COUNT(*) FROM stock") == 0


def test_pg_existing_owner_connect_and_phone_login_preserve_identity(store):
    user, token = store.connect_existing_phone("alphaowner", PASSWORD, PHONE, "local")
    assert user["id"] == OWNER and user["restaurant_id"] == "alpha"
    assert store.session_user(token) == user
    found, _ = store.login_phone_password("08123456789", PASSWORD, "local")
    assert found == user
    assert scalar(store, "SELECT COUNT(*) FROM tenants") == 2
    assert scalar(store, "SELECT COUNT(*) FROM app_users") == 3


def test_pg_wrong_password_attempts_commit_and_eventually_throttle(store):
    store.enroll_login_phone("alpha", "alphaowner", PHONE, ADMIN)
    for _ in range(20):
        with pytest.raises(DomainError) as exc:
            store.login_phone_password(PHONE, "wrong", "local")
        assert exc.value.status == 401
    with pytest.raises(DomainError) as exc:
        store.login_phone_password(PHONE, PASSWORD, "local")
    assert exc.value.status == 429
    assert scalar(store, "SELECT COUNT(*) FROM app_sessions") == 0


def test_pg_parallel_registration_creates_one_account(store):
    barrier = Barrier(2)

    def attempt(_):
        barrier.wait()
        try:
            register(store)
            return 201
        except DomainError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [201, 409]
    assert scalar(store, "SELECT COUNT(*) FROM app_sessions") == 1
    assert scalar(store, "SELECT COUNT(*) FROM tenants") == 3
    assert scalar(store, "SELECT COUNT(*) FROM login_phone_identities") == 1


def test_pg_registration_and_existing_connect_never_merge_accounts(store):
    user, _ = register(store)
    with pytest.raises(DomainError) as exc:
        store.connect_existing_phone("alphaowner", PASSWORD, PHONE, "local")
    assert exc.value.status == 409
    found, _ = store.login_phone_password(PHONE, PASSWORD, "local")
    assert found == user and found["id"] != OWNER
    assert scalar(store, f"SELECT slug FROM app_users WHERE id='{OWNER}'") == "alpha"


def test_pg_reassignment_requires_review_and_invalidates_sessions(store):
    store.enroll_login_phone("alpha", "alphaowner", PHONE, ADMIN)
    user, token = store.login_phone_password(PHONE, PASSWORD, "local")
    with pytest.raises(DomainError) as exc:
        store.enroll_login_phone("beta", "betaowner", PHONE, ADMIN)
    assert exc.value.status == 409
    store.enroll_login_phone("alpha", "alphaowner", None, ADMIN)
    assert store.session_user(token) is None
    store.enroll_login_phone("beta", "betaowner", PHONE, ADMIN)
    found, _ = store.login_phone_password(PHONE, PASSWORD, "local")
    assert found["id"] == BETA and found["restaurant_id"] == "beta"


def test_pg_concurrent_assignments_cannot_merge_owners(store):
    barrier = Barrier(2)

    def assign(args):
        barrier.wait()
        try:
            store.enroll_login_phone(*args, PHONE, ADMIN)
            return 200
        except DomainError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(assign, [("alpha", "alphaowner"), ("beta", "betaowner")])) == [200, 409]
    assert scalar(store, "SELECT COUNT(*) FROM login_phone_identities") == 1


def test_pg_failed_session_insert_rolls_back_whole_signup(store):
    with store.transaction() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE app_sessions ADD CONSTRAINT fail_test CHECK (false)")
    try:
        with pytest.raises(Exception):
            register(store)
        assert scalar(store, "SELECT COUNT(*) FROM tenants") == 2
        assert scalar(store, "SELECT COUNT(*) FROM app_users") == 3
        assert scalar(store, "SELECT COUNT(*) FROM login_phone_identities") == 0
        assert scalar(store, "SELECT COUNT(*) FROM activity") == 0
    finally:
        with store.transaction() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE app_sessions DROP CONSTRAINT fail_test")


def test_pg_password_reset_cannot_leave_a_racing_old_password_session(store, monkeypatch):
    store.enroll_login_phone("alpha", "alphaowner", PHONE, ADMIN)
    acquired, release, reset_started = Event(), Event(), Event()
    original = store._phone_session

    def paused_session(conn, user_id):
        acquired.set()
        assert release.wait(5)
        return original(conn, user_id)

    def reset():
        reset_started.set()
        with store.transaction() as conn, conn.cursor() as cur:
            cur.execute("UPDATE app_users SET password_hash=%s WHERE id=%s", (hash_password("new-password-9"), OWNER))
            cur.execute("DELETE FROM app_sessions WHERE user_id=%s", (OWNER,))

    monkeypatch.setattr(store, "_phone_session", paused_session)
    with ThreadPoolExecutor(max_workers=2) as pool:
        login = pool.submit(store.login_phone_password, PHONE, PASSWORD, "local")
        try:
            assert acquired.wait(5)
            resetting = pool.submit(reset)
            assert reset_started.wait(5)
        finally:
            release.set()
        _, token = login.result(timeout=10)
        resetting.result(timeout=10)
    assert store.session_user(token) is None
    with pytest.raises(DomainError):
        store.login_phone_password(PHONE, PASSWORD, "local")


def test_pg_missing_optional_schema_fails_closed_without_startup_migration(store):
    with store.transaction() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE login_phone_limits RENAME TO login_phone_limits_hidden")
    try:
        with pytest.raises(DomainError) as exc:
            register(store)
        assert exc.value.code == "migration_required" and exc.value.status == 503
        assert scalar(store, "SELECT to_regclass('public.login_phone_limits')") is None
    finally:
        with store.transaction() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE login_phone_limits_hidden RENAME TO login_phone_limits")
