"""Supplier-photo retries share one tenant-scoped purchase draft and posting."""

import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import SENDER_NUMBER
from service.db import Store, now
from service.operation_extraction import parse_operations_text
from test_meta_whatsapp import (
    FakeMetaClient, FakeResponse, FakeVisionClient, payload, photo_app,
    vision_completion, webhook,
)


TENANT = "demo-resto"
OTHER_SENDER = "628199999991"
DIGEST = hashlib.sha256(b"one supplier invoice").hexdigest()
EXTRACTED = {"supplier": "Supplier", "items": [
    {"ingredient_id": "beras", "raw_name": "Beras", "quantity": "3", "unit": "kg", "line_total_idr": 30000},
], "warnings": []}


def message(store, *, sender=SENDER_NUMBER, digest=DIGEST, event=None, key=None, text="", photo=True):
    value = {"sender": sender, "text": text, "event_id": event or "wamid." + uuid4().hex, "num_media": 1 if photo else 0}
    if photo:
        value.update(extracted=EXTRACTED, operation_source={"digest": digest, "media_kind": "image", "media_id": "media." + uuid4().hex})
    return store.whatsapp_ingest(key or uuid4().hex, value)


def link(store, sender, tenant=TENANT):
    with store.transaction() as conn:
        conn.execute("INSERT INTO whatsapp_links(id,restaurant_id,phone,label,created_at) VALUES (?,?,?,?,?)", (uuid4().hex, tenant, sender, "Local test", now()))


def facts(store):
    with store.transaction() as conn:
        return {
            "drafts": conn.execute("SELECT count(*) FROM drafts").fetchone()[0],
            "purchases": conn.execute("SELECT count(*) FROM purchases").fetchone()[0],
            "sources": conn.execute("SELECT count(*) FROM whatsapp_purchase_sources").fetchone()[0],
            "stock": conn.execute("SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id='beras'", (TENANT,)).fetchone()[0],
        }


def test_same_photo_pending_then_recorded_new_events_cross_sender_are_duplicates(tmp_path):
    store = Store(tmp_path / "photos.sqlite3", seed=False)
    link(store, OTHER_SENDER)
    initial = facts(store)
    first = message(store)
    assert first["result"]["action"] == "draft_created"
    pending = facts(store)
    assert pending["drafts"] == initial["drafts"] + 1
    assert pending["sources"] == 1
    for sender in (SENDER_NUMBER, OTHER_SENDER):
        replay = message(store, sender=sender)
        assert replay["result"] == {"action": "purchase_source_duplicate", "source_status": "pending_approval"}
        if sender == OTHER_SENDER:
            assert replay["thread"]["active_draft_id"] is None
            assert "draft" not in replay
        assert facts(store) == pending
    # A different sender cannot approve the original sender's pending draft.
    assert message(store, sender=OTHER_SENDER, text="YA", photo=False)["result"]["action"] == "no_active_draft"
    approved = message(store, text="YA", photo=False)
    assert approved["result"]["action"] == "approved"
    posted = facts(store)
    assert posted["purchases"] == initial["purchases"] + 1
    assert posted["stock"] != initial["stock"]
    for sender in (SENDER_NUMBER, OTHER_SENDER):
        replay = message(store, sender=sender)
        assert replay["result"]["source_status"] == "recorded"
        assert facts(store) == posted


def test_rejected_receipt_requires_explicit_correction_not_a_new_draft(tmp_path):
    store = Store(tmp_path / "rejected.sqlite3", seed=False)
    message(store)
    assert message(store, text="BATAL", photo=False)["result"]["action"] == "rejected"
    before = facts(store)
    replay = message(store)
    assert replay["result"] == {"action": "purchase_source_duplicate", "source_status": "rejected"}
    assert "koreksi" in replay["messages"][-1]["body"]
    assert facts(store) == before


def test_identical_bytes_in_another_restaurant_create_a_separate_draft(tmp_path):
    store = Store(tmp_path / "tenants.sqlite3", seed=False)
    other_tenant, other_phone = "other-resto", "628199999992"
    with store.transaction() as conn:
        conn.execute("INSERT INTO restaurants(id,name,address,created_at) VALUES (?,?,NULL,?)", (other_tenant, "Other restaurant", now()))
    link(store, other_phone, other_tenant)
    first = message(store)
    second = message(store, sender=other_phone)
    assert first["draft"]["id"] != second["draft"]["id"]
    assert second["result"]["action"] == "draft_created"
    with store.transaction() as conn:
        rows = conn.execute("SELECT restaurant_id,digest FROM whatsapp_purchase_sources ORDER BY restaurant_id").fetchall()
    assert {(row[0], row[1]) for row in rows} == {(TENANT, DIGEST), (other_tenant, DIGEST)}


