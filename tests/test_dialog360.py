"""360dialog sandbox webhook tests. Provider calls use a local fake."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.dialog360 import SANDBOX_MESSAGES_URL, sandbox_webhook_path


ORIGIN = "https://restopilot-test.example"
TOKEN = "abcdefghijklmnopqrstuvwxyz012345"
API_KEY = "synthetic-dialog360-api-key"
SENDER = "628123456789"


class FakeResponse:
    def __init__(self, status_code=201, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"messages": [{"id": "wamid.outbound"}]}

    def json(self):
        return self._body


class FakeDialog360Client:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0) if self.responses else FakeResponse()


def payload(text="beli cabai 3 kg 150rb", *, event_id=None, sender=SENDER, bsuid=False):
    message = {
        "id": event_id or "wamid." + uuid4().hex,
        "timestamp": "1591955533",
        "type": "text",
        "text": {"body": text},
    }
    message["from_user_id" if bsuid else "from"] = sender
    return {"entry": [{"changes": [{"field": "messages", "value": {"messages": [message]}}]}]}


@pytest.fixture
def provider():
    return FakeDialog360Client()


@pytest.fixture
def app(tmp_path, provider):
    return create_app(
        tmp_path / "dialog360.sqlite3",
        dialog360_api_key=API_KEY,
        dialog360_webhook_token=TOKEN,
        dialog360_http_client=provider,
        public_base_url=ORIGIN,
    )


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        yield value


def webhook(client, body, *, token=TOKEN, host="restopilot-test.example", content_type="application/json"):
    return client.post(
        sandbox_webhook_path(token),
        json=body,
        headers={"Host": host, "Content-Type": content_type},
    )


def overview(client):
    return client.get("/v1/overview").json()["data"]


def test_inbound_purchase_and_confirmation_send_real_sandbox_replies_once(client, provider, app):
    first_id = "wamid.inbound-purchase"
    first = webhook(client, payload(event_id=first_id))
    assert first.status_code == 200
    assert provider.calls[0]["url"] == SANDBOX_MESSAGES_URL
    assert provider.calls[0]["headers"] == {"Content-Type": "application/json", "D360-API-KEY": API_KEY}
    assert provider.calls[0]["json"]["to"] == SENDER
    assert "YA" in provider.calls[0]["json"]["text"]["body"]

    # A provider retry is acknowledged but cannot create or send a second reply.
    assert webhook(client, payload(event_id=first_id)).status_code == 200
    assert len(provider.calls) == 1

    assert webhook(client, payload("ya", event_id="wamid.inbound-confirmation")).status_code == 200
    assert "tercatat" in provider.calls[1]["json"]["text"]["body"]
    assert len(overview(client)["purchases"]) == 3
    with app.state.store.transaction() as conn:
        rows = conn.execute(
            "SELECT direction, status, transport FROM whatsapp_messages ORDER BY rowid"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("inbound", "received", "dialog360_sandbox"),
        ("outbound", "dialog360_sandbox_sent", "dialog360_sandbox"),
        ("inbound", "received", "dialog360_sandbox"),
        ("outbound", "dialog360_sandbox_sent", "dialog360_sandbox"),
    ]


def test_bsuid_uses_recipient_field(client, provider):
    body = payload("beli beras", event_id="wamid.bsuid", sender="business.scoped:user-123", bsuid=True)
    assert webhook(client, body).status_code == 200
    sent = provider.calls[0]["json"]
    assert sent["recipient"] == "business.scoped:user-123"
    assert "to" not in sent


def test_phone_takes_precedence_when_sandbox_supplies_both_sender_ids(client, provider):
    body = payload(event_id="wamid.dual-sender")
    message = body["entry"][0]["changes"][0]["value"]["messages"][0]
    message["from_user_id"] = "business.scoped:user-123"
    assert webhook(client, body).status_code == 200
    sent = provider.calls[0]["json"]
    assert sent["to"] == SENDER
    assert "recipient" not in sent


def test_live_sandbox_http_200_success_is_accepted(client, provider):
    provider.responses.append(FakeResponse(200))
    assert webhook(client, payload(event_id="wamid.http-200")).status_code == 200


def test_status_callback_is_acknowledged_without_creating_or_sending(client, provider):
    body = {"entry": [{"changes": [{"field": "messages", "value": {"statuses": [{"id": "wamid.outbound", "status": "read"}]}}]}]}
    before = overview(client)
    assert webhook(client, body).status_code == 200
    assert provider.calls == []
    assert overview(client) == before


def test_legacy_unwrapped_sandbox_payload_and_plus_phone_are_supported(client, provider):
    body = payload("beli bawang 2 kg 80rb", event_id="wamid.legacy", sender="+628123456789")
    legacy = body["entry"][0]["changes"][0]["value"]
    assert webhook(client, legacy).status_code == 200
    assert provider.calls[0]["json"]["to"] == SENDER
    assert "80.000" in provider.calls[0]["json"]["text"]["body"]


def test_send_failure_is_safe_and_retryable(client, provider):
    provider.responses.append(FakeResponse(502, {}))
    body = payload(event_id="wamid.retry-send")
    failed = webhook(client, body)
    assert failed.status_code == 502
    assert failed.json()["error"]["code"] == "dialog360_send_failed"
    assert API_KEY not in failed.text
    assert webhook(client, body).status_code == 200
    assert len(provider.calls) == 2


def test_webhook_rejects_wrong_boundary_and_malformed_input(client, tmp_path):
    assert webhook(client, payload(), token="x" * 24).status_code == 403
    assert webhook(client, payload(), host="evil.example").status_code == 403
    wrong_type = client.post(
        sandbox_webhook_path(TOKEN),
        content=b"not-json",
        headers={"Host": "restopilot-test.example", "Content-Type": "text/plain"},
    )
    assert wrong_type.status_code == 415
    malformed = client.post(
        sandbox_webhook_path(TOKEN),
        content=b"not-json",
        headers={"Host": "restopilot-test.example", "Content-Type": "application/json"},
    )
    assert malformed.status_code == 400
    with TestClient(create_app(tmp_path / "unconfigured.sqlite3"), base_url="http://127.0.0.1", raise_server_exceptions=False) as unconfigured:
        assert unconfigured.post(
            "/webhooks/360dialog/sandbox/" + TOKEN,
            json=payload(),
        ).status_code == 503


def test_sandbox_token_validation(tmp_path):
    for token in ("short", "contains spaces and is long enough", "x" * 129):
        with pytest.raises(ValueError, match="DIALOG360_WEBHOOK_TOKEN"):
            create_app(tmp_path / (uuid4().hex + ".sqlite3"), dialog360_webhook_token=token)
