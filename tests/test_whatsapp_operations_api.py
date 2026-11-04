"""Transport routing: operation evidence never falls through to a purchase."""

import hashlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import SENDER_NUMBER, login_owner
from service import api
from service.db import DomainError, RESTAURANT_TIMEZONE
from service.operation_extraction import clean_operations_document
from service.whatsapp import LIVE, MetaWhatsAppError, parse_webhook
from test_meta_whatsapp import (
    APP_SECRET, ACCESS_TOKEN, GRAPH_VERSION, ORIGIN, PHONE_NUMBER_ID,
    SENDER, VERIFY_TOKEN, WABA_ID, FakeMetaClient, FakeResponse, payload, webhook,
)


def application(tmp_path, provider, **kwargs):
    return api.create_app(
        tmp_path / "operations-api.sqlite3", public_base_url=ORIGIN,
        meta_access_token=ACCESS_TOKEN, meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN, meta_waba_id=WABA_ID,
        meta_phone_number_id=PHONE_NUMBER_ID, meta_graph_api_version=GRAPH_VERSION,
        meta_http_client=provider, **kwargs,
    )


def capture_operation(monkeypatch, store):
    calls = []

    def handle(conn, thread, tenant, text, **kwargs):
        calls.append({"tenant": tenant, "text": text, **kwargs})
        return {"reply": "Tinjau dulu. Stok belum berubah.", "result": {"action": "operation_review"}}

    monkeypatch.setattr(store, "_operation_handle", handle)
    return calls


