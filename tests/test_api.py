"""Exercise the real API and persistent ledger, without providers or network."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, login_owner
from service.api import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "demo.sqlite3")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        login_owner(value)
        yield value


@pytest.fixture
def admin_client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        login_admin(value)
        yield value


def headers(key=None):
    return {"Idempotency-Key": key or uuid4().hex}


def overview(client):
    response = client.get("/v1/overview")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def pending(client):
    return next(draft for draft in overview(client)["drafts"] if draft["status"] == "pending_approval")


def decision(client, draft, *, approve=True, received=True, key=None, version=None):
    return client.post(f"/v1/drafts/{draft['id']}/decision", headers=headers(key), json={
        "expected_version": version or draft["version"], "decision": "approve" if approve else "reject", "received": received,
    })


def edit_payload(draft, **changes):
    fields = ("ingredient_id", "raw_name", "quantity", "unit", "line_total_idr")
    value = {"expected_version": draft["version"], "supplier": draft["supplier"],
             "items": [{field: item.get(field) for field in fields} for item in draft["items"]]}
    value.update(changes)
    return value


def test_seed_comparison_and_overview(client):
    state = overview(client)
    assert state["mode"] == {"transport": "manual", "extraction": "rules", "storage": "sqlite", "identity": "session"}
    assert state["metrics"] == {"purchases_today_idr": 0, "purchases_yesterday_idr": 0, "pending_approvals": 1, "low_stock_count": 8, "recorded_purchases": 2}
    draft = pending(client)
    comparison = draft["items"][0]["comparison"]
    assert comparison["status"] == "price_increase"
    assert comparison["change_percent"] == "16.3"
    assert comparison["baseline_unit_price_idr"] == "43000"
    assert comparison["baseline_purchase_id"] in {row["id"] for row in state["purchases"]}
    assert all(row["payment_status"] == "unknown" for row in state["purchases"])


def test_overview_delta_splits_today_from_yesterday(client):
    state = overview(client)
    assert state["metrics"]["purchases_yesterday_idr"] == 0
    assert decision(client, pending(client)).status_code == 200
    state = overview(client)
    assert state["metrics"]["purchases_today_idr"] == 150000
    assert state["metrics"]["purchases_yesterday_idr"] == 0


def test_draft_provenance_reflects_intake_channel(client):
    manual = client.post("/v1/intake", headers=headers(), json={"text": "beli cabai 3 kg 150rb"}).json()["data"]
    simulated = client.post("/v1/whatsapp/simulate", headers=headers(), json={"sender": "628123456789", "text": "beli telur 2 kg 60000"}).json()["data"]["draft"]
    drafts = {draft["id"]: draft for draft in overview(client)["drafts"]}
    assert "source" not in drafts[manual["id"]]
    assert drafts[simulated["id"]]["source"]["kind"] == "simulator"
    assert drafts[simulated["id"]]["source"]["created_at"]


def test_intake_edit_approval_updates_real_stock_and_spend(client):
    response = client.post("/v1/intake", headers=headers(), json={"text": "Cabai 500 g 25rb", "supplier": "Pasar Pagi"})
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    assert draft["status"] == "pending_approval"
    assert draft["items"][0]["canonical_quantity"] == "0.5"
    payload = edit_payload(draft)
    payload["items"][0]["line_total_idr"] = 26000
    response = client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=payload)
    assert response.status_code == 200, response.text
    updated = response.json()["data"]
    assert updated["version"] == draft["version"] + 1
    response = decision(client, updated)
    assert response.status_code == 200, response.text
    recorded = response.json()["data"]
    assert recorded["status"] == "recorded" and recorded["purchase_id"]
    assert recorded["items"][0]["comparison"]["change_percent"] == "20.9"
    state = overview(client)
    assert state["metrics"]["purchases_today_idr"] == 26000
    assert next(item["quantity"] for item in state["inventory"] if item["ingredient_id"] == "cabai") == "3.5"
    assert len(state["purchases"]) == 3
    purchase = next(row for row in state["purchases"] if row["id"] == recorded["purchase_id"])
    assert purchase["payment_status"] == "unknown"
    assert client.get(f"/v1/drafts/{draft['id']}").json()["data"]["status"] == "recorded"


def test_reject_never_posts_inventory_or_purchase(client):
    before = overview(client)
    draft = pending(client)
    response = decision(client, draft, approve=False, received=False)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "rejected"
    after = overview(client)
    assert after["purchases"] == before["purchases"]
    assert after["inventory"] == before["inventory"]
    assert decision(client, response.json()["data"]).status_code == 409


def test_ambiguity_missing_fields_and_wrong_dimensions_block_approval(client):
    for text in ("Telur 150rb", "Telur 2 tray 100rb", "Minyak 2 kg 50rb", "Nanti beli beras 5 kg 70rb"):
        response = client.post("/v1/intake", headers=headers(), json={"text": text})
        assert response.status_code == 201
        draft = response.json()["data"]
        assert draft["status"] == "needs_clarification" and draft["warnings"]
        rejection = decision(client, draft)
        assert rejection.status_code == 422
        assert rejection.json()["error"]["code"] == "clarification_required"
    assert len(overview(client)["purchases"]) == 2


def test_manual_correction_clears_extraction_warnings(client):
    response = client.post("/v1/intake", headers=headers(), json={"text": "Telur 150rb", "supplier": "Pasar Pagi"})
    draft = response.json()["data"]
    payload = edit_payload(draft)
    payload["items"][0].update({"ingredient_id": "telur", "quantity": "3", "unit": "kg"})
    response = client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=payload)
    assert response.status_code == 200
    corrected = response.json()["data"]
    assert corrected["status"] == "pending_approval" and not corrected["warnings"]
    assert decision(client, corrected).status_code == 200


def test_received_confirmation_is_required_and_payment_not_inferred(client):
    draft = pending(client)
    response = decision(client, draft, received=False)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "receipt_required"
    assert len(overview(client)["purchases"]) == 2
    assert decision(client, draft).status_code == 200
    assert overview(client)["purchases"][0]["payment_status"] == "unknown"


def test_stale_version_blocks_edit_and_approval(client):
    draft = pending(client)
    payload = edit_payload(draft)
    first = client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=payload)
    assert first.status_code == 200
    stale_edit = client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=payload)
    assert stale_edit.status_code == 409
    assert stale_edit.json()["error"]["code"] == "version_conflict"
    assert decision(client, draft).status_code == 409
    assert len(overview(client)["purchases"]) == 2


def test_admin_can_review_but_not_edit_or_decide_drafts(admin_client):
    state = admin_client.get("/v1/admin/restaurants/demo-resto/overview").json()["data"]
    draft = next(row for row in state["drafts"] if row["status"] == "pending_approval")
    assert admin_client.post("/v1/intake", headers=headers(), json={"text": "Cabai 2 kg 100rb"}).status_code == 403
    assert admin_client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=edit_payload(draft)).status_code == 403
    assert decision(admin_client, draft).status_code == 403
    assert decision(admin_client, draft, approve=False).status_code == 403
    # The read-only admin view still sees the untouched queue.
    detail = admin_client.get("/v1/admin/restaurants/demo-resto").json()["data"]
    assert detail["pending_drafts"] == 1


def test_unauthenticated_requests_are_rejected(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as anon:
        assert anon.get("/v1/overview").status_code == 401
        assert anon.post("/v1/intake", headers=headers(), json={"text": "Cabai 2 kg 100rb"}).status_code == 401
        assert anon.post("/v1/whatsapp/simulate", headers=headers(), json={"text": "ya", "sender": "x"}).status_code == 401


def test_idempotency_replays_exact_response_and_rejects_key_reuse(client):
    key = uuid4().hex
    payload = {"text": "Cabai 2 kg 100rb", "supplier": "Pasar Pagi"}
    first = client.post("/v1/intake", headers=headers(key), json=payload)
    repeat = client.post("/v1/intake", headers=headers(key), json=payload)
    assert repeat.status_code == 201 and repeat.json() == first.json()
    assert len(overview(client)["drafts"]) == 4
    conflict = client.post("/v1/intake", headers=headers(key), json={"text": "Beras 2 kg 50rb"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    draft = first.json()["data"]
    decision_key = uuid4().hex
    approved = decision(client, draft, key=decision_key)
    replay = decision(client, draft, key=decision_key)
    assert approved.json() == replay.json()
    assert decision(client, draft).status_code == 409
    assert len(overview(client)["purchases"]) == 3


def test_concurrent_decisions_post_exactly_once(app, client):
    draft = pending(client)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: decision(client, draft), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    state = overview(client)
    assert len(state["purchases"]) == 3
    assert next(row["quantity"] for row in state["inventory"] if row["ingredient_id"] == "telur") == "13"
    with app.state.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM movements WHERE purchase_id IS NOT NULL").fetchone()[0] == 3


def test_concurrent_same_key_returns_same_commit(client):
    draft, key = pending(client), uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: decision(client, draft, key=key), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert len(overview(client)["purchases"]) == 3


def test_posting_failure_rolls_back_ledger_stock_audit_and_response(app, client):
    draft, key = pending(client), uuid4().hex
    before = overview(client)
    with app.state.store.transaction() as conn:
        conn.execute("CREATE TRIGGER reject_purchase_audit BEFORE INSERT ON activity WHEN NEW.event='purchase_recorded' BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END")
    response = decision(client, draft, key=key)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    after = overview(client)
    assert after == before
    with app.state.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM idempotency WHERE key=?", (key,)).fetchone()[0] == 0
        conn.execute("DROP TRIGGER reject_purchase_audit")
    assert decision(client, draft, key=key).status_code == 200
    assert len(overview(client)["purchases"]) == 3


def test_restart_preserves_approval_and_idempotency(tmp_path):
    path = tmp_path / "restart.sqlite3"
    app1 = create_app(path)
    with TestClient(app1, base_url="http://127.0.0.1") as client1:
        login_owner(client1)
        draft, key = pending(client1), uuid4().hex
        response = decision(client1, draft, key=key)
        before = overview(client1)
    with TestClient(create_app(path), base_url="http://127.0.0.1") as client2:
        login_owner(client2)
        assert overview(client2) == before
        assert decision(client2, draft, key=key).json() == response.json()


def test_error_envelope_validation_and_local_browser_boundary(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get("/v1/drafts/missing").json()["error"]["code"] == "not_found"
    assert client.get("/missing").json()["error"]["code"] == "not_found"
    assert client.post("/v1/intake", json={"text": "Cabai 2 kg 100rb"}).status_code == 400
    assert client.post("/v1/intake", headers=headers(), json={"text": " "}).status_code == 422
    draft = pending(client)
    payload = edit_payload(draft)
    payload["items"][0]["line_total_idr"] = True
    assert client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=payload).status_code == 422
    assert client.get("/v1/overview", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/v1/overview", headers={"Origin": "http://localhost"}).status_code == 403
    assert client.get("/v1/overview", headers={"Origin": "http://127.0.0.1"}).status_code == 200
    assert client.get("/v1/overview", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/v1/overview", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_public_demo_mode_allows_hosted_boundary(tmp_path):
    app = create_app(tmp_path / "public.sqlite3", public_demo=True)
    with TestClient(app, base_url="https://restopilot.vercel.app", raise_server_exceptions=False) as client:
        login_owner(client)
        assert client.get("/health/live").status_code == 200
        assert client.get("/v1/overview").status_code == 200
        assert client.get("/v1/overview", headers={"Origin": "https://restopilot.vercel.app"}).status_code == 200
        # Public mode has no session or cookie to protect, so foreign origins pass.
        assert client.get("/v1/overview", headers={"Origin": "https://evil.example"}).status_code == 200
        assert client.get("/v1/overview", headers={"Host": "restopilot.vercel.app"}).status_code == 200
        assert client.get("/v1/overview", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
        # Hardening headers still apply in public mode.
        assert client.get("/health/live").headers["X-Content-Type-Options"] == "nosniff"


def test_supplier_and_30_day_baseline_are_bounded(app, client):
    draft = pending(client)
    payload = edit_payload(draft, supplier="Different supplier")
    updated = client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=payload).json()["data"]
    assert updated["items"][0]["comparison"]["status"] == "insufficient_history"
    with app.state.store.transaction() as conn:
        conn.execute("UPDATE purchases SET created_at='2000-01-01T00:00:00+00:00'")
    payload = edit_payload(updated, supplier="Pasar Pagi")
    updated = client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=payload).json()["data"]
    assert updated["items"][0]["comparison"]["status"] == "insufficient_history"


def test_multiple_lines_for_one_ingredient_make_one_movement(app, client):
    draft = client.post("/v1/intake", headers=headers(), json={"text": "Cabai 250 g 10rb\nCabai 750 g 30rb", "supplier": "Pasar Pagi"}).json()["data"]
    response = decision(client, draft)
    assert response.status_code == 200, response.text
    purchase_id = response.json()["data"]["purchase_id"]
    with app.state.store.transaction() as conn:
        rows = conn.execute("SELECT quantity FROM movements WHERE purchase_id=?", (purchase_id,)).fetchall()
        assert len(rows) == 1 and Decimal(rows[0][0]) == Decimal(1)
    assert next(row["quantity"] for row in overview(client)["inventory"] if row["ingredient_id"] == "cabai") == "4"


def test_exact_price_threshold_uses_cross_products(client):
    baseline = client.post("/v1/intake", headers=headers(), json={"text": "Beras 3 kg Rp20", "supplier": "Boundary fixture"}).json()["data"]
    assert decision(client, baseline).status_code == 200
    at_threshold = client.post("/v1/intake", headers=headers(), json={"text": "Beras 3 kg Rp23", "supplier": "Boundary fixture"}).json()["data"]
    assert at_threshold["items"][0]["comparison"]["status"] == "price_increase"
    assert at_threshold["items"][0]["comparison"]["change_percent"] == "15.0"
    below_threshold = client.post("/v1/intake", headers=headers(), json={"text": "Beras 3 kg Rp22", "supplier": "Boundary fixture"}).json()["data"]
    assert below_threshold["items"][0]["comparison"]["status"] == "comparable"


@pytest.mark.parametrize("instant", ["2026-10-17T23:30:00+00:00", "2026-10-18T00:30:00+00:00"])
def test_purchases_today_uses_wib_across_utc_midnight(app, client, monkeypatch, instant):
    from service import db

    frozen = datetime.fromisoformat(instant)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(db, "datetime", FrozenDateTime)
    with app.state.store.transaction() as conn:
        for row in conn.execute("SELECT id, body FROM purchases").fetchall():
            purchase = json.loads(row["body"])
            # 23:00 WIB on Oct17 versus 01:00 WIB on Oct18. Both are Oct17 UTC.
            purchase["created_at"] = "2026-10-17T16:00:00+00:00" if purchase["total_idr"] == 129000 else "2026-10-17T18:00:00+00:00"
            conn.execute("UPDATE purchases SET created_at=?, body=? WHERE id=?", (purchase["created_at"], json.dumps(purchase), row["id"]))
    assert overview(client)["metrics"]["purchases_today_idr"] == 430000


def test_incomplete_or_invalid_drafts_do_not_claim_partial_totals(client):
    draft = client.post("/v1/intake", headers=headers(), json={"text": "Cabai 2 kg 100rb\nTelur 3 kg"}).json()["data"]
    assert draft["status"] == "needs_clarification"
    assert draft["total_idr"] is None
    payload = edit_payload(draft)
    payload["items"][1]["line_total_idr"] = 150000
    fixed = client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=payload).json()["data"]
    assert fixed["total_idr"] == 250000
    payload = edit_payload(fixed)
    payload["items"][1]["line_total_idr"] = -1
    invalid = client.patch(f"/v1/drafts/{draft['id']}", headers=headers(), json=payload).json()["data"]
    assert invalid["total_idr"] is None
    assert decision(client, invalid).status_code == 422
    omitted_clause = client.post("/v1/intake", headers=headers(), json={"text": "Cabai 2 kg 100rb\nOngkir 10rb"}).json()["data"]
    assert omitted_clause["total_idr"] is None
    empty = client.post("/v1/intake", headers=headers(), json={"text": "nanti beli beras 5 kg 70rb"}).json()["data"]
    assert empty["total_idr"] is None
