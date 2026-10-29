"""Request-scoped provider reuse and sanitized timing, using synthetic fakes."""

import asyncio
import logging

import httpx
from fastapi.testclient import TestClient

from service import api
from test_meta_whatsapp import (
    ACCESS_TOKEN, APP_SECRET, GRAPH_VERSION, ORIGIN, PHONE_NUMBER_ID, SENDER,
    VERIFY_TOKEN, WABA_ID, FakeResponse, payload, vision_completion, webhook,
)


IMAGE = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class ScopedClient:
    def __init__(self, timeout, responses):
        self.timeout = timeout
        self.responses = responses
        self.calls = []
        self.closes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        self.closes += 1

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def application(tmp_path, **kwargs):
    return api.create_app(
        tmp_path / "latency.sqlite3", public_base_url=ORIGIN,
        meta_access_token=ACCESS_TOKEN, meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN, meta_waba_id=WABA_ID,
        meta_phone_number_id=PHONE_NUMBER_ID, meta_graph_api_version=GRAPH_VERSION,
        vision_api_key="synthetic-vision-key", **kwargs,
    )


def responses():
    return {
        30.0: [
            FakeResponse(body={"url": "https://lookaside.example/photo", "mime_type": "image/jpeg"}),
            FakeResponse(content=IMAGE), FakeResponse(),
        ],
        60.0: [
            vision_completion({"document_type": "purchase_invoice", "lines": [], "unresolved": []}),
            vision_completion({"supplier": "Synthetic supplier", "items": [{"raw_name": "Beras", "quantity": "2", "unit": "kg", "line_total_idr": 20000}], "unreadable": []}),
        ],
    }


def owned_clients(monkeypatch, queues):
    created = []

    def factory(*, timeout, follow_redirects):
        assert follow_redirects is False
        client = ScopedClient(timeout, queues[timeout])
        created.append(client)
        return client

    monkeypatch.setattr(api.httpx, "AsyncClient", factory)
    return created


def test_photo_uses_one_meta_pool_and_one_vision_pool_and_closes_both(tmp_path, monkeypatch, caplog):
    queues = responses()
    created = owned_clients(monkeypatch, queues)
    app = application(tmp_path)
    caplog.set_level(logging.INFO, logger="restopilot.api")
    body = payload(message_type="image", event_id="wamid.private-timing-event")
    body["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["caption"] = "Private supplier caption"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, body).status_code == 200
    assert len(created) == 2
    meta = next(client for client in created if client.timeout == 30)
    vision = next(client for client in created if client.timeout == 60)
    assert [call[0] for call in meta.calls] == ["GET", "GET", "POST"]
    assert [call[2]["timeout"] for call in meta.calls] == [30.0, 30.0, 5.0]
    assert len(vision.calls) == 2  # classification and invoice share the pool
    assert all(client.closes == 1 for client in created)
    timings = [record.getMessage() for record in caplog.records if record.name == "restopilot.api" and record.getMessage().startswith("WhatsApp stage=")]
    assert {line.split()[1] for line in timings} >= {"stage=media_download", "stage=document_read", "stage=store_ingestion", "stage=send", "stage=total"}
    assert all("duration_ms=" in line for line in timings)
    assert not any(secret in "\n".join(timings) for secret in (SENDER, ACCESS_TOKEN, "synthetic-vision-key", "wamid.private-timing-event", "Private supplier caption"))


def test_injected_clients_are_passed_through_and_remain_open(tmp_path, monkeypatch):
    queues = responses()
    meta, vision = ScopedClient(30, queues[30]), ScopedClient(60, queues[60])

    def unexpected(**kwargs):
        raise AssertionError("injected clients must not allocate owned clients")

    monkeypatch.setattr(api.httpx, "AsyncClient", unexpected)
    app = application(tmp_path, meta_http_client=meta, vision_http_client=vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload(message_type="image")).status_code == 200
    assert meta.closes == vision.closes == 0
    # The owned-client timeout facade must not modify caller-owned clients.
    assert all("timeout" not in kwargs for _, _, kwargs in meta.calls)


def test_owned_clients_close_and_total_is_logged_when_send_fails(tmp_path, monkeypatch, caplog):
    queues = responses()
    queues[30][-1] = httpx.ConnectError("synthetic network failure")
    created = owned_clients(monkeypatch, queues)
    app = application(tmp_path)
    caplog.set_level(logging.INFO, logger="restopilot.api")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload(message_type="image")).status_code == 502
    assert len(created) == 2 and all(client.closes == 1 for client in created)
    assert any("WhatsApp stage=send " in record.getMessage() for record in caplog.records)
    assert any("WhatsApp stage=total " in record.getMessage() for record in caplog.records)


def test_text_clarification_does_not_allocate_vision_client(tmp_path, monkeypatch):
    queues = {30.0: [FakeResponse()]}
    created = owned_clients(monkeypatch, queues)
    app = application(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload("Pakai beras 1 kg hari ini")).status_code == 200
    assert len(created) == 1 and created[0].timeout == 30
    assert created[0].closes == 1


def test_meta_pool_preserves_upload_timeout():
    underlying = ScopedClient(30, [FakeResponse()])
    client = api._MetaWebhookClient(underlying)
    asyncio.run(client.post("https://graph.facebook.com/v23.0/123456/media", files={"file": ("voice.mp3", b"voice")}))
    assert underlying.calls[0][2]["timeout"] == 30.0
