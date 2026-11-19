"""Hosted-store supplier coverage.

supplier_add/supplier_update return their detail through the idempotent
JSON encoder; the pg detail read returns created_at as a timestamptz
datetime, which made every supplier POST/PATCH on the hosted store fail
with 500 (TypeError: datetime is not JSON serializable). These tests run
the real PostgresStore against a disposable local database with the
0008 schema applied.
"""
import os
import uuid
from uuid import uuid4
from pathlib import Path

import pytest

from service.pg_store import PostgresStore
from test_inventory_postgres import local_database

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_LOCAL_PG_DSN"), reason="requires explicit local PostgreSQL DSN")


@pytest.fixture
def supplier_store(local_database, monkeypatch):
    with local_database() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS whatsapp_links (id text PRIMARY KEY, phone text NOT NULL, platform text NOT NULL DEFAULT 'whatsapp', label text, created_at timestamptz DEFAULT now(), restaurant_id text, UNIQUE (phone, platform))")
        cur.execute((Path(__file__).parents[1] / "supabase/migrations/0008_suppliers.sql").read_text())
        cur.execute("INSERT INTO activity (id, slug, event) VALUES (%s,'alpha','seed')", (uuid.uuid4().hex,))
    monkeypatch.setattr("service.pg_store.pg_transaction", local_database)
    store = PostgresStore.__new__(PostgresStore)
    store._catalogs = {}
    store._overview_cache = {}
    return store


def test_supplier_add_returns_json_safe_detail(supplier_store):
    detail = supplier_store.supplier_add(uuid4().hex, {"name": "Pasar Induk"}, "alpha")
    assert detail["name"] == "Pasar Induk"
    assert isinstance(detail["created_at"], str), "created_at must be an ISO string, not a datetime"
    assert [s["name"] for s in supplier_store.suppliers_list("alpha")] == ["Pasar Induk"]


def test_supplier_add_and_update_with_contact(supplier_store):
    key = uuid4().hex
    detail = supplier_store.supplier_add(key, {"name": "Pak Budi", "contact_phone": "628111000777",
                                               "contact_platform": "whatsapp", "notes": "ayam"}, "alpha")
    assert detail["contact_phone"] == "628111000777"
    updated = supplier_store.supplier_update("alpha", detail["id"], uuid4().hex, {"contact_phone": "628111000888"})
    assert updated["contact_phone"] == "628111000888"
    assert isinstance(updated["created_at"], str)
    from service.db import DomainError
    with pytest.raises(DomainError) as exc:
        supplier_store.supplier_add(uuid4().hex, {"name": "pak budi"}, "alpha")
    assert exc.value.code == "supplier_exists"


def test_supplier_duplicate_name_rejects(supplier_store):
    from service.db import DomainError
    name = "Pak Budi " + uuid4().hex[:6]
    supplier_store.supplier_add(uuid4().hex, {"name": name}, "alpha")
    with pytest.raises(DomainError) as exc:
        supplier_store.supplier_add(uuid4().hex, {"name": name}, "alpha")
    assert exc.value.code == "supplier_exists"
