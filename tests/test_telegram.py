"""Telegram Bot API webhook tests; Bot API calls use a local fake."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from service.api import create_app
from service.db import RESTAURANT
from service.telegram import parse_update, validate_bot_token


BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExam"
SECRET = "integration-test-secret"
CHAT_ID = "987654321"  # The owner's Telegram chat id, registered like a number.
UNREGISTERED = "111222333"
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"}


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}
        self.content = content

    def json(self):
        return self._body


class FakeTelegramClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self.responses.pop(0) if self.responses else FakeResponse()

    async def get(self, url, *, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return self.responses.pop(0)


def update(text, *, update_id=100, sender=CHAT_ID):
    message = {"message_id": update_id, "chat": {"id": int(sender)}, "date": 1700000000, "text": text}
    return {"update_id": update_id, "message": message}


def body(payload):
    return json.dumps(payload).encode()


def admin_client(tmp_path, fake=None, **kwargs):
    kwargs.setdefault("telegram_bot_token", BOT_TOKEN)
    kwargs.setdefault("telegram_webhook_secret", SECRET)
    app = create_app(tmp_path / "tg.sqlite3", telegram_http_client=fake, **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_admin(client)
    return client


def link_chat_to_demo(client, chat_id=CHAT_ID):
    """Register the chat id onto the seeded demo restaurant, on the Telegram platform."""
    response = client.post(f"/v1/admin/restaurants/{RESTAURANT['id']}/numbers", headers={"Idempotency-Key": uuid4().hex}, json={"whatsapp_number": chat_id, "platform": "telegram", "label": "Owner chat"})
    assert response.status_code == 200, response.text


# --- Transport parsing and validation ----------------------------------------


def test_parse_update_normalizes_text_and_media():
    text = parse_update(json.dumps(update("beli cabai 3 kg")).encode())
    assert text == [{"sender": CHAT_ID, "text": "beli cabai 3 kg", "event_id": "100", "num_media": 0, "media_id": None, "media_kind": None, "media_group_id": None}]
    photo = update(None, update_id=101)
    photo["message"].pop("text")
    photo["message"]["photo"] = [{"file_id": "small", "file_size": 10}, {"file_id": "LARGE", "file_size": 900}]
    parsed = parse_update(json.dumps(photo).encode())
    assert parsed[0]["media_kind"] == "image" and parsed[0]["media_id"] == "LARGE"
    assert parse_update(json.dumps({"update_id": 7}).encode()) == []  # edits/callbacks: ignore


def test_validate_bot_token_shape():
    assert validate_bot_token(BOT_TOKEN) == BOT_TOKEN
    for bad in ("", "no-colon-here", "abc:defgh", "123456789:short"):
        with pytest.raises(ValueError):
            validate_bot_token(bad)


# --- Webhook authentication ---------------------------------------------------


def test_webhook_requires_configuration(tmp_path):
    client = TestClient(create_app(tmp_path / "tg.sqlite3"), base_url="http://127.0.0.1")
    response = client.post("/webhooks/telegram", headers=HEADERS, content=body(update("halo")))
    assert response.status_code == 503


def test_webhook_rejects_a_wrong_or_missing_secret(tmp_path):
    client = admin_client(tmp_path)
    wrong = client.post("/webhooks/telegram", headers={"X-Telegram-Bot-Api-Secret-Token": "wrong", "Content-Type": "application/json"}, content=body(update("halo")))
    assert wrong.status_code == 403
    missing = client.post("/webhooks/telegram", headers={"Content-Type": "application/json"}, content=body(update("halo")))
    assert missing.status_code == 403


def test_webhook_rejects_malformed_json(tmp_path):
    client = admin_client(tmp_path)
    response = client.post("/webhooks/telegram", headers=HEADERS, content=b"not json")
    assert response.status_code == 400


# --- End to end: text purchase, dedupe, stock, unregistered --------------------


def test_registered_purchase_creates_a_draft_and_replies_once(tmp_path):
    fake = FakeTelegramClient()
    client = admin_client(tmp_path, fake)
    link_chat_to_demo(client)

    first = client.post("/webhooks/telegram", headers=HEADERS, content=body(update("beli telur 2 kg 60rb", update_id=501)))
    assert first.status_code == 200, first.text
    assert [call["url"] for call in fake.calls] == [SEND_URL]
    sent = fake.calls[0]["json"]
    assert sent["chat_id"] == CHAT_ID and "Draf pembelian" in sent["text"] and "Telur ayam" in sent["text"]

    # Telegram retries the same update_id until a 2xx: never double-send.
    replay = client.post("/webhooks/telegram", headers=HEADERS, content=body(update("beli telur 2 kg 60rb", update_id=501)))
    assert replay.status_code == 200
    assert len(fake.calls) == 1

    # A new update flows normally: YA records the draft.
    client.post("/webhooks/telegram", headers=HEADERS, content=body(update("ya", update_id=502)))
    assert "tercatat" in fake.calls[-1]["json"]["text"]


def test_stock_question_answers_from_the_inventory(tmp_path):
    fake = FakeTelegramClient()
    client = admin_client(tmp_path, fake)
    link_chat_to_demo(client)
    response = client.post("/webhooks/telegram", headers=HEADERS, content=body(update("berapa total cabai yang tersisa saat ini?", update_id=601)))
    assert response.status_code == 200, response.text
    sent = fake.calls[0]["json"]
    assert "Cabai merah — 3 kg" in sent["text"] and "5 kg" in sent["text"]


def test_unregistered_chat_gets_the_rejection_reply(tmp_path):
    fake = FakeTelegramClient()
    client = admin_client(tmp_path, fake)
    response = client.post("/webhooks/telegram", headers=HEADERS, content=body(update("beli cabai 3 kg", update_id=701, sender=UNREGISTERED)))
    assert response.status_code == 200, response.text
    sent = fake.calls[0]["json"]
    assert sent["chat_id"] == UNREGISTERED
    assert "belum terdaftar" in sent["text"]


def test_send_failure_rolls_the_status_back_and_errors(tmp_path):
    fake = FakeTelegramClient()
    fake.responses.append(FakeResponse(status_code=500, body={"ok": False}))
    client = admin_client(tmp_path, fake)
    link_chat_to_demo(client)
    response = client.post("/webhooks/telegram", headers=HEADERS, content=body(update("beli telur 2 kg", update_id=801)))
    assert response.status_code == 502
    # The outbound row returned to pending so a Telegram retry can resend it.
    with client.app.state.store.transaction() as conn:
        row = conn.execute("SELECT status FROM whatsapp_messages WHERE direction='outbound' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row["status"] == "telegram_send_pending"


def test_telegram_webhook_reachable_through_the_public_host(tmp_path):
    """The tunnel boundary lets a matching-host Telegram webhook through."""
    fake = FakeTelegramClient()
    client = TestClient(
        create_app(tmp_path / "pub.sqlite3", public_base_url="https://restopilot-test.example",
                   vision_api_key="synthetic-key", telegram_bot_token=BOT_TOKEN,
                   telegram_webhook_secret=SECRET, telegram_http_client=fake, telegram_group_seconds=0),
        base_url="https://restopilot-test.example")
    update = {"update_id": 901, "message": {"message_id": 901, "chat": {"id": 111222333}, "date": 1, "text": "halo"}}
    response = client.post("/webhooks/telegram", headers=HEADERS, content=json.dumps(update).encode())
    assert response.status_code == 200, response.text
    assert response.status_code == 200 and response.text in ("", "null")  # empty ok


def test_public_demo_opens_the_dashboard_to_the_tunnel_host(tmp_path):
    """PUBLIC_DEMO=1: non-webhook pages are reachable from the public host."""
    from service.api import create_app as _create

    update = {"update_id": 901, "message": {"message_id": 901, "chat": {"id": 111222333}, "date": 1, "text": "halo"}}
    loopback_client = TestClient(
        create_app(tmp_path / "loop.sqlite3", public_base_url="https://restopilot-test.example",
                   telegram_bot_token=BOT_TOKEN, telegram_webhook_secret=SECRET,
                   telegram_http_client=FakeTelegramClient(), telegram_group_seconds=0),
        base_url="https://restopilot-test.example")
    blocked = loopback_client.get("/")
    assert blocked.status_code == 403  # default: the dashboard stays loopback-only
    webhook_ok = loopback_client.post("/webhooks/telegram", headers=HEADERS, content=json.dumps(update).encode())
    assert webhook_ok.status_code == 200  # but the provider webhook still works over a tunnel

    public_client = TestClient(
        create_app(tmp_path / "pub.sqlite3", public_base_url="https://restopilot-test.example", public_demo=True,
                   telegram_bot_token=BOT_TOKEN, telegram_webhook_secret=SECRET,
                   telegram_http_client=FakeTelegramClient(), telegram_group_seconds=0),
        base_url="https://restopilot-test.example")
    dashboard = public_client.get("/")
    assert dashboard.status_code == 200  # login page served instead of 403
    webhook = public_client.post("/webhooks/telegram", headers=HEADERS, content=json.dumps(update).encode())
    assert webhook.status_code == 200


# --- Hosted-mode env fallback (Atoms adapter) ---------------------------------


def test_hosted_mode_reads_telegram_credentials_from_env(tmp_path, monkeypatch):
    """The Atoms adapter builds the app with an explicit settings allowlist and
    (today) does not forward TELEGRAM_* kwargs. Hosted mode must fall back to
    the process environment — the project Keys live there — or the webhook
    stays disabled no matter what the platform configures. Regression for the
    live 503 telegram_not_configured with keys present in the backend env."""
    from conftest import login_admin  # noqa: F401  (parity with other suites)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    seed_app = create_app(tmp_path / "seed.sqlite3")
    app = create_app(None, hosted=True, public_demo=True, store_factory=lambda: seed_app.state.store)
    client = TestClient(app, base_url="http://127.0.0.1")
    response = client.post("/webhooks/telegram", headers=HEADERS, content=body(update("halo")))
    assert response.status_code != 503, response.text
    assert "telegram_not_configured" not in response.text


def test_hosted_mode_stays_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    seed_app = create_app(tmp_path / "seed.sqlite3")
    app = create_app(None, hosted=True, public_demo=True, store_factory=lambda: seed_app.state.store)
    client = TestClient(app, base_url="http://127.0.0.1")
    response = client.post("/webhooks/telegram", headers=HEADERS, content=body(update("halo")))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "telegram_not_configured"
