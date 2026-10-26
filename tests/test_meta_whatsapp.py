"""Meta WhatsApp Cloud API webhook tests; Graph calls use a local fake."""

import asyncio
import hashlib
import hmac
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.whatsapp import (
    LIVE,
    WEBHOOK_PATH,
    MetaWhatsAppError,
    download_audio,
    download_image,
    parse_webhook,
)


ORIGIN = "https://restopilot-test.example"
ACCESS_TOKEN = "synthetic-meta-access-token"
APP_SECRET = "synthetic-meta-app-secret"
VERIFY_TOKEN = "abcdefghijklmnopqrstuvwxyz012345"
WABA_ID = "2096089503992178"
PHONE_NUMBER_ID = "123456789012345"
GRAPH_VERSION = "v23.0"
SENDER = "628123456789"
MESSAGES_URL = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
MEDIA_URL = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/media"
OGG = b"OggS" + b"\x00" * 32


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b"", headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {"messages": [{"id": "wamid.outbound"}]}
        self.content = content
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        return self._body


class FakeMetaClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers=None, json=None, files=None, data=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json, "files": files, "data": data})
        return self.responses.pop(0) if self.responses else FakeResponse()

    async def get(self, url, *, headers):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return self.responses.pop(0)


class FakeVisionClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers=None, json=None, files=None, data=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "files": files, "data": data})
        return self.responses.pop(0)


def vision_completion(payload):
    return FakeResponse(body={"choices": [{"message": {"content": json.dumps(payload)}}]})


def payload(text="beli cabai 3 kg 150rb", *, event_id=None, sender=SENDER, phone_number_id=PHONE_NUMBER_ID, waba_id=WABA_ID, message_type="text"):
    message = {
        "from": sender,
        "id": event_id or "wamid." + uuid4().hex,
        "timestamp": "1591955533",
        "type": message_type,
    }
    if message_type == "text":
        message["text"] = {"body": text}
    else:
        message[message_type] = {"id": "provider-media-id"}
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": waba_id,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15550000000", "phone_number_id": phone_number_id},
                    "messages": [message],
                },
            }],
        }],
    }


def signature(raw, secret=APP_SECRET):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


@pytest.fixture
def provider():
    return FakeMetaClient()


@pytest.fixture
def app(tmp_path, provider):
    return create_app(
        tmp_path / "meta-whatsapp.sqlite3",
        public_base_url=ORIGIN,
        meta_access_token=ACCESS_TOKEN,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        meta_waba_id=WABA_ID,
        meta_phone_number_id=PHONE_NUMBER_ID,
        meta_graph_api_version=GRAPH_VERSION,
        meta_http_client=provider,
    )


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        yield value


def webhook(client, body, *, host="restopilot-test.example", secret=APP_SECRET, content_type="application/json", query=""):
    raw = json.dumps(body, separators=(",", ":")).encode()
    return client.post(
        WEBHOOK_PATH + query,
        content=raw,
        headers={"Host": host, "Content-Type": content_type, "X-Hub-Signature-256": signature(raw, secret)},
    )


def overview(client):
    return client.get("/v1/overview").json()["data"]


def test_verification_handshake_and_public_route_boundary(client):
    response = client.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "123456789"},
        headers={"Host": "restopilot-test.example"},
    )
    assert response.status_code == 200
    assert response.text == "123456789"
    assert response.headers["content-type"].startswith("text/plain")

    wrong = client.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong-token-value", "hub.challenge": "1"},
        headers={"Host": "restopilot-test.example"},
    )
    assert wrong.status_code == 403
    assert client.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
        headers={"Host": "evil.example"},
    ).status_code == 403
    assert client.get("/v1/overview", headers={"Host": "restopilot-test.example"}).status_code == 403


