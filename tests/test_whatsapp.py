"""Persistent provider-neutral WhatsApp simulator tests."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.whatsapp import draft_reply, parse_payment_followup


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


def test_unmapped_ingredient_self_heals_on_approval(client):
    created = data(simulate(client, "Beli gula 1 kg 20 rb"))
    draft = created["draft"]
    assert draft["status"] == "pending_approval"
    assert draft["items"][0]["ingredient_id"] is None and draft["items"][0]["raw_name"] == "gula"
    reply = created["messages"][-1]["body"]
    assert "gula belum ada di daftar bahan" in reply and "ditambahkan otomatis" in reply
    approved = data(simulate(client, "ya"))
    assert approved["result"]["action"] == "approved"
    confirmation = approved["messages"][-1]["body"]
    assert "tercatat" in confirmation and "'Gula' ditambahkan sebagai bahan baru" in confirmation
    state = overview(client)
    purchase = state["purchases"][0]
    assert purchase["items"][0]["ingredient_id"] == "gula" and purchase["payment_status"] == "unknown"
    assert next(row for row in state["inventory"] if row["ingredient_id"] == "gula")["name"] == "Gula"
    # A repeat purchase of the same unknown ingredient links without duplicating.
    data(simulate(client, "Beli gula 2 kg 40 rb"))
    again = data(simulate(client, "oke"))
    assert again["result"]["action"] == "approved"
    assert "bahan baru" not in again["messages"][-1]["body"]
    state = overview(client)
    assert next(row["quantity"] for row in state["inventory"] if row["ingredient_id"] == "gula") == "3"
    assert sum(1 for row in state["inventory"] if row["ingredient_id"] == "gula") == 1


def test_payment_followup_completes_the_recorded_purchase(client):
    data(simulate(client, "Cabai 3 kg 150rb"))
    approved = data(simulate(client, "ya"))
    ask = approved["messages"][-1]["body"]
    assert "lunas" in ask and "utang" in ask and "skip" in ask
    followup = data(simulate(client, "Lunas, dari Pasar Pagi"))
    assert followup["result"]["action"] == "purchase_updated"
    purchase = overview(client)["purchases"][0]
    assert purchase["payment_status"] == "paid" and purchase["supplier"] == "Pasar Pagi"

    data(simulate(client, "Telur 1 kg 30rb"))
    assert data(simulate(client, "sip"))["result"]["action"] == "approved"
    unpaid = data(simulate(client, "utang"))
    assert unpaid["result"]["action"] == "purchase_updated"
    purchase = overview(client)["purchases"][0]
    assert purchase["payment_status"] == "unpaid" and not purchase["supplier"]


def test_supplier_reply_canonicalizes_against_recorded_suppliers(client):
    data(simulate(client, "Cabai 3 kg 150rb"))
    assert data(simulate(client, "ya"))["result"]["action"] == "approved"
    first = data(simulate(client, "di AEON"))
    assert "Penjual: AEON (pemasok baru)." in first["messages"][-1]["body"]
    assert overview(client)["purchases"][0]["supplier"] == "AEON"

    data(simulate(client, "Telur 1 kg 30rb"))
    assert data(simulate(client, "sip"))["result"]["action"] == "approved"
    second = data(simulate(client, "utang di aeon mall"))
    assert "Penjual: AEON." in second["messages"][-1]["body"]
    assert overview(client)["purchases"][0]["supplier"] == "AEON"


def test_payment_followup_skips_and_lapses_without_blocking(client):
    data(simulate(client, "Cabai 3 kg 150rb"))
    assert data(simulate(client, "ya"))["result"]["action"] == "approved"
    skipped = data(simulate(client, "skip"))
    assert skipped["result"]["action"] == "followup_skipped"
    assert "dicatat" in skipped["messages"][-1]["body"]
    assert overview(client)["purchases"][0]["payment_status"] == "unknown"

    data(simulate(client, "Beras 1 kg 20rb"))
    assert data(simulate(client, "oke"))["result"]["action"] == "approved"
    # A new purchase right after a recording is a draft, never a payment detail.
    newdraft = data(simulate(client, "Minyak 2 l 40rb"))
    assert newdraft["result"]["action"] == "draft_created"
    assert newdraft["draft"]["items"][0]["ingredient_id"] == "minyak"


def test_approval_and_rejection_variants_are_accepted(client):
    for word in ("YA", "y", "Ok", "oke", "SIP", "Setuju", "betul", "approve", "ya benar"):
        data(simulate(client, "Beras 1 kg 20rb"))
        assert data(simulate(client, word))["result"]["action"] == "approved", word
    for word in ("BATAL", "cancel", "tidak", "jangan"):
        data(simulate(client, "Beras 1 kg 20rb"))
        assert data(simulate(client, word))["result"]["action"] == "rejected", word


def test_payment_followup_parser_matches_spec_examples():
    assert parse_payment_followup("lunas") == {"payment_status": "paid", "supplier": None, "skip": False}
    assert parse_payment_followup("LUNAS SEMUA")["payment_status"] == "paid"
    assert parse_payment_followup("sudah dibayar")["payment_status"] == "paid"
    assert parse_payment_followup("utang")["payment_status"] == "unpaid"
    assert parse_payment_followup("belum bayar")["payment_status"] == "unpaid"
    assert parse_payment_followup("lunas, dari Pasar Pagi") == {"payment_status": "paid", "supplier": "Pasar Pagi", "skip": False}
    assert parse_payment_followup("utang di Bu Siti")["supplier"] == "Bu Siti"
    assert parse_payment_followup("dari Pasar Pagi") == {"payment_status": None, "supplier": "Pasar Pagi", "skip": False}
    assert parse_payment_followup("skip") == {"payment_status": None, "supplier": None, "skip": True}
    # Purchase messages and small talk never masquerade as payment replies.
    assert parse_payment_followup("Beli telur 2 kg 30rb") is None
    assert parse_payment_followup("halo") is None
    assert parse_payment_followup("ya") is None


def test_draft_reply_template_is_scannable_and_confirms_as_one():
    draft = {
        "id": "83216d28abcdef1234", "version": 1, "status": "pending_approval",
        "supplier": None, "warnings": [], "total_idr": 100000,
        "items": [
            {"ingredient_id": "bawang", "raw_name": "bawang", "quantity": "2", "unit": "kg", "line_total_idr": 80000, "unit_price_idr": "40000"},
            {"ingredient_id": None, "raw_name": "gula", "quantity": "1", "unit": "kg", "line_total_idr": 20000, "unit_price_idr": "20000"},
        ],
    }
    lines = draft_reply(draft).splitlines()
    assert lines[0] == "Draf pembelian:"
    assert "• Bawang merah — 2 kg × Rp40.000 = Rp80.000" in lines
    assert "• Gula — 1 kg × Rp20.000 = Rp20.000" in lines
    assert "Total: Rp100.000" in lines
    assert any("Catatan: gula belum ada di daftar bahan" in line and "balas YA" in line for line in lines)
    assert lines[-1] == "Balas YA jika semua barang sudah diterima, BATAL untuk batal."


def test_draft_reply_needs_clarification_shows_total_supplier_and_groups_notes():
    draft = {
        "id": "12eceb45b39b4632", "version": 1, "status": "needs_clarification",
        "supplier": "PETEK SEGAR INDONESIA, PT", "total_idr": None,
        "warnings": [
            "Line 2: unknown ingredient 'pecombang'; select a catalog item manually.",
            "Line 9: the photo was partly unreadable; verify this line.",
        ],
        "items": [
            {"ingredient_id": "bawang", "raw_name": "banwang meksah kupas", "quantity": "3.5", "unit": "kg", "line_total_idr": 129500, "unit_price_idr": "37000"},
            {"ingredient_id": None, "raw_name": "pecombang", "quantity": "1", "unit": "kg", "line_total_idr": 34000, "unit_price_idr": "34000"},
        ],
    }
    reply = draft_reply(draft)
    assert reply.startswith("Draf pembelian (perlu konfirmasi):")
    assert "Pemasok: PETEK SEGAR INDONESIA, PT" in reply
    assert "Total sementara: Rp163.500" in reply
    # English dev-warnings never leak into the owner chat; unmapped items are
    # grouped into one friendly note and other problems stay generic.
    assert "unknown ingredient" not in reply
    assert "select a catalog item" not in reply
    assert "Catatan: pecombang belum ada di daftar bahan" in reply
    assert "perlu dicek di dashboard" in reply
    assert reply.splitlines()[-1].startswith("Balas YA untuk konfirmasi semua item")


def test_greeting_text_gets_help_instead_of_an_empty_draft(client):
    before = len(overview(client)["drafts"])
    result = data(simulate(client, "hello"))
    assert "draft" not in result
    assert result["result"]["action"] == "help_reply"
    assert result["thread"]["active_draft_id"] is None
    reply = result["messages"][-1]
    assert reply["direction"] == "outbound" and reply["draft_id"] is None
    assert "asisten pembelian RestoPilot" in reply["body"]
    assert "Beli bawang 2 kg 80 rb" in reply["body"]
    assert len(overview(client)["drafts"]) == before
    # Texts carrying purchase signal still draft, even when incomplete.
    assert data(simulate(client, "beli beras"))["result"]["action"] == "review_required"
    assert data(simulate(client, "batal"))["result"]["action"] == "rejected"
    created = data(simulate(client, "Beli bawang 2 kg 80 rb"))
    assert created["result"]["action"] == "draft_created"
    assert created["draft"]["items"][0]["raw_name"] == "bawang"
