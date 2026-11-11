"""Per-restaurant agent rules and proactive low-stock alerts."""

from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, login_owner
from service.api import create_app
from service.db import RESTAURANT, Store
from service.db import DomainError
from service.notifications import build_alert_text
from service.rules import effective_directives, parse_rule


TENANT = RESTAURANT["id"]
BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExam"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}

    def json(self):
        return self._body


class FakeChannel:
    def __init__(self):
        self.calls = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"url": url, "json": json})
        return FakeResponse()


def catalog_for(store):
    return store.catalog_list(TENANT)


# --- Deterministic rule parsing ----------------------------------------------


def test_parse_rule_reads_threshold_overrides():
    catalog = [{"id": "cabai", "name": "Cabai merah", "unit": "kg"}, {"id": "beras", "name": "Beras", "unit": "kg"}]
    parsed = parse_rule("kabari kalau cabai di bawah 3 kg", catalog)
    assert parsed == {"kind": "threshold", "ingredient_id": "cabai", "threshold": "3"}
    assert parse_rule("Pantau beras kurang dari 10", catalog)["threshold"] == "10"
    assert parse_rule("kabari kalau beras di bawah 2,5 kg", catalog)["threshold"] == "2.5"


def test_parse_rule_reads_mutes_and_rejects_the_rest():
    catalog = [{"id": "telur", "name": "Telur ayam", "unit": "kg"}, {"id": "cabai", "name": "Cabai merah", "unit": "kg"}]
    assert parse_rule("jangan ingatkan stok telur", catalog) == {"kind": "mute", "ingredient_id": "telur"}
    # No ingredient named: free-text context only.
    assert parse_rule("selalu sapa dengan hangat", catalog) is None
    # Ingredient but no readable threshold: free-text context only.
    assert parse_rule("cabai penting banget", catalog) is None
    assert parse_rule("", catalog) is None


def test_effective_directives_merge_in_order_and_skip_disabled():
    catalog = [{"id": "cabai", "name": "Cabai merah", "unit": "kg"}, {"id": "telur", "name": "Telur ayam", "unit": "kg"}]
    rules = [
        {"rule": "kabari kalau cabai di bawah 3", "enabled": True},
        {"rule": "kabari kalau cabai di bawah 4", "enabled": True},  # later wins
        {"rule": "jangan ingatkan stok telur", "enabled": True},
        {"rule": "kabari kalau telur di bawah 2", "enabled": False},  # disabled: ignored
    ]
    overrides, muted = effective_directives(rules, catalog)
    assert overrides == {"cabai": "4"}
    assert muted == {"telur"}


# --- Store rules CRUD and tenant isolation ------------------------------------


def test_rules_crud_scoped_to_the_restaurant(tmp_path):
    store = Store(tmp_path / "rules.sqlite3")
    created = store.add_rule(TENANT, "kabari kalau cabai di bawah 3 kg")
    assert created["enabled"] is True
    assert [rule["rule"] for rule in store.list_rules(TENANT)] == ["kabari kalau cabai di bawah 3 kg"]
    updated = store.set_rule(TENANT, created["id"], enabled=False)
    assert updated["enabled"] is False
    edited = store.set_rule(TENANT, created["id"], rule_text="kabari kalau cabai di bawah 4 kg")
    assert edited["rule"].endswith("4 kg")

    # Another restaurant neither sees nor can touch this rule.
    assert store.list_rules("other-resto") == []
    with pytest.raises(DomainError):
        store.set_rule("other-resto", created["id"], enabled=True)
    with pytest.raises(DomainError):
        store.delete_rule("other-resto", created["id"])
    assert store.delete_rule(TENANT, created["id"]) == {"ok": True}


def test_low_stock_candidates_honor_overrides(tmp_path):
    store = Store(tmp_path / "rules.sqlite3")
    # Seed: cabai 3 kg against a 5 kg threshold is already low.
    assert any(item["ingredient_id"] == "cabai" for item in store.low_stock_candidates(TENANT))
    # A rule lowering the threshold below the current stock silences it.
    overrides, _muted = effective_directives([{"rule": "kabari kalau cabai di bawah 1 kg", "enabled": True}], catalog_for(store))
    assert not any(item["ingredient_id"] == "cabai" for item in store.low_stock_candidates(TENANT, overrides))
    # A rule raising it keeps it alerting with the rule threshold.
    overrides, _muted = effective_directives([{"rule": "kabari kalau cabai di bawah 10 kg", "enabled": True}], catalog_for(store))
    cabai = next(item for item in store.low_stock_candidates(TENANT, overrides) if item["ingredient_id"] == "cabai")
    assert cabai["threshold"] == "10" and cabai["quantity"] == "3"