def test_inbound_purchase_and_confirmation_send_graph_replies_once(client, provider, app):
    first_id = "wamid.inbound-purchase"
    first = webhook(client, payload(event_id=first_id))
    assert first.status_code == 200
    assert provider.calls[0]["url"] == MESSAGES_URL
    assert provider.calls[0]["headers"] == {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    sent = provider.calls[0]["json"]
    assert sent["messaging_product"] == "whatsapp"
    assert sent["recipient_type"] == "individual"
    assert sent["to"] == SENDER
    assert sent["text"]["preview_url"] is False
    assert "YA" in sent["text"]["body"]

    assert webhook(client, payload(event_id=first_id)).status_code == 200
    assert len(provider.calls) == 1

    assert webhook(client, payload("ya", event_id="wamid.inbound-confirmation")).status_code == 200
    assert "tercatat" in provider.calls[1]["json"]["text"]["body"]
    assert len(overview(client)["purchases"]) == 3
    with app.state.store.transaction() as conn:
        rows = conn.execute("SELECT direction, status, transport FROM whatsapp_messages ORDER BY rowid").fetchall()
    assert [tuple(row) for row in rows] == [
        ("inbound", "received", "meta_whatsapp_cloud"),
        ("outbound", "meta_sent", "meta_whatsapp_cloud"),
        ("inbound", "received", "meta_whatsapp_cloud"),
        ("outbound", "meta_sent", "meta_whatsapp_cloud"),
    ]


def test_status_callback_is_acknowledged_without_creating_or_sending(client, provider, caplog):
    body = payload()
    value = body["entry"][0]["changes"][0]["value"]
    value.pop("messages")
    value["statuses"] = [{"id": "wamid.outbound", "status": "read", "recipient_id": SENDER}]
    before = overview(client)
    assert webhook(client, body).status_code == 200
    assert provider.calls == []
    assert overview(client) == before
    assert "Meta WhatsApp delivery status: read error_codes=[]" in caplog.text


def test_media_is_not_fetched_and_gets_safe_text_reply(client, provider, app):
    body = payload(event_id="wamid.media", message_type="image")
    body["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["link"] = "http://127.0.0.1/private-secret"
    assert webhook(client, body).status_code == 200
    assert "Foto/audio" in provider.calls[0]["json"]["text"]["body"]
    with app.state.store.transaction() as conn:
        bodies = " ".join(row[0] for row in conn.execute("SELECT body FROM whatsapp_messages"))
    assert "private-secret" not in bodies


def test_audio_and_voice_messages_normalize_to_a_media_id():
    for message_type in ("audio", "voice"):
        messages = parse_webhook(json.dumps(payload(message_type=message_type)), waba_id=WABA_ID, phone_number_id=PHONE_NUMBER_ID)
        assert len(messages) == 1
        message = messages[0]
        assert message["sender"] == SENDER
        assert message["text"] == "" and message["num_media"] == 1
        assert message["media_id"] == "provider-media-id"
        assert message["media_kind"] == "audio"


def test_audio_message_with_a_bad_media_id_is_rejected():
    for message_type in ("audio", "voice"):
        body = payload(message_type=message_type)
        body["entry"][0]["changes"][0]["value"]["messages"][0][message_type]["id"] = "not a valid id!"
        with pytest.raises(MetaWhatsAppError) as error:
            parse_webhook(json.dumps(body), waba_id=WABA_ID, phone_number_id=PHONE_NUMBER_ID)
        assert error.value.status == 400
        assert "audio" in str(error.value)


def test_download_audio_accepts_documented_voice_types():
    async def scenario():
        client = FakeMetaClient()
        client.responses.extend([
            FakeResponse(body={"url": "https://lookaside.example/media/a", "mime_type": "audio/mp4"}),
            FakeResponse(content=b"\x00\x00\x00 ftypM4A " + b"\x00" * 32),
        ])
        raw, mime = await download_audio("provider-media-id", ACCESS_TOKEN, GRAPH_VERSION, client=client)
        assert mime == "audio/mp4"
        assert raw.startswith(b"\x00\x00\x00 ftyp")
    asyncio.run(scenario())


def test_download_audio_rejects_wrong_types_oversize_and_keeps_image_rules():
    async def scenario():
        client = FakeMetaClient()
        client.responses.append(FakeResponse(body={"url": "https://lookaside.example/media/b", "mime_type": "video/mp4"}))
        with pytest.raises(MetaWhatsAppError) as error:
            await download_audio("provider-media-id", ACCESS_TOKEN, GRAPH_VERSION, client=client)
        assert "unsupported audio type" in str(error.value)

        client.responses.extend([
            FakeResponse(body={"url": "https://lookaside.example/media/c", "mime_type": "audio/ogg"}),
            FakeResponse(content=b"OggS" + b"\x00" * (5 * 1024 * 1024)),
        ])
        with pytest.raises(MetaWhatsAppError) as error:
            await download_audio("provider-media-id", ACCESS_TOKEN, GRAPH_VERSION, client=client)
        assert "exceeds 5 MiB" in str(error.value)

        client.responses.append(FakeResponse(body={"url": "https://lookaside.example/media/d", "mime_type": "audio/ogg"}))
        with pytest.raises(MetaWhatsAppError) as error:
            await download_image("provider-media-id", ACCESS_TOKEN, GRAPH_VERSION, client=client)
        assert "unsupported image type" in str(error.value)
    asyncio.run(scenario())


def photo_app(tmp_path, provider, vision):
    return create_app(
        tmp_path / "meta-photo.sqlite3",
        public_base_url=ORIGIN,
        meta_access_token=ACCESS_TOKEN,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        meta_waba_id=WABA_ID,
        meta_phone_number_id=PHONE_NUMBER_ID,
        meta_graph_api_version=GRAPH_VERSION,
        meta_http_client=provider,
        vision_api_key="synthetic-stepfun-key",
        vision_http_client=vision,
    )


def test_inbound_photo_is_read_by_vision_and_becomes_a_draft(tmp_path, provider):
    vision = FakeVisionClient()
    vision.responses.append(vision_completion({
        "supplier": "Petik Segar",
        "items": [{"raw_name": "Cabai merah", "quantity": "3", "unit": "kg", "line_total_idr": 150000, "confidence": "high"}],
        "unreadable": [],
    }))
    provider.responses.extend([
        FakeResponse(body={"url": "https://lookaside.example/media/one", "mime_type": "image/jpeg"}),
        FakeResponse(content=b"\xff\xd8\xff\xe0" + b"\x00" * 64),
    ])
    app = photo_app(tmp_path, provider, vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        body = payload(event_id="wamid.photo", message_type="image")
        body["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["link"] = "http://127.0.0.1/private-secret"
        assert webhook(client, body).status_code == 200

        replies = [call for call in provider.calls if call["method"] == "POST"]
        assert "Total: Rp150.000" in replies[0]["json"]["text"]["body"]
        assert provider.calls[0]["url"].startswith(f"https://graph.facebook.com/{GRAPH_VERSION}/provider-media-id")
        assert provider.calls[1]["url"] == "https://lookaside.example/media/one"
        assert vision.calls[0]["json"]["model"] == "step-1o-turbo-vision"
        with app.state.store.transaction() as conn:
            bodies = " ".join(row[0] for row in conn.execute("SELECT body FROM whatsapp_messages"))
            draft = json.loads(conn.execute("SELECT body FROM drafts ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        assert "[Foto nota] Foto nota WhatsApp" in bodies
        assert "private-secret" not in bodies
        assert "Foto/audio belum diproses" not in bodies
        assert draft["status"] == "pending_approval"
        assert draft["supplier"] == "Petik Segar"
        assert draft["items"][0]["ingredient_id"] == "cabai"


def test_inbound_photo_with_a_failed_read_falls_back_to_the_safe_reply(tmp_path, provider):
    vision = FakeVisionClient()
    vision.responses.append(FakeResponse(status_code=500))
    provider.responses.extend([
        FakeResponse(body={"url": "https://lookaside.example/media/two", "mime_type": "image/jpeg"}),
        FakeResponse(content=b"\xff\xd8\xff\xe0" + b"\x00" * 64),
    ])
    app = photo_app(tmp_path, provider, vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.overview()["drafts"]
        assert webhook(client, payload(event_id="wamid.photo-fail", message_type="image")).status_code == 200
        replies = [call for call in provider.calls if call["method"] == "POST"]
        assert "Foto/audio belum diproses" in replies[0]["json"]["text"]["body"]
        assert app.state.store.overview()["drafts"] == before


def test_send_failure_is_safe_and_retryable(client, provider):
    provider.responses.append(FakeResponse(502, {"error": {"message": ACCESS_TOKEN}}))
    body = payload(event_id="wamid.retry-send")
    failed = webhook(client, body)
    assert failed.status_code == 502
    assert failed.json()["error"]["code"] == "meta_send_failed"
    assert ACCESS_TOKEN not in failed.text
    assert webhook(client, body).status_code == 200
    assert len(provider.calls) == 2


def test_signature_ids_content_type_and_query_are_rejected(client, provider):
    body = payload()
    assert webhook(client, body, secret="wrong-app-secret").status_code == 403
    assert provider.calls == []
    assert webhook(client, payload(waba_id="9999999999999999")).status_code == 403
    assert webhook(client, payload(phone_number_id="999999999999999")).status_code == 403
    assert webhook(client, body, content_type="text/plain").status_code == 415
    assert webhook(client, body, query="?unexpected=value").status_code == 400


def test_malformed_envelope_and_unconfigured_app_are_rejected(client, tmp_path):
    raw = b"not-json"
    malformed = client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={"Host": "restopilot-test.example", "Content-Type": "application/json", "X-Hub-Signature-256": signature(raw)},
    )
    assert malformed.status_code == 400
    assert webhook(client, {"object": "page", "entry": []}).status_code == 400
    with TestClient(create_app(tmp_path / "unconfigured.sqlite3"), base_url="http://127.0.0.1", raise_server_exceptions=False) as unconfigured:
        assert unconfigured.post(WEBHOOK_PATH, json={}).status_code == 503
        assert unconfigured.get(WEBHOOK_PATH).status_code == 503


def test_meta_configuration_validation(tmp_path):
    with pytest.raises(ValueError, match="META_WABA_ID"):
        create_app(tmp_path / "bad-waba.sqlite3", meta_waba_id="not-an-id")
    with pytest.raises(ValueError, match="META_PHONE_NUMBER_ID"):
        create_app(tmp_path / "bad-phone.sqlite3", meta_phone_number_id="123")
    with pytest.raises(ValueError, match="META_WEBHOOK_VERIFY_TOKEN"):
        create_app(tmp_path / "bad-token.sqlite3", meta_verify_token="short")
    with pytest.raises(ValueError, match="META_GRAPH_API_VERSION"):
        create_app(tmp_path / "bad-version.sqlite3", meta_graph_api_version="latest")


def voice_app(tmp_path, provider, vision, **kwargs):
    return create_app(
        tmp_path / "meta-voice.sqlite3",
        public_base_url=ORIGIN,
        meta_access_token=ACCESS_TOKEN,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        meta_waba_id=WABA_ID,
        meta_phone_number_id=PHONE_NUMBER_ID,
        meta_graph_api_version=GRAPH_VERSION,
        meta_http_client=provider,
        vision_api_key="synthetic-stepfun-key",
        vision_http_client=vision,
        **kwargs,
    )


def summary_app(tmp_path, provider, vision=None, seed=True, **kwargs):
    return create_app(
        tmp_path / "meta-summary.sqlite3",
        public_base_url=ORIGIN,
        meta_access_token=ACCESS_TOKEN,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        meta_waba_id=WABA_ID,
        meta_phone_number_id=PHONE_NUMBER_ID,
        meta_graph_api_version=GRAPH_VERSION,
        meta_http_client=provider,
        seed=seed,
        vision_api_key="synthetic-stepfun-key" if vision else None,
        vision_http_client=vision,
        **kwargs,
    )


def test_inbound_voice_note_is_transcribed_and_becomes_a_draft(tmp_path, provider):
    vision = FakeVisionClient()
    vision.responses.append(FakeResponse(body={"text": "  beli cabai 3 kg 150rb  "}))
    provider.responses.extend([
        FakeResponse(body={"url": "https://lookaside.example/media/voice", "mime_type": "audio/ogg"}),
        FakeResponse(content=OGG),
    ])
    app = voice_app(tmp_path, provider, vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload(event_id="wamid.voice", message_type="audio")).status_code == 200

        assert provider.calls[0]["url"].startswith(f"https://graph.facebook.com/{GRAPH_VERSION}/provider-media-id")
        asr = vision.calls[0]
        assert asr["data"] == {"model": "step-asr-1.1", "response_format": "json"}
        assert asr["files"]["file"] == ("voice.ogg", OGG, "audio/ogg")
        replies = [call for call in provider.calls if call["method"] == "POST" and call["url"] == MESSAGES_URL]
        assert "Total: Rp150.000" in replies[0]["json"]["text"]["body"]

        with app.state.store.transaction() as conn:
            bodies = " ".join(row[0] for row in conn.execute("SELECT body FROM whatsapp_messages"))
            draft = json.loads(conn.execute("SELECT body FROM drafts ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        assert "[Catatan suara] beli cabai 3 kg 150rb" in bodies
        assert "Foto/audio belum diproses" not in bodies
        assert draft["status"] == "pending_approval"
        assert draft["source_text"] == "beli cabai 3 kg 150rb"
        assert draft["items"][0]["ingredient_id"] == "cabai"


def test_voice_retry_with_a_different_transcript_does_not_conflict(tmp_path, provider):
    vision = FakeVisionClient()
    vision.responses.extend([
        FakeResponse(body={"text": "beli cabai 3 kg 150rb"}),
        FakeResponse(body={"text": "ASR heard something else entirely"}),
    ])
    provider.responses.extend([
        FakeResponse(body={"url": "https://lookaside.example/media/voice", "mime_type": "audio/ogg"}),
        FakeResponse(content=OGG),
        FakeResponse(),
        FakeResponse(body={"url": "https://lookaside.example/media/voice", "mime_type": "audio/ogg"}),
        FakeResponse(content=OGG),
    ])
    app = voice_app(tmp_path, provider, vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        event_id = "wamid.voice-retry"
        first = webhook(client, payload(event_id=event_id, message_type="voice"))
        retry = webhook(client, payload(event_id=event_id, message_type="voice"))
        assert first.status_code == retry.status_code == 200
        replies = [call for call in provider.calls if call["method"] == "POST" and call["url"] == MESSAGES_URL]
        assert len(replies) == 1
        with app.state.store.transaction() as conn:
            assert conn.execute("SELECT count(*) FROM drafts").fetchone()[0] == 4
            assert conn.execute("SELECT count(*) FROM whatsapp_events").fetchone()[0] == 1


def test_ingest_fingerprint_ignores_a_flapping_transcript(app):
    store = app.state.store
    base = {"sender": SENDER, "text": "", "event_id": "evt.voice-flap", "num_media": 1}
    channel = LIVE + ":" + PHONE_NUMBER_ID
    first = store.whatsapp_ingest("key-one", {**base, "transcript": "Cabai 3 kg 150rb"}, transport=LIVE, channel=channel)
    assert first["result"]["action"] == "draft_created"
    # The same Meta event redelivered with a different ASR result must replay
    # the stored response, never raise event_conflict (500 -> retry loop).
    retry = store.whatsapp_ingest("key-two", {**base, "transcript": "A completely different ASR flap"}, transport=LIVE, channel=channel)
    assert retry == first
    assert store.overview()["metrics"]["recorded_purchases"] == 2


def test_voice_note_without_asr_gets_the_safe_reply(client, provider, app):
    assert webhook(client, payload(event_id="wamid.voice-noasr", message_type="voice")).status_code == 200
    assert provider.calls[0]["url"] == MESSAGES_URL
    assert "Foto/audio" in provider.calls[0]["json"]["text"]["body"]
    with app.state.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM drafts").fetchone()[0] == 3


def test_voice_note_with_a_failed_transcription_falls_back_to_the_safe_reply(tmp_path, provider):
    vision = FakeVisionClient()
    vision.responses.append(FakeResponse(status_code=500))
    provider.responses.extend([
        FakeResponse(body={"url": "https://lookaside.example/media/voice", "mime_type": "audio/ogg"}),
        FakeResponse(content=OGG),
    ])
    app = voice_app(tmp_path, provider, vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.overview()["drafts"]
        assert webhook(client, payload(event_id="wamid.voice-fail", message_type="audio")).status_code == 200
        replies = [call for call in provider.calls if call["method"] == "POST"]
        assert "Foto/audio belum diproses" in replies[0]["json"]["text"]["body"]
        assert app.state.store.overview()["drafts"] == before


def test_ringkas_replies_with_the_zeroed_text_summary_without_tts(tmp_path, provider):
    app = summary_app(tmp_path, provider, seed=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload("Ringkas", event_id="wamid.ringkas-empty")).status_code == 200
        assert provider.calls[0]["url"] == MESSAGES_URL
        assert provider.calls[0]["json"]["text"]["body"] == "Ringkasan 7 hari: 0 pembelian, total Rp0."
        assert app.state.store.overview()["drafts"] == []


def test_ringkas_summarizes_purchases_and_the_biggest_price_spike(tmp_path, provider):
    app = summary_app(tmp_path, provider)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        draft = next(row for row in app.state.store.overview()["drafts"] if row["status"] == "pending_approval")
        approved = client.post(
            f"/v1/drafts/{draft['id']}/decision",
            headers={"Idempotency-Key": uuid4().hex},
            json={"expected_version": draft["version"], "decision": "approve", "received": True},
        )
        assert approved.status_code == 200
        assert webhook(client, payload("ringkasan", event_id="wamid.ringkas-spike")).status_code == 200
        assert provider.calls[0]["json"]["text"]["body"] == (
            "Ringkasan 7 hari: 3 pembelian, total Rp709.000. Terbanyak: Telur ayam 13 kg. Lonjakan harga terbesar: Telur ayam +16,3%."
        )


def test_ringkas_leaves_an_active_draft_untouched(tmp_path, provider):
    app = summary_app(tmp_path, provider, seed=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload("Cabai 3 kg 150rb", event_id="wamid.draft")).status_code == 200
        assert webhook(client, payload("ringkas", event_id="wamid.ringkas-draft")).status_code == 200
        assert provider.calls[1]["json"]["text"]["body"].startswith("Ringkasan 7 hari: 0 pembelian")
        assert webhook(client, payload("ya", event_id="wamid.approve")).status_code == 200
        assert "tercatat" in provider.calls[2]["json"]["text"]["body"]
        assert webhook(client, payload("summary", event_id="wamid.ringkas-after")).status_code == 200
        assert provider.calls[3]["json"]["text"]["body"] == "Ringkasan 7 hari: 1 pembelian, total Rp150.000. Terbanyak: Cabai merah 3 kg."


def test_ringkas_replies_with_a_voice_note_when_tts_and_upload_succeed(tmp_path, provider):
    vision = FakeVisionClient()
    vision.responses.append(FakeResponse(content=b"ID3summary-audio", headers={"content-type": "audio/mpeg"}))
    provider.responses.append(FakeResponse(body={"id": "synthetic-audio-media-id"}))
    app = summary_app(tmp_path, provider, vision=vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload("ringkas", event_id="wamid.ringkas-voice")).status_code == 200

        tts = vision.calls[0]
        assert tts["json"]["model"] == "step-tts-2"
        assert tts["json"]["voice"] == "linjiameimei"
        assert tts["json"]["response_format"] == "mp3"
        assert tts["json"]["input"] == "Ringkasan 7 hari: 2 pembelian, total Rp559.000. Terbanyak: Telur ayam 10 kg."

        upload = provider.calls[0]
        assert upload["url"] == MEDIA_URL
        assert upload["data"] == {"messaging_product": "whatsapp", "type": "audio"}
        assert upload["files"]["file"] == ("audio.mp3", b"ID3summary-audio", "audio/mpeg")
        audio_send = provider.calls[1]
        assert audio_send["url"] == MESSAGES_URL
        assert audio_send["json"]["type"] == "audio"
        assert audio_send["json"]["audio"] == {"id": "synthetic-audio-media-id"}
        assert audio_send["json"]["to"] == SENDER

        with app.state.store.transaction() as conn:
            row = conn.execute("SELECT direction, status, body FROM whatsapp_messages ORDER BY rowid DESC LIMIT 1").fetchone()
        assert row["status"] == "meta_sent" and row["body"].startswith("Ringkasan 7 hari: 2 pembelian")


def test_ringkas_falls_back_to_the_text_summary_when_tts_fails(tmp_path, provider):
    vision = FakeVisionClient()
    vision.responses.append(FakeResponse(status_code=500, body={"error": {"message": "synthesis down"}}))
    app = summary_app(tmp_path, provider, vision=vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload("Ringkas", event_id="wamid.ringkas-tts-fail")).status_code == 200
        assert [call["url"] for call in provider.calls] == [MESSAGES_URL]
        assert provider.calls[0]["json"]["text"]["body"].startswith("Ringkasan 7 hari: 2 pembelian")
        assert "synthesis down" not in provider.calls[0]["json"]["text"]["body"]


def test_ringkas_falls_back_to_the_text_summary_when_upload_fails(tmp_path, provider):
    vision = FakeVisionClient()
    vision.responses.append(FakeResponse(content=b"ID3summary-audio", headers={"content-type": "audio/mpeg"}))
    provider.responses.append(FakeResponse(status_code=500, body={"error": {"message": "upload rejected"}}))
    app = summary_app(tmp_path, provider, vision=vision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload("Ringkas", event_id="wamid.ringkas-upload-fail")).status_code == 200
        assert [call["url"] for call in provider.calls] == [MEDIA_URL, MESSAGES_URL]
        assert provider.calls[1]["json"]["type"] == "text"
        assert provider.calls[1]["json"]["text"]["body"].startswith("Ringkasan 7 hari: 2 pembelian")
