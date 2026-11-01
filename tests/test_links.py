"""Multi-platform channel links: one phone may serve WhatsApp and Telegram."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, login_owner
from service.api import create_app
from service.db import RESTAURANT, Store


TENANT = RESTAURANT["id"]
OWNER_CHAT = "987654321"
BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExam"


def owner_client(tmp_path, **kwargs):
    app = create_app(tmp_path / "links.sqlite3", **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client


def add_link(client, phone, platform, label=None, expected=201):
    response = client.post("/v1/links", headers={"Idempotency-Key": uuid4().hex},
                           json={"whatsapp_number": phone, "platform": platform, "label": label})
    assert response.status_code == expected, response.text
    return response


def test_one_number_links_to_both_platforms(tmp_path):
    client = owner_client(tmp_path)
    add_link(client, OWNER_CHAT, "whatsapp", label="WA utama")
    add_link(client, OWNER_CHAT, "telegram", label="Chat owner")
    links = client.get("/v1/links").json()["data"]["links"]
    mine = {(link["phone"], link["platform"]) for link in links if link["phone"] == OWNER_CHAT}
    assert mine == {(OWNER_CHAT, "whatsapp"), (OWNER_CHAT, "telegram")}


def test_same_platform_twice_conflicts(tmp_path):
    client = owner_client(tmp_path)
    add_link(client, OWNER_CHAT, "whatsapp")
    add_link(client, OWNER_CHAT, "whatsapp", expected=409)
    # The Telegram link stays available even after the WhatsApp conflict.
    add_link(client, OWNER_CHAT, "telegram")


def test_platform_reaches_its_own_restaurant(tmp_path):
    """The same digits on WhatsApp and Telegram may belong to different restaurants."""
    client = owner_client(tmp_path)  # owner of the demo restaurant
    add_link(client, OWNER_CHAT, "whatsapp")

    admin = TestClient(client.app, base_url="http://127.0.0.1")
    login_admin(admin)
    created = admin.post("/v1/admin/restaurants", headers={"Idempotency-Key": uuid4().hex}, json={
        "name": "Kedai Lain", "owner_username": "lain", "owner_password": "password-lain",
    })
    other_id = created.json()["data"]["id"]
    admin.post(f"/v1/admin/restaurants/{other_id}/numbers", headers={"Idempotency-Key": uuid4().hex},
               json={"whatsapp_number": OWNER_CHAT, "platform": "telegram"})

    store = client.app.state.store
    assert store.restaurant_for_sender(OWNER_CHAT, "whatsapp")["id"] == TENANT
    assert store.restaurant_for_sender(OWNER_CHAT, "telegram")["id"] == other_id
    # And the cross lookup finds nothing: platforms do not leak into each other.
    assert store.restaurant_for_sender(OWNER_CHAT, "telegram") and store.restaurant_for_sender(OWNER_CHAT, "whatsapp")
    assert store.restaurant_for_sender("000000000", "whatsapp") is None


def test_links_remove_is_per_platform(tmp_path):
    client = owner_client(tmp_path)
    add_link(client, OWNER_CHAT, "whatsapp")
    add_link(client, OWNER_CHAT, "telegram")
    removed = client.delete(f"/v1/links/{OWNER_CHAT}", headers={"Idempotency-Key": uuid4().hex}, params={"platform": "whatsapp"})
    assert removed.status_code == 200, removed.text
    mine = {link["platform"] for link in client.get("/v1/links").json()["data"]["links"] if link["phone"] == OWNER_CHAT}
    assert mine == {"telegram"}
    assert client.delete(f"/v1/links/{OWNER_CHAT}", headers={"Idempotency-Key": uuid4().hex}, params={"platform": "whatsapp"}).status_code == 404


def test_invalid_platform_is_rejected(tmp_path):
    client = owner_client(tmp_path)
    response = client.post("/v1/links", headers={"Idempotency-Key": uuid4().hex}, json={"whatsapp_number": OWNER_CHAT, "platform": "signal"})
    assert response.status_code == 422


def test_proactive_sends_per_link_platform(tmp_path):
    """WhatsApp link gets no channel without Meta config; Telegram link still sends."""
    class FakeChannel:
        def __init__(self):
            self.calls = []

        async def post(self, url, *, headers=None, json=None):
            self.calls.append({"url": url, "json": json})
            return type("R", (), {"status_code": 200, "json": lambda self: {"ok": True}})()

    fake = FakeChannel()
    client = owner_client(tmp_path, telegram_bot_token=BOT_TOKEN, telegram_webhook_secret="secret-token", telegram_http_client=fake)
    add_link(client, OWNER_CHAT, "whatsapp")
    add_link(client, OWNER_CHAT, "telegram")

    result = client.post("/v1/notifications/check").json()["data"]
    by_platform = {entry["platform"]: entry for entry in result["sent"]}
    assert by_platform["whatsapp"]["ok"] is False and by_platform["whatsapp"]["channel"] is None
    assert by_platform["telegram"]["ok"] is True and by_platform["telegram"]["channel"] == "telegram"
    assert len(fake.calls) == 1 and fake.calls[0]["json"]["chat_id"] == OWNER_CHAT


def test_legacy_database_migrates_to_platform_links(tmp_path):
    """A pre-platform links table (UNIQUE phone) rebuilds with platform='whatsapp'."""
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE restaurants (id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO restaurants VALUES ('demo-resto', 'Warung Bu Sari', NULL, '2026-01-01T00:00:00+00:00')")
    conn.execute("CREATE TABLE whatsapp_links (id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL REFERENCES restaurants(id), phone TEXT NOT NULL UNIQUE, label TEXT, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO whatsapp_links VALUES ('link1', 'demo-resto', '628123456789', 'Bu Sari', '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    store = Store(path, seed=False)
    links = store.linked_numbers(TENANT)
    assert links == [{"phone": "628123456789", "platform": "whatsapp", "label": "Bu Sari", "created_at": "2026-01-01T00:00:00+00:00"}]
    # The rebuilt table accepts the same phone on a second platform.
    with store.transaction() as conn2:
        conn2.execute("INSERT INTO whatsapp_links (id, restaurant_id, phone, platform, label, created_at) VALUES ('link2', 'demo-resto', '628123456789', 'telegram', NULL, '2026-01-02T00:00:00+00:00')")
    assert len(store.linked_numbers(TENANT)) == 2
