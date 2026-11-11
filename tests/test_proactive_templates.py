"""Proactive alerts respect WhatsApp's 24-hour customer-service window.

Inside the window (the owner wrote to us recently) the alert goes as a
normal free-form reply. Outside it — or if the owner never wrote at all —
WhatsApp only delivers an approved template; without one configured the
alert is recorded as proactive_template_required instead of failing.
Telegram has no window, so its routing is unchanged (covered in
test_rules.py). The demo tenant starts with no message history, which is
exactly the outside-window state, and seeded low stock for candidates.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

import pytest

from conftest import login_owner
from service.api import create_app
from service.whatsapp import MetaWhatsAppError, send_template
from test_tenant_isolation import META, FakeGraphClient

SENDER = "628123456789"  # the demo restaurant's linked number


def owner_client(tmp_path, **kwargs):
    app = create_app(tmp_path / "templates.sqlite3", **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client


def graph_posts(fake):
    return [call["json"] for call in fake.calls if call["method"] == "POST"]


def inbound_now(store, text="berapa sisa cabai?"):
    """A fresh inbound simulator message puts the sender inside the window."""
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": text})


def age_inbound_messages(store, hours=72.0):
    with store.transaction() as conn:
        conn.execute(
            "UPDATE whatsapp_messages SET created_at=? WHERE direction='inbound'",
            ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),),
        )


def test_outside_window_sends_the_approved_template(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake,
                          whatsapp_alert_template="restopilot_low_stock", whatsapp_alert_template_lang="id")
    result = client.post("/v1/notifications/check").json()["data"]
    assert result["sent"] == [{"sender": SENDER, "platform": "whatsapp", "channel": "whatsapp", "ok": True}]

    posts = graph_posts(fake)
    assert len(posts) == 1
    assert posts[0]["type"] == "template"
    assert posts[0]["template"]["name"] == "restopilot_low_stock"
    assert posts[0]["template"]["language"]["code"] == "id"
    body_param = posts[0]["template"]["components"][0]["parameters"][0]
    assert body_param["type"] == "text"
    # The template body placeholder carries the deterministic database facts.
    assert "Notifikasi stok rendah" in body_param["text"] and "Cabai merah" in body_param["text"]
    with client.app.state.store.transaction() as conn:
        status = conn.execute("SELECT status FROM whatsapp_messages WHERE id=(SELECT max(id) FROM whatsapp_messages)").fetchone()[0]
    assert status == "proactive_sent"


def test_inside_window_sends_free_form_text(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake,
                          whatsapp_alert_template="restopilot_low_stock", whatsapp_alert_template_lang="id")
    inbound_now(client.app.state.store)

    result = client.post("/v1/notifications/check").json()["data"]
    assert result["sent"][0]["ok"] is True
    posts = graph_posts(fake)
    assert len(posts) == 1
    # Inside the window: a normal reply, no template envelope.
    assert posts[0]["type"] == "text"
    assert "Notifikasi stok rendah" in posts[0]["text"]["body"]


def test_outside_window_without_template_is_recorded_not_sent(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake)

    result = client.post("/v1/notifications/check").json()["data"]
    assert result["sent"] == [{"sender": SENDER, "platform": "whatsapp", "channel": "whatsapp", "ok": False}]
    assert graph_posts(fake) == []  # no doomed send attempt
    with client.app.state.store.transaction() as conn:
        status = conn.execute("SELECT status FROM whatsapp_messages WHERE id=(SELECT max(id) FROM whatsapp_messages)").fetchone()[0]
    assert status == "proactive_template_required"


def test_dry_run_outside_window_sends_and_records_nothing(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake,
                          whatsapp_alert_template="restopilot_low_stock", whatsapp_alert_template_lang="id")
    result = client.post("/v1/notifications/check?dry_run=true").json()["data"]
    assert result["dry_run"] is True and result["candidates"] and result["sent"] == []
    assert graph_posts(fake) == []
    with client.app.state.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM whatsapp_messages").fetchone()[0] == 0


def test_telegram_link_ignores_the_whatsapp_window(tmp_path):
    fake = FakeGraphClient(content_json=None)

    class FakeTelegram:
        def __init__(self):
            self.calls = []

        async def post(self, url, *, headers=None, json=None):
            self.calls.append(json)
            return type("R", (), {"status_code": 200, "json": lambda self: {"ok": True}})()

    telegram = FakeTelegram()
    client = owner_client(tmp_path, **META, meta_http_client=fake, telegram_bot_token="123456789:AAExampleExampleExampleExampleExam",
                          telegram_webhook_secret="secret-token", telegram_http_client=telegram,
                          whatsapp_alert_template="restopilot_low_stock", whatsapp_alert_template_lang="id")
    owner = client
    owner.post("/v1/links", headers={"Idempotency-Key": uuid4().hex}, json={"whatsapp_number": "987654321", "platform": "telegram"})
    # The Telegram link has no inbound history either, but Telegram has no window.
    result = client.post("/v1/notifications/check").json()["data"]
    by_sender = {entry["sender"]: entry for entry in result["sent"]}
    assert by_sender["987654321"]["channel"] == "telegram" and by_sender["987654321"]["ok"] is True
    assert len(telegram.calls) == 1 and "Notifikasi stok rendah" in telegram.calls[0]["text"]


def test_send_template_requires_a_configured_name():
    with pytest.raises(MetaWhatsAppError) as raised:
        import asyncio
        asyncio.run(send_template("token", "222222", "v23.0", "628123456789", "  ", "id", ["facts"]))
    assert raised.value.status == 503
