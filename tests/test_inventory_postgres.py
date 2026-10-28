"""Real local PostgreSQL integration tests, never Supabase.

Opt in with TEST_LOCAL_PG_DSN='host=/private/tmp port=55439 dbname=postgres'
and run pytest tests/test_inventory_postgres.py. The connection must use a
Unix socket or loopback address. A disposable database is created and removed.

Local setup (Homebrew PostgreSQL 17; run initdb only once):
  /opt/homebrew/opt/postgresql@17/bin/initdb -D tmp/inventory-pg -A trust
  /opt/homebrew/opt/postgresql@17/bin/pg_ctl -D tmp/inventory-pg \
      -l tmp/inventory-pg/server.log -o "-h '' -k /private/tmp -p 55439" start
  TEST_LOCAL_PG_DSN='host=/private/tmp port=55439 dbname=postgres' \
      python -m pytest tests/test_inventory_postgres.py -q
  /opt/homebrew/opt/postgresql@17/bin/pg_ctl -D tmp/inventory-pg stop
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from service.db import DomainError
from service.pg_store import PostgresStore

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_LOCAL_PG_DSN"), reason="requires explicit local PostgreSQL DSN")


@pytest.fixture(scope="module")
def local_database():
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extensions import parse_dsn
    from psycopg2 import sql
    params = parse_dsn(os.environ["TEST_LOCAL_PG_DSN"])
    host = params.get("host", "")
    assert host in {"127.0.0.1", "::1"} or host.startswith("/"), "Explicit local host required; cloud connections prohibited"
    assert not params.get("service") and not params.get("hostaddr"), "No connection overrides allowed"
    name = "restopilot_inventory_test_" + uuid.uuid4().hex
    admin = psycopg2.connect(**params)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params["dbname"] = name

    @contextmanager
    def transaction():
        conn = psycopg2.connect(**params)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    try:
        with transaction() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE tenants (slug text PRIMARY KEY);
                INSERT INTO tenants VALUES ('alpha'),('beta');
                CREATE TABLE stock (slug text REFERENCES tenants, ingredient_id text, quantity text NOT NULL, PRIMARY KEY(slug,ingredient_id));
                CREATE TABLE drafts (id text PRIMARY KEY, slug text REFERENCES tenants, version integer, status text, body jsonb, created_at timestamptz);
                CREATE TABLE purchases (id text PRIMARY KEY, slug text REFERENCES tenants, draft_id text UNIQUE REFERENCES drafts, supplier text, created_at timestamptz, body jsonb);
                CREATE TABLE movements (id text PRIMARY KEY, slug text REFERENCES tenants, purchase_id text REFERENCES purchases, ingredient_id text, quantity text NOT NULL, created_at timestamptz);
                CREATE TABLE activity (id text PRIMARY KEY, slug text REFERENCES tenants, event text, message text, created_at timestamptz);
                CREATE TABLE idempotency (key text PRIMARY KEY, fingerprint text, response jsonb);
            """)
        with transaction() as conn, conn.cursor() as cur:
            cur.execute((Path(__file__).parents[1] / "supabase/migrations/0004_inventory_operations.sql").read_text())
        yield transaction
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def pg_inventory(local_database, monkeypatch):
    monkeypatch.setattr("service.pg_store.pg_transaction", local_database)
    with local_database() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE inventory_events, inventory_recipes, inventory_settings, stock, drafts, purchases, movements, activity, idempotency CASCADE")
        cur.execute("INSERT INTO stock VALUES ('alpha','beras','10'),('alpha','telur','10'),('beta','beras','20')")
    store = PostgresStore.__new__(PostgresStore)
    store._catalogs = {}
    store._overview_cache = {}
    return store


def usage(amount="1", ingredient="beras", **kwargs):
    return {"kind": "usage", "confirmed": True, "note": "Local integration test", "items": [{"ingredient_id": ingredient, "quantity": amount}], **kwargs}


def query(store, sql, params=()):
    with store.transaction() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def balance(store, tenant="alpha", ingredient="beras"):
    return query(store, "SELECT quantity::numeric FROM stock WHERE slug=%s AND ingredient_id=%s", (tenant, ingredient))[0][0]


def parallel(actions):
    barrier = Barrier(len(actions))
    def run(action):
        barrier.wait(timeout=10)
        return action()
    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        futures = [pool.submit(run, action) for action in actions]
        return [future.result(timeout=20) for future in futures]


def test_fractional_package_deduction_and_replay(pg_inventory):
    store = pg_inventory
    payload = usage("0.5")
    payload["items"][0]["pack_size"] = "2.5"
    event = store.record_inventory_event("fraction", payload, "alpha")
    assert store.record_inventory_event("fraction", payload, "alpha") == event
    assert balance(store) == Decimal("8.75")
    assert query(store, "SELECT quantity::numeric FROM movements") == [(Decimal("-1.25"),)]
    assert query(store, "SELECT count(*) FROM inventory_events") == [(1,)]


def test_concurrent_identical_requests_post_once(pg_inventory):
    store = pg_inventory
    results = parallel([lambda: store.record_inventory_event("same", usage("0.25"), "alpha") for _ in range(6)])
    assert len({event["id"] for event in results}) == 1
    assert balance(store) == Decimal("9.75")
    assert query(store, "SELECT count(*) FROM movements") == [(1,)]


