"""Persistent provider-neutral WhatsApp simulator tests."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service.api import create_app


SENDER = "demo-owner"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "whatsapp.sqlite3")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        yield value


def simulate(client, text, *, sender=SENDER, key=None, event=None, role=None):
    headers = {"Idempotency-Key": key or uuid4().hex}
    if role:
        headers["X-Demo-Role"] = role
    payload = {"sender": sender, "text": text}
    if event is not None:
        payload["event_id"] = event
    return client.post("/v1/whatsapp/simulate", headers=headers, json=payload)


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def overview(client):
    return client.get("/v1/overview").json()["data"]


def test_simulator_purchase_ya_records_once_and_persists_messages(client):
    before = overview(client)
    first = data(simulate(client, "beli cabai 3 kg 150rb"))
    assert first["thread"]["transport"] == "whatsapp_simulator"
    assert first["thread"]["sender"] == SENDER
    assert first["draft"]["status"] == "pending_approval"
    assert first["thread"]["active_draft_id"] == first["draft"]["id"]
    assert [message["direction"] for message in first["messages"]] == ["inbound", "outbound"]
    assert [message["status"] for message in first["messages"]] == ["received", "simulated"]
    assert "YA" in first["messages"][-1]["body"] and "diterima" in first["messages"][-1]["body"]
    assert overview(client)["purchases"] == before["purchases"]
    approved = data(simulate(client, "YA"))
    assert approved["result"]["action"] == "approved"
    assert approved["draft"]["purchase_id"] == approved["result"]["purchase_id"]
    assert approved["thread"]["active_draft_id"] is None
    assert len(approved["messages"]) == 4
    assert all(message["draft_id"] == first["draft"]["id"] for message in approved["messages"])
    state = overview(client)
    assert len(state["purchases"]) == len(before["purchases"]) + 1
    assert next(row["quantity"] for row in state["inventory"] if row["ingredient_id"] == "cabai") == "6"
    assert state["purchases"][0]["payment_status"] == "unknown"
    assert data(simulate(client, "ya"))["result"]["action"] == "no_active_draft"
    saved = data(client.get("/v1/whatsapp/messages", params={"sender": SENDER}))
    assert len(saved["messages"]) == 6 and "draft" not in saved
    assert len(overview(client)["purchases"]) == 3


def test_simulator_deduplicates_keys_and_events(client):
    key, event = uuid4().hex, uuid4().hex
    first = data(simulate(client, "Telur 3 kg 150rb", key=key, event=event))
    assert data(simulate(client, "Telur 3 kg 150rb", key=key, event=event)) == first
    assert data(simulate(client, "Telur 3 kg 150rb", event=event)) == first
    assert simulate(client, "Beras 2 kg 50rb", event=event).status_code == 409
    assert simulate(client, "Telur 3 kg 150rb", sender="other", event=event).status_code == 409
    key, event = uuid4().hex, uuid4().hex
    approved = data(simulate(client, "ya", key=key, event=event))
    assert data(simulate(client, "ya", key=key, event=event)) == approved
    assert data(simulate(client, "ya", event=event)) == approved
    assert len(overview(client)["purchases"]) == 3
    assert len(data(client.get("/v1/whatsapp/messages", params={"sender": SENDER}))["messages"]) == 4


def test_active_draft_cannot_be_replaced_and_wrong_sender_cannot_approve(client):
    first = data(simulate(client, "Cabai 3 kg 150rb"))
    collision = data(simulate(client, "Beras 10 kg 200rb"))
    assert collision["result"]["action"] == "active_draft_exists"
    assert collision["draft"]["id"] == first["draft"]["id"]
    assert collision["draft"]["source_text"] == "Cabai 3 kg 150rb"
    assert data(simulate(client, "ya", sender="other"))["result"]["action"] == "no_active_draft"
    assert len(overview(client)["purchases"]) == 2
    assert data(simulate(client, "batal"))["draft"]["status"] == "rejected"
    assert len(overview(client)["purchases"]) == 2
    assert data(simulate(client, "Beras 10 kg 200rb"))["draft"]["id"] != first["draft"]["id"]


def test_unresolved_draft_requires_dashboard_correction_and_fresh_confirmation(client):
    created = data(simulate(client, "beli beras"))
    draft = created["draft"]
    assert draft["status"] == "needs_clarification"
    assert data(simulate(client, "ya"))["result"]["action"] == "review_required"
    payload = {"expected_version": draft["version"], "supplier": "Pasar Pagi", "items": [
        {"ingredient_id": "beras", "raw_name": "beras", "quantity": "5", "unit": "kg", "line_total_idr": 75000}
    ]}
    edited = data(client.patch(f"/v1/drafts/{draft['id']}", headers={"Idempotency-Key": uuid4().hex}, json=payload))
    assert edited["status"] == "pending_approval"
    changed = data(simulate(client, "ya"))
    assert changed["result"]["action"] == "review_required"
    assert "berubah" in changed["messages"][-1]["body"]
    assert len(overview(client)["purchases"]) == 2
    assert data(simulate(client, "ya"))["result"]["action"] == "approved"


def test_dashboard_finalization_clears_correlation_without_second_post(client):
    draft = data(simulate(client, "Cabai 3 kg 150rb"))["draft"]
    approved = client.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": uuid4().hex}, json={"expected_version": draft["version"], "decision": "approve", "received": True})
    assert approved.status_code == 200
    assert data(client.get("/v1/whatsapp/messages", params={"sender": SENDER}))["thread"]["active_draft_id"] is None
    assert data(simulate(client, "ya"))["result"]["action"] == "no_active_draft"
    assert len(overview(client)["purchases"]) == 3


def test_concurrent_ya_and_restart_preserve_exactly_one_purchase(app, client, tmp_path):
    data(simulate(client, "Cabai 3 kg 150rb"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: data(simulate(client, "ya")), range(2)))
    assert sorted(result["result"]["action"] for result in results) == ["approved", "no_active_draft"]
    assert len(overview(client)["purchases"]) == 3
    second_app = create_app(tmp_path / "whatsapp.sqlite3")
    with TestClient(second_app, base_url="http://127.0.0.1") as restarted:
        saved = data(restarted.get("/v1/whatsapp/messages", params={"sender": SENDER}))
        assert len(saved["messages"]) == 6
        assert saved["thread"]["active_draft_id"] is None
        assert len(overview(restarted)["purchases"]) == 3


def test_message_failure_rolls_back_inbound_ledger_stock_and_deduplication(app, client):
    data(simulate(client, "Cabai 3 kg 150rb"))
    before = overview(client)
    key, event = uuid4().hex, uuid4().hex
    with app.state.store.transaction() as conn:
        conn.execute("CREATE TRIGGER reject_chat_reply BEFORE INSERT ON whatsapp_messages WHEN NEW.direction='outbound' BEGIN SELECT RAISE(ABORT, 'injected reply failure'); END")
    failed = simulate(client, "ya", key=key, event=event)
    assert failed.status_code == 500
    assert overview(client) == before
    saved = data(client.get("/v1/whatsapp/messages", params={"sender": SENDER}))
    assert len(saved["messages"]) == 2 and saved["thread"]["active_draft_id"]
    with app.state.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM whatsapp_events WHERE event_id=?", (event,)).fetchone()[0] == 0
        conn.execute("DROP TRIGGER reject_chat_reply")
    assert data(simulate(client, "ya", key=key, event=event))["result"]["action"] == "approved"


def test_simulator_requires_owner_idempotency_and_local_origin(client):
    assert simulate(client, "ya", role="staff").status_code == 403
    assert client.get("/v1/whatsapp/messages", headers={"X-Demo-Role": "staff"}).status_code == 403
    assert client.post("/v1/whatsapp/simulate", json={"text": "ya"}).status_code == 400
    assert client.post("/v1/whatsapp/simulate", json={"text": "ya"}, headers={"Idempotency-Key": uuid4().hex, "Origin": "https://evil.example"}).status_code == 403


def test_public_origin_must_be_https_without_path(tmp_path):
    for origin in ("http://example.com", "https://example.com/path", "https://user:pass@example.com", "https://example.com?token=x"):
        with pytest.raises(ValueError):
            create_app(tmp_path / "bad-config.sqlite3", public_base_url=origin)


def test_concurrent_same_event_creates_one_draft_and_message_pair(client):
    event = uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: data(simulate(client, "Cabai 3 kg 150rb", event=event)), range(2)))
    assert results[0] == results[1]
    assert len(results[0]["messages"]) == 2
    assert len(overview(client)["drafts"]) == 4
