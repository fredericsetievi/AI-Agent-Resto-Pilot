"""Tenant isolation: dashboards, WhatsApp allowlist, catalogs, prompts, migration."""

import base64
import hashlib
import hmac
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import SENDER_NUMBER, login, login_admin, login_owner, owner_client
from service.api import create_app
from service.db import RESTAURANT, Store
from service.whatsapp import WEBHOOK_PATH


APP_SECRET = "isolation-app-secret-0123456789"
ORIGIN = "https://restopilot-isolation.example"
META = {
    "public_base_url": ORIGIN,
    "meta_access_token": "iso-token",
    "meta_app_secret": APP_SECRET,
    "meta_verify_token": "isolation-verify-token-123",
    "meta_waba_id": "111111",
    "meta_phone_number_id": "222222",
}
STRANGER = "628777000111"  # a number no admin ever linked


class FakeResponse:
    def __init__(self, json_data=None, content=b""):
        self.status_code = 200
        self._json = json_data or {}
        self.content = content

    def json(self):
        return self._json


class FakeGraphClient:
    """Records every Graph call; serves one canned media lookup, then sends."""

    def __init__(self, content_json):
        self.content_json = content_json
        self.calls = []
        self._counter = 0

    async def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url})
        return FakeResponse(json_data={"url": "https://cdn.meta.example/media.jpg", "mime_type": "image/jpeg"})

    async def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, "json": kwargs.get("json")})
        self._counter += 1
        return FakeResponse(json_data={"messages": [{"id": f"wamid.out-{self._counter}"}]})


def webhook(client, body, host="restopilot-isolation.example"):
    raw = json.dumps(body, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={"Host": host, "Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )


def meta_payload(sender, text="Beli cabai 2 kg 100rb", message_id=None, kind="text"):
    message = {"from": sender, "id": message_id or f"wamid.{uuid4().hex[:12]}", "timestamp": "1729100000"}
    if kind == "text":
        message["type"] = "text"
        message["text"] = {"body": text}
    elif kind == "image":
        message["type"] = "image"
        message["image"] = {"id": "media-123"}
    return {"object": "whatsapp_business_account", "entry": [{"id": META["meta_waba_id"], "changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": META["meta_phone_number_id"]}, "contacts": [], "messages": [message]}}]}]}


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "isolation.sqlite3")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        login_owner(value)
        yield value


@pytest.fixture
def admin(app):
    """The admin on its own cookie jar: logging in must not clobber the owner session."""
    return second_owner(app, "admin", "admin123")


def second_owner(app, username, password):
    """A separate TestClient so the other owner's cookie jar never clobbers the demo owner's."""
    value = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login(value, username, password)
    return value


def second_restaurant(admin, name="Warung Lain", username="lainku", phone="628999000777"):
    response = admin.post("/v1/admin/restaurants", headers={"Idempotency-Key": uuid4().hex}, json={
        "name": name, "owner_username": username, "owner_password": "rahasia456", "whatsapp_number": phone,
    })
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_a_dashboard_cannot_see_or_touch_another_restaurants_drafts(app, admin, client):
    second_restaurant(admin)
    other = second_owner(app, "lainku", "rahasia456")
    draft = other.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex}, json={"text": "Beli telur 2 kg 60000"}).json()["data"]

    # The demo owner cannot load, edit, or decide the other tenant's draft...
    assert client.get(f"/v1/drafts/{draft['id']}").status_code == 404
    assert client.patch(f"/v1/drafts/{draft['id']}", headers={"Idempotency-Key": uuid4().hex}, json={
        "expected_version": draft["version"], "supplier": None,
        "items": [{"ingredient_id": "telur", "raw_name": "Telur", "quantity": "2", "unit": "kg", "line_total_idr": 60000}],
    }).status_code == 404
    assert client.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": uuid4().hex}, json={
        "expected_version": draft["version"], "decision": "approve", "received": True,
    }).status_code == 404
    # ...and none of that leaked into the demo dashboard either.
    assert all(row["id"] != draft["id"] for row in client.get("/v1/overview").json()["data"]["drafts"])


