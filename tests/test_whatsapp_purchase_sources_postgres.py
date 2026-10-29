"""Supplier-photo races in an explicitly local disposable PostgreSQL database."""

import hashlib
import os
from uuid import uuid4

import pytest

from test_inventory_postgres import local_database, pg_inventory, balance, parallel, query
from test_whatsapp_operations_postgres import ALPHA, ALPHA_OTHER, BETA, operations_store


pytestmark = pytest.mark.skipif(not os.environ.get("TEST_LOCAL_PG_DSN"), reason="requires explicit local PostgreSQL DSN")


def photo(store, sender=ALPHA):
    return store.whatsapp_ingest(uuid4().hex, {
        "sender": sender, "text": "", "event_id": uuid4().hex, "num_media": 1,
        "operation_source": {"digest": hashlib.sha256(b"same supplier photo").hexdigest(), "media_kind": "image", "media_id": uuid4().hex},
        "extracted": {"supplier": "Supplier", "items": [{"ingredient_id": "beras", "raw_name": "Beras", "quantity": "3", "unit": "kg", "line_total_idr": 30000}], "warnings": []},
    }, restaurant_id="beta" if sender == BETA else "alpha")


def text(store, message, sender=ALPHA):
    return store.whatsapp_ingest(uuid4().hex, {"sender": sender, "text": message, "event_id": uuid4().hex}, restaurant_id="alpha")


def test_concurrent_same_supplier_photo_creates_one_draft_across_senders(operations_store):
    store = operations_store
    responses = parallel([lambda: photo(store, ALPHA), lambda: photo(store, ALPHA_OTHER)])
    assert sorted(r["result"]["action"] for r in responses) == ["draft_created", "purchase_source_duplicate"]
    duplicate = next(r for r in responses if r["result"]["action"] == "purchase_source_duplicate")
    assert "draft" not in duplicate
    assert duplicate["thread"]["active_draft_id"] is None
    assert query(store, "SELECT count(*) FROM drafts") == [(1,)]
    assert query(store, "SELECT count(*) FROM whatsapp_purchase_sources") == [(1,)]
    assert query(store, "SELECT count(*) FROM purchases") == [(0,)]
    assert balance(store) == 10


def test_recorded_photo_replay_does_not_add_stock_and_other_tenant_is_independent(operations_store):
    store = operations_store
    assert photo(store)["result"]["action"] == "draft_created"
    assert text(store, "YA")["result"]["action"] == "approved"
    assert balance(store) == 13
    duplicates = parallel([lambda: photo(store, ALPHA), lambda: photo(store, ALPHA_OTHER)])
    assert all(r["result"] == {"action": "purchase_source_duplicate", "source_status": "recorded"} for r in duplicates)
    assert query(store, "SELECT count(*) FROM purchases") == [(1,)]
    assert balance(store) == 13
    assert photo(store, BETA)["result"]["action"] == "draft_created"
    assert query(store, "SELECT count(*) FROM whatsapp_purchase_sources") == [(2,)]