def test_concurrent_distinct_requests_do_not_lose_stock(pg_inventory):
    store = pg_inventory
    results = parallel([lambda i=i: store.record_inventory_event(str(i), usage("0.25"), "alpha") for i in range(8)])
    assert len({event["id"] for event in results}) == 8
    assert balance(store) == Decimal("8")
    assert query(store, "SELECT sum(quantity::numeric),count(*) FROM movements") == [(Decimal("-2"), 8)]


def test_count_rejects_stale_balance_and_rolls_back(pg_inventory):
    store = pg_inventory
    store.record_inventory_event("usage", usage(), "alpha")
    payload = usage("7", kind="count")
    payload["items"][0]["expected_quantity"] = "10"
    with pytest.raises(DomainError) as error:
        store.record_inventory_event("count", payload, "alpha")
    assert error.value.code == "stock_conflict"
    assert balance(store) == 9
    payload["items"][0]["expected_quantity"] = "9"
    store.record_inventory_event("count", payload, "alpha")
    assert balance(store) == 7


def test_partial_multiline_event_rolls_back_every_write(pg_inventory):
    store = pg_inventory
    payload = usage()
    payload["items"].append({"ingredient_id": "telur", "quantity": "11"})
    with pytest.raises(DomainError) as error:
        store.record_inventory_event("rollback", payload, "alpha")
    assert error.value.code == "insufficient_stock"
    assert balance(store) == 10
    for table in ("movements", "inventory_events", "activity", "idempotency"):
        assert query(store, f"SELECT count(*) FROM {table}") == [(0,)]


@pytest.mark.parametrize("kind,amount,expected_error,remaining", [
    ("usage", "6", "insufficient_stock", Decimal("4")),
    ("count", "7", "stock_conflict", Decimal("7")),
])
def test_competing_stock_changes_recheck_locked_balance(pg_inventory, kind, amount, expected_error, remaining):
    store = pg_inventory
    payload = usage(amount, kind=kind)
    if kind == "count":
        payload["items"][0]["expected_quantity"] = "10"
    def record(key):
        try:
            store.record_inventory_event(key, payload, "alpha")
            return "saved"
        except DomainError as error:
            return error.code
    results = parallel([lambda: record("first"), lambda: record("second")])
    assert sorted(results) == sorted(["saved", expected_error])
    assert balance(store) == remaining
    for table in ("movements", "inventory_events", "activity", "idempotency"):
        assert query(store, f"SELECT count(*) FROM {table}") == [(1,)]


def test_recipe_sale_is_atomic_and_tenant_scoped(pg_inventory):
    store = pg_inventory
    recipe = store.save_inventory_recipe("recipe", {"name": "Rice portion", "items": [{"ingredient_id": "beras", "quantity": "0.125"}]}, "alpha")
    payload = {"kind": "sale", "confirmed": True, "note": "Lunch", "recipe_id": recipe["id"], "expected_recipe_version": recipe["version"], "portions": "3", "revenue_idr": 30000}
    event = store.record_inventory_event("sale", payload, "alpha")
    assert event["recipe"] == recipe
    assert balance(store) == Decimal("9.625")
    with pytest.raises(DomainError) as error:
        store.record_inventory_event("sale", payload, "beta")
    assert error.value.code == "not_found"
    store.record_inventory_event("sale", usage("2"), "beta")
    assert balance(store, "beta") == 18
    assert len(store.inventory_workspace("alpha")["events"]) == 1
    assert len(store.inventory_workspace("beta")["events"]) == 1


def test_receipt_and_usage_concurrently_preserve_both_deltas(pg_inventory):
    store = pg_inventory
    draft = store.intake("intake", {"text": "rice", "supplier": "Test"}, extracted={"items": [{"ingredient_id": "beras", "raw_name": "Beras", "quantity": "2", "unit": "kg", "line_total_idr": 20000}], "warnings": []}, restaurant_id="alpha")
    parallel([
        lambda: store.decision(draft["id"], "approve", {"expected_version": draft["version"], "decision": "approve", "received": True}, "alpha"),
        lambda: store.record_inventory_event("consume", usage("0.5"), "alpha"),
    ])
    assert balance(store) == Decimal("11.5")
    assert query(store, "SELECT sum(quantity::numeric),count(*) FROM movements") == [(Decimal("1.5"), 2)]
    assert query(store, "SELECT count(*) FROM purchases") == [(1,)]


def test_migration_reapply_preserves_ledger(pg_inventory):
    store = pg_inventory
    event = store.record_inventory_event("before_migration", usage("0.5"), "alpha")
    migration = (Path(__file__).parents[1] / "supabase/migrations/0004_inventory_operations.sql").read_text()
    for _ in range(2):
        with store.transaction() as conn, conn.cursor() as cur:
            cur.execute(migration)
    assert balance(store) == Decimal("9.5")
    assert store.record_inventory_event("before_migration", usage("0.5"), "alpha") == event
    assert query(store, "SELECT count(*) FROM movements") == [(1,)]


def test_missing_movement_column_reports_migration_required(pg_inventory):
    store = pg_inventory
    with store.transaction() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE movements DROP COLUMN inventory_event_id")
    try:
        with pytest.raises(DomainError) as error:
            store.record_inventory_event("missing_schema", usage(), "alpha")
        assert error.value.code == "migration_required"
        assert error.value.status == 503
        assert balance(store) == 10
        assert query(store, "SELECT count(*) FROM idempotency") == [(0,)]
    finally:
        with store.transaction() as conn, conn.cursor() as cur:
            cur.execute((Path(__file__).parents[1] / "supabase/migrations/0004_inventory_operations.sql").read_text())