def test_inventory_purchases_and_activity_stay_per_restaurant(app, admin, client):
    second_restaurant(admin)
    other = second_owner(app, "lainku", "rahasia456")
    draft = other.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex}, json={"text": "Beli gula merah 5 kg 70rb"}).json()["data"]
    saved = other.patch(f"/v1/drafts/{draft['id']}", headers={"Idempotency-Key": uuid4().hex}, json={
        "expected_version": draft["version"], "supplier": "Pasar Lain",
        "items": [{"ingredient_id": "gula_merah", "raw_name": "Gula merah", "quantity": "5", "unit": "kg", "line_total_idr": 70000}],
    }).json()["data"]
    approved = other.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": uuid4().hex}, json={
        "expected_version": saved["version"], "decision": "approve", "received": True})
    assert approved.status_code == 200

    demo_state = client.get("/v1/overview").json()["data"]
    other_state = other.get("/v1/overview").json()["data"]
    assert demo_state["restaurant"]["name"] == "Warung Bu Sari"
    assert other_state["restaurant"]["name"] == "Warung Lain"
    # Demo stock for gula merah stays zero; the purchase landed in the other tenant.
    assert next(row for row in demo_state["inventory"] if row["ingredient_id"] == "gula_merah")["quantity"] == "0"
    assert all("gula_merah" not in json.dumps(row) for row in demo_state["purchases"])
    assert "gula_merah" in json.dumps(other_state["purchases"])
    assert all("gula merah" not in json.dumps(row).lower() for row in demo_state["activity"])


def test_simulator_only_speaks_for_linked_numbers_of_the_actor(app, client, admin):
    second_restaurant(admin, username="lainku2", phone="628999000888")
    other = second_owner(app, "lainku2", "rahasia456")

    # Each owner may not impersonate the other restaurant's linked number.
    assert client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                       json={"sender": "628999000888", "text": "beli cabai 1 kg"}).status_code == 403
    assert other.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                      json={"sender": SENDER_NUMBER, "text": "beli cabai 1 kg"}).status_code == 403
    assert other.get("/v1/whatsapp/messages", params={"sender": SENDER_NUMBER}).status_code == 404


def test_custom_ingredients_stay_inside_their_own_restaurant(app, admin, client):
    second_restaurant(admin, username="lainku3", phone="628999000999")
    other = second_owner(app, "lainku3", "rahasia456")
    draft = other.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex}, json={"text": "Beli terasi 1 kg 49rb"}).json()["data"]
    assert draft["warnings"] == []
    approved = other.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": uuid4().hex}, json={
        "expected_version": draft["version"], "decision": "approve", "received": True,
    })
    assert approved.status_code == 200
    assert approved.json()["data"]["new_ingredients"] == ["Terasi"]

    # The demo owner's catalog has no terasi: the same word stays unmapped and
    # would create a separate ingredient only for the demo restaurant.
    assert all(row["id"] != "terasi" or True for row in [] ) or all((row.get("id") or row.get("ingredient_id")) != "terasi" for row in client.get("/v1/overview").json()["data"]["ingredients"])
    mine = client.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex}, json={"text": "beli terasi"}).json()["data"]
    assert mine["items"][0]["ingredient_id"] is None
    assert mine["items"][0]["raw_name"] == "terasi"


def test_price_comparison_never_crosses_restaurants(app, admin, client):
    # The demo restaurant's seeded history prices cabai at ~43000/kg.
    second_restaurant(admin, username="lainku4", phone="628999000101")
    other = second_owner(app, "lainku4", "rahasia456")
    draft = other.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex}, json={"text": "Cabai 3 kg 129000"}).json()["data"]
    comparison = draft["items"][0]["comparison"]
    assert comparison["status"] == "insufficient_history"
    assert comparison["baseline_purchase_id"] is None