def test_parse_rule_threshold_with_minimum_wording():
    catalog = [{"id": "cabai", "name": "Cabai merah", "unit": "kg"}]
    for text in ("kabari kalau cabai minimum 10", "cabai minimal 8 pcs", "kabari kalau cabai di bawah 3 kg"):
        parsed = parse_rule(text, catalog)
        assert parsed and parsed["kind"] == "threshold", text


def test_refill_plan_text_with_estimates():
    from service.notifications import build_refill_plan_text, refill_suggestion
    assert refill_suggestion("8", "20") == "12"
    assert refill_suggestion("20", "20") is None
    assert refill_suggestion("abc", "20") is None
    facts = [
        {"name": "Pandan", "unit": "pcs", "suggest_qty": "12", "suggest_est_idr": 120000},
        {"name": "Choco", "unit": "pcs", "suggest_qty": "9", "suggest_est_idr": 95000},
        {"name": "Keju", "unit": "pcs", "suggest_qty": "4"},
    ]
    plan = build_refill_plan_text(facts)
    assert "Rencana belanja besok" in plan
    assert "beli ±12 pcs (~Rp120.000)" in plan
    assert "beli ±4 pcs (harga belum tercatat)" in plan
    assert "Estimasi modal: Rp215.000" in plan
    assert build_refill_plan_text([{"name": "Gula", "unit": "kg"}]) is None


def test_low_stock_candidates_suggest_refill_with_last_price(tmp_path):
    from service.db import encode, now
    store = Store(tmp_path / "r8.sqlite3")
    # Seed has cabai at 3 kg against a 5 kg threshold; the owner's rule raises
    # the effective threshold to 20 (same path chat thresholds take).
    overrides = {"cabai": "20"}
    with store.transaction() as conn:
        draft_id = uuid4().hex
        conn.execute("INSERT INTO drafts (id, version, status, body, restaurant_id) VALUES (?, 1, 'recorded', ?, ?)",
                     (draft_id, encode({"items": [], "warnings": []}), TENANT))
        purchase = {"items": [{"ingredient_id": "cabai", "quantity": "10", "unit": "kg",
                               "unit_price_idr": 10000, "line_total_idr": 100000}]}
        conn.execute("INSERT INTO purchases (id, draft_id, supplier, created_at, body, restaurant_id) VALUES (?,?,?,?,?,?)",
                     (uuid4().hex, draft_id, "Dika", now(), encode(purchase), TENANT))
    candidates = store.low_stock_candidates(TENANT, overrides)
    cabai = next(item for item in candidates if item["ingredient_id"] == "cabai")
    assert cabai["suggest_qty"] == "17"
    assert cabai["suggest_est_idr"] == 170000
    # An ingredient without purchase history still gets a quantity suggestion, no estimate.
    with store.transaction() as conn:
        conn.execute("INSERT INTO custom_ingredients (id, name, unit, reorder_threshold, created_at, restaurant_id) VALUES ('choco', 'Choco', 'pcs', '9', ?, ?)", (now(), TENANT))
        conn.execute("INSERT INTO inventory (restaurant_id, ingredient_id, quantity) VALUES (?, 'choco', '2')", (TENANT,))
    store._load_custom_ingredients()  # reload the catalog cache like a chat-created ingredient would
    candidates = store.low_stock_candidates(TENANT, overrides)
    choco = next(item for item in candidates if item["ingredient_id"] == "choco")
    assert choco["suggest_qty"] == "7"
    assert "suggest_est_idr" not in choco


def test_proactive_cooldown_quiets_repeats_until_quantity_changes(tmp_path):
    store = Store(tmp_path / "rules.sqlite3")
    first = store.low_stock_candidates(TENANT)
    assert first, "the seed starts with low stock"
    store.mark_proactive_alerts(TENANT, first)
    assert store.low_stock_candidates(TENANT) == []  # just alerted: stay quiet

    with store.transaction() as conn:
        conn.execute("UPDATE inventory SET quantity='2' WHERE restaurant_id=? AND ingredient_id='cabai'", (TENANT,))
    assert store.low_stock_candidates(TENANT) == []  # changed but still inside the cooldown

    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    with store.transaction() as conn:
        conn.execute("UPDATE proactive_alerts SET last_alerted_at=? WHERE restaurant_id=?", (stale, TENANT))
    candidates = store.low_stock_candidates(TENANT)
    assert any(item["ingredient_id"] == "cabai" and item["quantity"] == "2" for item in candidates)


# --- API: rules endpoints and the proactive check ------------------------------