def test_meta_image_caption_survives_normalization():
    body = payload(message_type="image")
    body["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["caption"] = "  Laporan penjualan kemarin  "
    import json
    message = parse_webhook(json.dumps(body).encode(), waba_id=WABA_ID, phone_number_id=PHONE_NUMBER_ID)[0]
    assert message["text"] == "Laporan penjualan kemarin"
    body["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["caption"] = {"invalid": True}
    with pytest.raises(MetaWhatsAppError, match="caption"):
        parse_webhook(json.dumps(body).encode(), waba_id=WABA_ID, phone_number_id=PHONE_NUMBER_ID)


def test_simulator_operation_skips_purchase_and_preserves_payment_followup(tmp_path, monkeypatch):
    app = application(tmp_path, FakeMetaClient())
    store = app.state.store
    calls = capture_operation(monkeypatch, store)
    candidate = {"document_type": "usage", "lines": [{"kind": "usage"}], "unresolved": []}
    monkeypatch.setattr(api, "parse_operations_text", lambda text: candidate)

    async def unexpected(*args, **kwargs):
        raise AssertionError("operation must not invoke purchase extraction")

    monkeypatch.setattr(api, "extract_message", unexpected)
    with store.transaction() as conn:
        thread = store._whatsapp_thread(conn, SENDER_NUMBER, "whatsapp_simulator", "whatsapp_simulator", restaurant_id="demo-resto", create=True)
        conn.execute("UPDATE whatsapp_threads SET pending_followup=? WHERE id=?", ("keep-purchase-followup", thread["id"]))
        before_drafts = conn.execute("SELECT count(*) FROM drafts").fetchone()[0]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login_owner(client)
        response = client.post("/v1/whatsapp/simulate", json={"sender": SENDER_NUMBER, "text": "Pakai beras 2 kg"}, headers={"Idempotency-Key": "operation-sim"})
    assert response.status_code == 200, response.text
    assert calls[0]["candidate"] == candidate
    with store.transaction() as conn:
        assert conn.execute("SELECT pending_followup FROM whatsapp_threads WHERE id=?", (thread["id"],)).fetchone()[0] == "keep-purchase-followup"
        assert conn.execute("SELECT count(*) FROM drafts").fetchone()[0] == before_drafts
        assert conn.execute("SELECT count(*) FROM inventory_events").fetchone()[0] == 0


def test_sales_image_classifier_gets_caption_and_no_purchase_extraction(tmp_path, monkeypatch):
    provider = FakeMetaClient()
    image = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    provider.responses.extend([
        FakeResponse(body={"url": "https://lookaside.example/media/report", "mime_type": "image/jpeg"}),
        FakeResponse(content=image),
    ])
    app = application(tmp_path, provider, vision_api_key="synthetic-stepfun-key")
    calls = capture_operation(monkeypatch, app.state.store)
    classified = []
    candidate = {"document_type": "sales_report", "lines": [], "unresolved": ["date"]}

    async def classify(photo, **kwargs):
        classified.append((photo, kwargs))
        return candidate

    async def unexpected(*args, **kwargs):
        raise AssertionError("sales report must not become a supplier invoice")

    monkeypatch.setattr(api, "extract_operations_document", classify)
    monkeypatch.setattr(api, "extract_invoice", unexpected)
    body = payload(message_type="image", event_id="wamid.sales-report")
    body["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["caption"] = "Penjualan kemarin"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, body).status_code == 200
    assert classified[0][0] == image
    assert classified[0][1]["caption"] == "Penjualan kemarin"
    assert calls[0]["candidate"] == candidate
    assert calls[0]["source"]["digest"] == hashlib.sha256(image).hexdigest()
    assert calls[0]["source"]["media_kind"] == "image"
    assert calls[0]["text"] == "Penjualan kemarin"


def test_active_clarification_skips_text_purchase_models(tmp_path, monkeypatch):
    provider = FakeMetaClient()
    app = application(tmp_path, provider, vision_api_key="synthetic-stepfun-key", text_llm_enabled=True)
    calls = capture_operation(monkeypatch, app.state.store)
    monkeypatch.setattr(api, "parse_operations_text", lambda text: None)
    channels = []
    monkeypatch.setattr(app.state.store, "has_active_operation", lambda sender, channel=None: channels.append((sender, channel)) or True)

    async def unexpected(*args, **kwargs):
        raise AssertionError("clarification must not invoke purchase extraction")

    monkeypatch.setattr(api, "extract_message", unexpected)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload("Satu dus 12 liter", event_id="wamid.package-answer")).status_code == 200
    assert channels == [(SENDER, LIVE + ":" + PHONE_NUMBER_ID)]
    assert calls[0]["text"] == "Satu dus 12 liter"


def test_sales_photo_date_reply_uses_saved_facts_without_another_model_call(tmp_path, monkeypatch):
    provider = FakeMetaClient()
    provider.responses.extend([
        FakeResponse(body={"url": "https://lookaside.example/media/report", "mime_type": "image/jpeg"}),
        FakeResponse(content=b"\xff\xd8\xff\xe0" + b"\x00" * 64),
    ])
    app = application(tmp_path, provider, vision_api_key="synthetic-stepfun-key", text_llm_enabled=True)
    store = app.state.store
    store.save_inventory_recipe("date-answer-recipe", {
        "name": "Nasi goreng", "items": [{"ingredient_id": "beras", "quantity": "0.1"}],
    }, "demo-resto")
    classified = []

    async def classify(photo, **kwargs):
        classified.append(photo)
        return clean_operations_document({
            "document_type": "sales_report", "business_date": None,
            "lines": [{"kind": "sale", "name": "Nasi goreng", "quantity": "2", "unit": "porsi"}],
        })

    async def unexpected(*args, **kwargs):
        raise AssertionError("date clarification must reuse the saved report without AI")

    monkeypatch.setattr(api, "extract_operations_document", classify)
    monkeypatch.setattr(api, "extract_invoice", unexpected)
    monkeypatch.setattr(api, "extract_message", unexpected)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload(message_type="image", event_id="wamid.missing-photo-date")).status_code == 200
        assert "Tanggal laporan ini kapan" in provider.calls[-1]["json"]["text"]["body"]
        assert webhook(client, payload("kemarin", event_id="wamid.photo-date-answer")).status_code == 200
    reply = provider.calls[-1]["json"]["text"]["body"]
    yesterday = (datetime.now(RESTAURANT_TIMEZONE).date() - timedelta(days=1)).isoformat()
    assert yesterday in reply and "2 × 0.1 = 0.2" in reply
    assert "Balas YA" in reply and "tidak terbaca" not in reply
    assert len(classified) == 1
    assert [call["method"] for call in provider.calls] == ["GET", "GET", "POST", "POST"]
    assert store.inventory_workspace("demo-resto")["events"] == []


def test_unregistered_media_is_not_downloaded_or_classified(tmp_path, monkeypatch):
    provider = FakeMetaClient()
    app = application(tmp_path, provider, vision_api_key="synthetic-stepfun-key", whatsapp_open_onboarding=True)

    async def unexpected(*args, **kwargs):
        raise AssertionError("onboarding must precede all media work")

    monkeypatch.setattr(api, "extract_operations_document", unexpected)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload(message_type="image", sender="628199999999", event_id="wamid.unlinked-image")).status_code == 200
    assert all(call["method"] == "POST" for call in provider.calls)