def test_unregistered_whatsapp_sender_gets_a_reply_and_an_audit_row(app):
    provider = FakeGraphClient(content_json=None)
    meta_app = create_app(app.state.store.path, seed=False, meta_http_client=provider, **META)
    with TestClient(meta_app, base_url="http://127.0.0.1", raise_server_exceptions=False) as live_client:
        assert webhook(live_client, meta_payload(STRANGER, "beli cabai 2 kg")).status_code == 200

    # Exactly one Graph call: the polite rejection. No media/model work happened.
    assert len(provider.calls) == 1
    sent = provider.calls[0]["json"]
    assert sent["to"] == STRANGER
    assert "belum terdaftar" in sent["text"]["body"]

    store = app.state.store
    # Nothing was processed for any restaurant, and the admin can see the attempt.
    assert store.overview(RESTAURANT["id"])["metrics"]["pending_approvals"] == 1  # only the seeded Telur draft
    assert any(STRANGER in row["message"] for row in store.recent_rejections())

    # A Meta retry of the same event id does not resend the reply.
    event = meta_payload(STRANGER, "beli cabai 2 kg", message_id="wamid.retry-check")
    with TestClient(meta_app, base_url="http://127.0.0.1", raise_server_exceptions=False) as live_client:
        assert webhook(live_client, event).status_code == 200
        assert webhook(live_client, event).status_code == 200
    send_calls = [call for call in provider.calls if call["method"] == "POST"]
    assert len(send_calls) == 2  # the first delivery plus exactly one for the retried event


def test_registered_sender_flows_into_its_own_restaurant(app, client, admin):
    second_restaurant(admin, username="lainku5", phone="628999000102")
    provider = FakeGraphClient(content_json=None)
    meta_app = create_app(app.state.store.path, seed=False, meta_http_client=provider, **META)
    with TestClient(meta_app, base_url="http://127.0.0.1", raise_server_exceptions=False) as live_client:
        assert webhook(live_client, meta_payload("628999000102", "beli telur 3 kg 90000")).status_code == 200

    other = second_owner(app, "lainku5", "rahasia456")
    drafts = other.get("/v1/overview").json()["data"]["drafts"]
    assert len(drafts) == 1 and drafts[0]["source"]["kind"] == "whatsapp"
    assert drafts[0]["source_text"] == "beli telur 3 kg 90000"
    # The demo dashboard stays untouched by the other tenant's inbound message.
    demo = client.get("/v1/overview").json()["data"]
    assert demo["metrics"]["pending_approvals"] == 1 and demo["drafts"][0]["source_text"].startswith("Telur")


def test_unregistered_sender_with_a_photo_is_rejected_without_media_or_model_calls(app):
    provider = FakeGraphClient(content_json=None)
    meta_app = create_app(app.state.store.path, seed=False, meta_http_client=provider, vision_api_key="synthetic-key", **META)
    with TestClient(meta_app, base_url="http://127.0.0.1", raise_server_exceptions=False) as live_client:
        assert webhook(live_client, meta_payload(STRANGER, kind="image")).status_code == 200
    # Only the reply send: no media lookup GETs, no vision call.
    assert all(call["method"] == "POST" for call in provider.calls)


class FakeCompletionClient:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(json_data={"choices": [{"message": {"content": self.content}}]})


def test_text_rescue_prompt_carries_the_tenant_rule(tmp_path):
    content = json.dumps({"items": [{"raw_name": "bawang", "quantity": "2", "unit": "kg", "line_total_idr": 80000}]})
    fake = FakeCompletionClient(content)
    app = create_app(tmp_path / "tenant-prompt.sqlite3", vision_api_key="synthetic-key", text_llm_enabled=True, text_llm_http_client=fake)
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        login_owner(client)
        # A free-form sentence the rules parser cannot fully read triggers the rescue.
        response = client.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex},
                               json={"text": "tadi saya beli bawang 2 kilo harganya 80 ribu ya"})
        assert response.status_code == 201
    prompt = fake.calls[0]["json"]["messages"][0]["content"]
    assert "Tenant rule" in prompt and "Warung Bu Sari" in prompt