def rules_api(tmp_path, **kwargs):
    app = create_app(tmp_path / "api.sqlite3", **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client


def test_rules_endpoints_round_trip(tmp_path):
    client = rules_api(tmp_path)
    created = client.post("/v1/rules", json={"rule": "kabari kalau cabai di bawah 3 kg"})
    assert created.status_code == 201, created.text
    rule_id = created.json()["data"]["id"]
    listed = client.get("/v1/rules").json()["data"]["rules"]
    assert [rule["id"] for rule in listed] == [rule_id]
    assert client.patch(f"/v1/rules/{rule_id}", json={"enabled": False}).json()["data"]["enabled"] is False
    assert client.patch(f"/v1/rules/{rule_id}", json={}).status_code == 422
    assert client.delete(f"/v1/rules/{rule_id}").json()["data"]["ok"] is True
    assert client.get("/v1/rules").json()["data"]["rules"] == []
    assert client.delete(f"/v1/rules/{rule_id}").status_code == 404


def test_rules_are_isolated_between_restaurants(tmp_path):
    client = rules_api(tmp_path)
    admin = TestClient(client.app, base_url="http://127.0.0.1")
    login_admin(admin)
    created = admin.post("/v1/admin/restaurants", headers={"Idempotency-Key": uuid4().hex}, json={
        "name": "Kedai Kedua", "owner_username": "kedua", "owner_password": "password-kedua",
    })
    assert created.status_code == 201, created.text

    rule_id = client.post("/v1/rules", json={"rule": "hanya untuk resto pertama"}).json()["data"]["id"]
    other = TestClient(client.app, base_url="http://127.0.0.1")
    other.post("/v1/auth/login", json={"username": "kedua", "password": "password-kedua"})
    assert other.get("/v1/rules").json()["data"]["rules"] == []
    assert other.patch(f"/v1/rules/{rule_id}", json={"enabled": True}).status_code == 404
    assert other.delete(f"/v1/rules/{rule_id}").status_code == 404


def test_proactive_check_dry_run_builds_but_never_records(tmp_path):
    client = rules_api(tmp_path)
    before = client.get("/v1/overview").json()["data"]["activity"]
    result = client.post("/v1/notifications/check?dry_run=true").json()["data"]
    assert result["dry_run"] is True and result["candidates"]
    assert "Cabai merah — 3 kg" in result["body"] and "ambang 5 kg" in result["body"]
    assert result["sent"] == []
    after = client.get("/v1/overview").json()["data"]["activity"]
    assert len(after) == len(before)  # dry run leaves no trace


def test_proactive_check_sends_via_telegram_then_goes_quiet(tmp_path):
    fake = FakeChannel()
    client = rules_api(tmp_path, telegram_bot_token=BOT_TOKEN,
                       telegram_webhook_secret="secret-token", telegram_http_client=fake)
    admin = TestClient(client.app, base_url="http://127.0.0.1")
    login_admin(admin)
    admin.post(f"/v1/admin/restaurants/{TENANT}/numbers", headers={"Idempotency-Key": uuid4().hex},
               json={"whatsapp_number": "987654321", "platform": "telegram", "label": "Owner chat"})

    result = client.post("/v1/notifications/check").json()["data"]
    assert result["dry_run"] is False
    # Routing follows each link's platform: the seeded WhatsApp number has no
    # Meta configuration here (recorded, not sent), the Telegram link sends.
    by_sender = {entry["sender"]: entry for entry in result["sent"]}
    assert set(by_sender) == {"628123456789", "987654321"}
    assert by_sender["628123456789"]["channel"] is None and by_sender["628123456789"]["ok"] is False
    assert by_sender["987654321"]["channel"] == "telegram" and by_sender["987654321"]["ok"] is True
    assert "Notifikasi stok rendah" in result["body"]
    assert len(fake.calls) == 1 and fake.calls[0]["json"]["chat_id"] == "987654321"

    # The sweep just alerted everything: an immediate second check stays quiet.
    quiet = client.post("/v1/notifications/check").json()["data"]
    assert quiet["candidates"] == [] and quiet["body"] is None
    assert len(fake.calls) == 1  # no new sends on the quiet check


def test_a_fabricated_llm_alert_falls_back_to_the_database_text(tmp_path):
    class FakeLLM:
        async def post(self, url, *, headers=None, json=None):
            return FakeResponse(200, {"choices": [{"message": {"content": "Stok cabai 30 kg, aman!"}}]})

    client = rules_api(tmp_path, vision_api_key="synthetic-key", stock_llm_enabled=True, stock_llm_http_client=FakeLLM())
    result = client.post("/v1/notifications/check?dry_run=true").json()["data"]
    assert "Cabai merah — 3 kg" in result["body"] and "30 kg" not in result["body"]


def test_build_alert_text_prints_database_facts():
    text = build_alert_text([
        {"ingredient_id": "cabai", "name": "Cabai merah", "quantity": "3", "unit": "kg", "threshold": "5"},
        {"ingredient_id": "beras", "name": "Beras", "quantity": "12", "unit": "kg", "threshold": "20"},
    ])
    assert text == (
        "⚠️ Notifikasi stok rendah:\n"
        "• Cabai merah — 3 kg (ambang 5 kg)\n"
        "• Beras — 12 kg (ambang 20 kg)\n"
        "Cek dashboard dan balas YA untuk mencatat pembelian."
    )