def test_unsupported_document_asks_without_download_or_purchase(tmp_path, monkeypatch):
    provider = FakeMetaClient()
    app = application(tmp_path, provider, vision_api_key="synthetic-stepfun-key")
    calls = capture_operation(monkeypatch, app.state.store)
    with app.state.store.transaction() as conn:
        before_drafts = conn.execute("SELECT count(*) FROM drafts").fetchone()[0]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload(message_type="document", event_id="wamid.unsupported-pdf")).status_code == 200
    assert calls[0]["candidate"]["document_type"] == "ambiguous"
    assert calls[0]["candidate"]["unresolved"]
    assert all(call["method"] == "POST" for call in provider.calls)
    with app.state.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM drafts").fetchone()[0] == before_drafts
        assert conn.execute("SELECT count(*) FROM inventory_events").fetchone()[0] == 0


def test_voice_operation_uses_same_routing_as_typed_text(tmp_path, monkeypatch):
    provider = FakeMetaClient()
    provider.responses.extend([
        FakeResponse(body={"url": "https://lookaside.example/media/audio", "mime_type": "audio/ogg"}),
        FakeResponse(content=b"OggS" + b"\x00" * 32),
    ])
    app = application(tmp_path, provider, vision_api_key="synthetic-stepfun-key")
    calls = capture_operation(monkeypatch, app.state.store)

    async def transcribe(*args, **kwargs):
        return "Pakai beras 2 kg"

    async def unexpected(*args, **kwargs):
        raise AssertionError("voice usage must not become a purchase")

    monkeypatch.setattr(api, "transcribe_audio", transcribe)
    monkeypatch.setattr(api, "extract_message", unexpected)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert webhook(client, payload(message_type="audio", event_id="wamid.voice-usage")).status_code == 200
    assert calls[0]["text"] == "Pakai beras 2 kg"
    assert calls[0]["candidate"]["document_type"] == "usage"
    assert calls[0]["source"]["media_kind"] == "audio"


def test_transport_retry_ignores_model_variation_and_new_digest(tmp_path, monkeypatch):
    app = application(tmp_path, FakeMetaClient())
    calls = capture_operation(monkeypatch, app.state.store)
    base = {
        "sender": SENDER_NUMBER, "text": "Laporan penjualan", "event_id": "wamid.read-retry", "num_media": 1,
        "operation_source": {"media_id": "stable-media", "media_kind": "image", "caption": "Laporan penjualan"},
        "operation_candidate": {"document_type": "ambiguous", "lines": [], "unresolved": ["unreadable"]},
    }
    first = app.state.store.whatsapp_ingest("first-read", base)
    changed = {**base, "operation_source": {**base["operation_source"], "digest": "a" * 64},
               "operation_candidate": {"document_type": "sales_report", "lines": []}}
    second = app.state.store.whatsapp_ingest("second-read", changed)
    assert second == first
    assert len(calls) == 1


def test_pre_release_provider_event_replays_after_media_metadata_added(tmp_path, monkeypatch):
    app = application(tmp_path, FakeMetaClient())
    calls = capture_operation(monkeypatch, app.state.store)
    original = {"sender": SENDER_NUMBER, "text": "", "event_id": "wamid.old-release", "num_media": 1}
    first = app.state.store.whatsapp_ingest("old-release-key", original)
    upgraded = {**original, "text": "Caption preserved by the new normalizer",
                "operation_source": {"media_id": "stable-media", "media_kind": "image", "caption": "Caption preserved by the new normalizer"},
                "operation_candidate": {"document_type": "ambiguous", "lines": [], "unresolved": ["unreadable"]}}
    # Same request key and a different request key both recover the old
    # committed callback response, rather than creating another draft/write.
    assert app.state.store.whatsapp_ingest("old-release-key", upgraded) == first
    assert app.state.store.whatsapp_ingest("new-release-key", upgraded) == first
    assert len(calls) == 1


def test_new_provider_event_rejects_changed_media_identity(tmp_path, monkeypatch):
    app = application(tmp_path, FakeMetaClient())
    calls = capture_operation(monkeypatch, app.state.store)
    original = {"sender": SENDER_NUMBER, "text": "", "event_id": "wamid.changed-media", "num_media": 1,
                "operation_source": {"media_id": "first-media", "media_kind": "image"}}
    app.state.store.whatsapp_ingest("first-media-key", original)
    changed = {**original, "operation_source": {"media_id": "different-media", "media_kind": "image"}}
    with pytest.raises(DomainError) as caught:
        app.state.store.whatsapp_ingest("different-media-key", changed)
    assert caught.value.code == "event_conflict"
    assert len(calls) == 1