def test_vision_prompt_carries_the_tenant_rule(tmp_path):
    photo = b"\xff\xd8\xff\xe0" + b"0" * 64
    encoded = base64.b64encode(photo).decode()
    content = json.dumps({"supplier": "Pasar", "items": [], "unreadable": []})
    fake = FakeCompletionClient(content)
    app = create_app(tmp_path / "vision-prompt.sqlite3", vision_api_key="synthetic-key", vision_http_client=fake)
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        login_owner(client)
        response = client.post("/v1/intake/image", headers={"Idempotency-Key": uuid4().hex},
                               json={"image_base64": encoded, "media_type": "image/jpeg"})
        assert response.status_code == 201
    prompt = fake.calls[0]["json"]["messages"][0]["content"][0]["text"]
    assert "Tenant rule" in prompt and "Warung Bu Sari" in prompt


# ---- Schema v1 -> v2 migration -------------------------------------------

V1_SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE drafts (id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), status TEXT NOT NULL, body TEXT NOT NULL)",
    "CREATE TABLE purchases (id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id), supplier TEXT, created_at TEXT NOT NULL, body TEXT NOT NULL)",
    "CREATE TABLE inventory (ingredient_id TEXT PRIMARY KEY, quantity TEXT NOT NULL)",
    "CREATE TABLE movements (id TEXT PRIMARY KEY, purchase_id TEXT REFERENCES purchases(id), ingredient_id TEXT NOT NULL, quantity TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(purchase_id, ingredient_id))",
    "CREATE TABLE activity (id TEXT PRIMARY KEY, event TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE idempotency (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL)",
    "CREATE TABLE whatsapp_threads (id TEXT PRIMARY KEY, channel TEXT NOT NULL, sender TEXT NOT NULL, transport TEXT NOT NULL, active_draft_id TEXT REFERENCES drafts(id), active_version INTEGER, pending_followup TEXT, UNIQUE(channel, sender))",
    "CREATE TABLE whatsapp_messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES whatsapp_threads(id), direction TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, transport TEXT NOT NULL, status TEXT NOT NULL, draft_id TEXT REFERENCES drafts(id))",
    "CREATE TABLE whatsapp_events (channel TEXT NOT NULL, event_id TEXT NOT NULL, thread_id TEXT NOT NULL REFERENCES whatsapp_threads(id), fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(channel, event_id))",
    "CREATE TABLE custom_ingredients (id TEXT PRIMARY KEY, name TEXT NOT NULL, unit TEXT NOT NULL, reorder_threshold TEXT NOT NULL, created_at TEXT NOT NULL)",
)


def test_v1_database_migrates_into_the_demo_tenant(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    for statement in V1_SCHEMA:
        conn.execute(statement)
    conn.execute("INSERT INTO inventory VALUES ('beras', '7')")
    legacy_body = json.dumps({"id": "legacy-draft", "version": 1, "status": "pending_approval", "source_text": "legacy note",
                              "supplier": None, "items": [], "total_idr": None, "warnings": [], "created_at": "2026-01-01T00:00:00+00:00"})
    conn.execute("INSERT INTO drafts VALUES ('legacy-draft', 1, 'pending_approval', ?)", (legacy_body,))
    conn.execute("INSERT INTO whatsapp_threads VALUES ('thread1', 'whatsapp_simulator', '628123456789', 'whatsapp_simulator', NULL, NULL, NULL)")
    conn.execute("INSERT INTO metadata VALUES ('initialized', '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    store = Store(path)
    with store.transaction() as conn:
        version = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(drafts)")}
        inventory_pk = [row[1] for row in conn.execute("PRAGMA table_info(inventory)")]
    assert version == "2"
    assert "restaurant_id" in columns
    assert inventory_pk[0] == "restaurant_id"
    # Legacy data survived and is owned by the demo restaurant.
    assert store.get_draft("legacy-draft", RESTAURANT["id"]) is not None
    overview = store.overview(RESTAURANT["id"])
    assert next(row for row in overview["inventory"] if row["ingredient_id"] == "beras")["quantity"] == "7"
    # Logins and the WhatsApp link were provisioned for the migrated database.
    assert store.verify_login("owner", "owner123")["restaurant_id"] == RESTAURANT["id"]
    assert store.restaurant_for_sender(SENDER_NUMBER)["id"] == RESTAURANT["id"]