def test_same_provider_event_retry_returns_original_without_another_source(tmp_path):
    store = Store(tmp_path / "event.sqlite3", seed=False)
    event = "wamid.same-source-event"
    first = message(store, event=event, key="first-event-key")
    # A request-key replay recovers its original committed response even when
    # extraction is retried; no second source or draft is inserted.
    replay = message(store, event=event, key="first-event-key")
    assert replay == first
    assert facts(store)["sources"] == 1
    assert facts(store)["drafts"] == 1


def test_missing_source_migration_blocks_only_photo_draft_creation(tmp_path):
    store = Store(tmp_path / "missing.sqlite3", seed=False)
    with store.transaction() as conn:
        conn.execute("DROP TABLE whatsapp_purchase_sources")
    response = message(store)
    assert response["result"]["action"] == "purchase_source_migration_required"
    assert "draft" not in response
    response = message(store, text="beli beras 3 kg 30rb", photo=False)
    assert response["result"]["action"] == "draft_created"


def test_provider_failure_does_not_register_digest_and_retry_can_be_reviewed(tmp_path):
    provider, vision = FakeMetaClient(), FakeVisionClient()
    image = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    provider.responses.extend([
        FakeResponse(body={"url": "https://lookaside.example/media/failure", "mime_type": "image/jpeg"}),
        FakeResponse(content=image),
    ])
    vision.responses.append(FakeResponse(status_code=500))
    app = photo_app(tmp_path, provider, vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload(message_type="image", event_id="wamid.source-failure")).status_code == 200
        with app.state.store.transaction() as conn:
            assert conn.execute("SELECT count(*) FROM whatsapp_purchase_sources").fetchone()[0] == 0
        assert webhook(client, payload("BATAL", event_id="wamid.cancel-unreadable")).status_code == 200
        provider.responses.extend([
            FakeResponse(body={"url": "https://lookaside.example/media/retry", "mime_type": "image/jpeg"}),
            FakeResponse(content=image),
        ])
        vision.responses.extend([
            vision_completion({"document_type": "purchase_invoice", "lines": [], "unresolved": []}),
            vision_completion({"supplier": "Supplier", "items": EXTRACTED["items"], "unreadable": []}),
        ])
        assert webhook(client, payload(message_type="image", event_id="wamid.source-success")).status_code == 200
        with app.state.store.transaction() as conn:
            row = conn.execute("SELECT digest FROM whatsapp_purchase_sources").fetchone()
            assert row[0] == hashlib.sha256(image).hexdigest()


def sale_photo(store, sender=SENDER_NUMBER):
    return store.whatsapp_ingest(uuid4().hex, {
        "sender": sender, "text": "", "event_id": "wamid." + uuid4().hex, "num_media": 1,
        "operation_source": {"digest": DIGEST, "media_kind": "image", "media_id": uuid4().hex},
        "operation_candidate": parse_operations_text("Terjual 2 nasi goreng hari ini"),
    })


def test_sales_photo_cannot_be_reclassified_into_a_new_supplier_purchase(tmp_path):
    store = Store(tmp_path / "sale-to-purchase.sqlite3", seed=False)
    with store.transaction() as conn:
        conn.execute("UPDATE inventory SET quantity='10' WHERE restaurant_id=? AND ingredient_id='beras'", (TENANT,))
    store.save_inventory_recipe(uuid4().hex, {"name": "Nasi goreng", "items": [{"ingredient_id": "beras", "quantity": "0.1"}]}, TENANT)
    review = sale_photo(store)
    operation = review["result"]["operation"]
    assert operation["status"] == "review"
    confirmed = message(store, text=f"YA {operation['id'][:8]} V{operation['version']}", photo=False)
    assert confirmed["result"]["action"] == "operation_posted"
    before = facts(store)
    duplicate = message(store)
    assert duplicate["result"]["action"] == "operation_duplicate"
    assert "draft" not in duplicate
    assert facts(store) == before
    assert before["sources"] == 0 and before["purchases"] == 0


@pytest.mark.parametrize("approve_first", [False, True])
def test_supplier_photo_cannot_be_reclassified_as_a_sale(tmp_path, approve_first):
    store = Store(tmp_path / f"purchase-to-sale-{approve_first}.sqlite3", seed=False)
    link(store, OTHER_SENDER)
    message(store)
    if approve_first:
        assert message(store, text="YA", photo=False)["result"]["action"] == "approved"
    before = facts(store)
    duplicate = sale_photo(store, OTHER_SENDER)
    assert duplicate["result"]["action"] == "purchase_source_duplicate"
    assert duplicate["result"]["source_status"] == ("recorded" if approve_first else "pending_approval")
    assert "draft" not in duplicate and duplicate["thread"]["active_draft_id"] is None
    assert facts(store) == before
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM inventory_events").fetchone()[0] == 0
