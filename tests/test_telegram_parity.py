"""Telegram parity: photo captions feed purchase text, summaries can be voice.

Two gaps against the WhatsApp channel: a Telegram photo's caption (the
sender's own words typed with the photo) was thrown away, and the weekly
summary could only be spoken on WhatsApp. Captions now ride along like
WhatsApp image captions and reach the vision prompt as untrusted data;
the weekly summary goes out as a voice note when StepFun TTS is
configured, degrading to text on any problem.
"""
import json
import json as jsonlib
from uuid import uuid4

from fastapi.testclient import TestClient

from conftest import login_admin, login_owner
from service.api import create_app
from service.db import RESTAURANT
from service.telegram import TelegramError, parse_update

TENANT = RESTAURANT["id"]
BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExam"
SECRET = "parity-secret"
CHAT_ID = "987654321"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
NOTA = {"supplier": "Pasar A", "items": [{"raw_name": "Cabai Merah", "quantity": "2", "unit": "kg", "line_total_idr": 100000}], "unreadable": []}


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}
        self.content = content

    def json(self):
        return self._body


class FakeTelegramAPI:
    """Records every call; sendVoice arrives as data+files, not json."""

    def __init__(self, file_bytes=JPEG):
        self.calls = []
        self.file_bytes = file_bytes

    async def post(self, url, *, headers=None, json=None, data=None, files=None):
        self.calls.append({"url": url, "json": json, "data": data, "files": files})
        return FakeResponse()

    async def get(self, url, *, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        if "/file/bot" in url:
            return FakeResponse(body={"ok": True}, content=self.file_bytes)
        return FakeResponse(body={"ok": True, "result": {"file_path": "notas/nota.jpg"}})


class FakeVisionClient:
    def __init__(self, completion):
        self.completion = completion
        self.prompts = []

    async def post(self, url, *, headers=None, json=None):
        self.prompts.append(json["messages"][0]["content"][0]["text"])
        return FakeResponse(body={"choices": [{"message": {"content": jsonlib.dumps(self.completion)}}]})


def client_with(tmp_path, *, telegram=None, vision=None, vision_key=None, **kwargs):
    app = create_app(tmp_path / "parity.sqlite3", seed=False,
                     telegram_bot_token=BOT_TOKEN, telegram_webhook_secret=SECRET,
                     telegram_http_client=telegram or FakeTelegramAPI(),
                     vision_api_key=vision_key, vision_http_client=vision, **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    admin = TestClient(app, base_url="http://127.0.0.1")
    login_admin(admin)
    admin.post(f"/v1/admin/restaurants/{TENANT}/numbers", headers={"Idempotency-Key": uuid4().hex},
               json={"whatsapp_number": CHAT_ID, "platform": "telegram", "label": "Owner chat"})
    return client, app


def post_update(client, update):
    return client.post("/webhooks/telegram", headers=HEADERS, content=json.dumps(update).encode())


def photo_update(update_id=200, caption=None):
    message = {"message_id": update_id, "chat": {"id": int(CHAT_ID)}, "date": 1700000000,
               "photo": [{"file_id": f"file-{update_id}", "file_size": 900}]}
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


# --- Captions -----------------------------------------------------------------


def test_parse_update_photo_caption_becomes_the_text():
    parsed = parse_update(json.dumps(photo_update(caption="beli cabai 2 kg 100rb dari pasar pagi")).encode())
    assert parsed[0]["text"] == "beli cabai 2 kg 100rb dari pasar pagi"
    assert parsed[0]["media_kind"] == "image" and parsed[0]["num_media"] == 1
    assert parse_update(json.dumps(photo_update()).encode())[0]["text"] == ""


def test_parse_update_rejects_an_oversized_caption_and_ignores_pdf_captions():
    assert parse_update(json.dumps(photo_update(caption="x" * 10_001)).encode())[0]["text"] == ""
    document = {"update_id": 300, "message": {"message_id": 300, "chat": {"id": int(CHAT_ID)}, "date": 1700000000,
                                               "caption": "should stay unused like WhatsApp PDF captions",
                                               "document": {"file_id": "pdf-1", "mime_type": "application/pdf"}}}
    parsed = parse_update(json.dumps(document).encode())
    assert parsed[0]["text"] == "" and parsed[0]["media_kind"] == "pdf"


def test_photo_caption_reaches_the_vision_prompt_and_creates_the_draft(tmp_path):
    telegram = FakeTelegramAPI()
    vision = FakeVisionClient(NOTA)
    client, app = client_with(tmp_path, telegram=telegram, vision=vision, vision_key="synthetic-key")
    response = post_update(client, photo_update(caption="beli cabai 2 kg 100rb dari pasar pagi"))
    assert response.status_code == 200, response.text
    assert len(vision.prompts) == 1
    assert "Untrusted photo caption" in vision.prompts[0] and "beli cabai 2 kg 100rb dari pasar pagi" in vision.prompts[0]
    draft = [d for d in client.get("/v1/overview").json()["data"]["drafts"] if d["status"] == "pending_approval"]
    assert draft and draft[0]["supplier"] == "Pasar A"


def test_photo_without_caption_still_extracts(tmp_path):
    telegram = FakeTelegramAPI()
    vision = FakeVisionClient(NOTA)
    client, _ = client_with(tmp_path, telegram=telegram, vision=vision, vision_key="synthetic-key")
    response = post_update(client, photo_update(caption=None))
    assert response.status_code == 200, response.text
    assert len(vision.prompts) == 1 and "Untrusted photo caption" not in vision.prompts[0]


# --- Voice summaries ----------------------------------------------------------


def weekly_update(update_id=500):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": int(CHAT_ID)}, "date": 1700000000, "text": "ringkasan"}}


def test_weekly_summary_replies_as_a_voice_note(tmp_path, monkeypatch):
    telegram = FakeTelegramAPI()
    client, _ = client_with(tmp_path, telegram=telegram, vision_key="synthetic-tts-key")

    async def fake_speech(*args, **kwargs):
        return b"OGGSPEECH"

    monkeypatch.setattr("service.api.synthesize_speech", fake_speech)
    response = post_update(client, weekly_update())
    assert response.status_code == 200, response.text
    voice_calls = [c for c in telegram.calls if c["url"].endswith("/sendVoice")]
    text_calls = [c for c in telegram.calls if c["url"].endswith("/sendMessage")]
    assert len(voice_calls) == 1 and not text_calls  # voice replaces the text reply
    assert voice_calls[0]["data"]["chat_id"] == CHAT_ID
    assert voice_calls[0]["files"]["voice"][1] == b"OGGSPEECH"


def test_voice_failure_falls_back_to_the_text_summary(tmp_path, monkeypatch):
    telegram = FakeTelegramAPI()
    client, _ = client_with(tmp_path, telegram=telegram, vision_key="synthetic-tts-key")

    async def broken_speech(*args, **kwargs):
        raise TelegramError("StepFun TTS rejected the request (HTTP 502).", 502)

    monkeypatch.setattr("service.api.synthesize_speech", broken_speech)
    response = post_update(client, weekly_update())
    assert response.status_code == 200, response.text
    assert [c["url"].rsplit("/", 1)[-1] for c in telegram.calls] == ["sendChatAction", "sendMessage"]


def test_weekly_summary_stays_text_without_tts_key(tmp_path):
    telegram = FakeTelegramAPI()
    client, _ = client_with(tmp_path, telegram=telegram, vision_key=None)
    response = post_update(client, weekly_update())
    assert response.status_code == 200, response.text
    assert [c["url"].rsplit("/", 1)[-1] for c in telegram.calls] == ["sendChatAction", "sendMessage"]


def test_send_voice_rejects_empty_audio():
    import asyncio

    from service.telegram import send_voice

    async def run():
        await send_voice(BOT_TOKEN, CHAT_ID, b"", client=FakeTelegramAPI())

    try:
        asyncio.run(run())
    except TelegramError as exc:
        assert exc.status == 400
    else:
        raise AssertionError("send_voice must reject empty audio")
