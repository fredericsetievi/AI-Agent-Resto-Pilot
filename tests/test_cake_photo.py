"""Evening leftover-count photos: caption intent routes them to a pending count draft.

The vision provider is a local fake; the suite never calls the StepFun API.
"""

import asyncio
import json
import json as jsonlib
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, login_owner
from service.api import create_app
from service.db import RESTAURANT
from service.vision import (
    StepFunVisionError,
    caption_requests_leftover_counts,
    clean_leftover_counts,
    extract_leftover_counts,
)

TENANT = RESTAURANT["id"]

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128
BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExam"
SECRET = "cake-secret"
CHAT_ID = "555111222"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"}
MODEL_URL = "https://api.stepfun.com/v1/chat/completions"

CAKES = [
    {"name": "Kue pandan", "unit": "pcs", "quantity": "5", "code": None, "threshold": None},
    {"name": "Bolu choco", "unit": "pcs", "quantity": "9", "code": None, "threshold": None},
]
# The model proposes loose names; validation must map them onto the catalog.
MODEL_COUNTS = {"items": [{"name": "pandan", "count": 8}, {"name": "choco", "count": 11}, {"name": "roti sobek", "count": 3}],
                "unsure": ["kemasan tak terbaca"]}


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}
        self.content = content

    def json(self):
        return self._body


class FakeTelegramAPI:
    def __init__(self):
        self.calls = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"url": url, "json": json})
        return FakeResponse()

    async def get(self, url, *, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        if "/file/bot" in url:
            return FakeResponse(body={"ok": True}, content=JPEG)
        return FakeResponse(body={"ok": True, "result": {"file_path": "stok/malam.jpg"}})


class FakeVisionClient:
    """One queued completion per photo, in arrival order."""

    def __init__(self, completions):
        self.completions = list(completions)
        self.calls = 0

    async def post(self, url, *, headers=None, json=None):
        self.calls += 1
        payload = self.completions[min(self.calls - 1, len(self.completions) - 1)]
        return FakeResponse(body={"choices": [{"message": {"content": jsonlib.dumps(payload)}}]})


def completion(payload):
    return FakeResponse(body={"choices": [{"message": {"content": json.dumps(payload)}}]})


def photo_update(update_id, caption=None):
    message = {"message_id": update_id, "chat": {"id": int(CHAT_ID)}, "date": 1700000000,
               "photo": [{"file_id": f"file-{update_id}", "file_size": 900}]}
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def text_update(update_id, text):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": int(CHAT_ID)}, "date": 1700000000, "text": text}}


def cake_client(tmp_path, completions=(MODEL_COUNTS,), **kwargs):
    telegram = FakeTelegramAPI()
    vision = FakeVisionClient(list(completions))
    kwargs.setdefault("vision_api_key", "synthetic-key")
    kwargs.setdefault("telegram_bot_token", BOT_TOKEN)
    kwargs.setdefault("telegram_webhook_secret", SECRET)
    kwargs.setdefault("telegram_http_client", telegram)
    kwargs.setdefault("vision_http_client", vision)
    kwargs.setdefault("seed", False)
    app = create_app(tmp_path / "cake.sqlite3", **kwargs)
    app.state.store.import_catalog(TENANT, [dict(cake) for cake in CAKES])
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    admin = TestClient(app, base_url="http://127.0.0.1")
    login_admin(admin)
    admin.post(f"/v1/admin/restaurants/{RESTAURANT['id']}/numbers", headers={"Idempotency-Key": uuid4().hex},
               json={"whatsapp_number": CHAT_ID, "platform": "telegram", "label": "Owner chat"})
    return client, telegram, vision, app.state.store


def post_update(client, update):
    return client.post("/webhooks/telegram", headers=HEADERS, content=json.dumps(update).encode())


def wait_for_reply(telegram, timeout_seconds=8):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        replies = [call for call in telegram.calls if call.get("url", "").endswith("/sendMessage")]
        if replies:
            return replies[0]["json"]["text"]
        time.sleep(0.05)
    return None


def operations(store):
    with store.transaction() as conn:
        return [json.loads(row["body"]) for row in conn.execute(
            "SELECT body FROM whatsapp_operations WHERE restaurant_id=?", (TENANT,)).fetchall()]


def inventory(store):
    return {row["name"]: row["quantity"] for row in store.overview(TENANT)["inventory"]}


# --- Caption intent and validation -------------------------------------------------


def test_caption_intent_keywords():
    assert caption_requests_leftover_counts("Stok malam")
    assert caption_requests_leftover_counts("sisa kue hari ini")
    assert caption_requests_leftover_counts("Hitung Stok rak kue")
    assert caption_requests_leftover_counts("OPNAME sore")
    assert caption_requests_leftover_counts("stok akhir nih")
    assert not caption_requests_leftover_counts("nota tadi pagi")
    assert not caption_requests_leftover_counts("")
    assert not caption_requests_leftover_counts(None)


def test_clean_leftover_counts_validates_and_matches_the_catalog():
    names = ["Kue pandan", "Kue lapis", "Bolu choco"]
    cleaned = clean_leftover_counts({
        "items": [
            {"name": "pandan", "count": 8},          # substring of a catalog name
            {"name": "kue pandan melebar", "count": "9"},  # catalog name inside a longer label
            {"name": "choco", "count": 2.0},          # integral float coerces
            {"name": "kue", "count": 4},              # ambiguous: two catalog matches -> dropped
            {"name": "pandan", "count": 5},           # duplicate catalog item: first wins
            {"name": "benda asing", "count": 3},      # not in the catalog -> dropped
            {"name": "Bolu choco", "count": -2},      # negative -> dropped
            {"name": "Bolu choco", "count": 1000},    # beyond 999 -> dropped
            {"name": "Bolu choco", "count": "banyak"},  # not a number -> dropped
        ],
        "unsure": ["kemasan rusak", "kemasan rusak", ""],
    }, names)
    assert cleaned == {"items": [{"name": "Kue pandan", "count": 8}, {"name": "Bolu choco", "count": 2}],
                       "unsure": ["kemasan rusak"]}


def test_extract_leftover_counts_sends_the_catalog_and_never_leaks_the_key():
    captured = []

    class Recorder:
        async def post(self, url, *, headers=None, json=None):
            captured.append({"url": url, "headers": headers, "json": json})
            return FakeResponse(body={"choices": [{"message": {"content": jsonlib.dumps(MODEL_COUNTS)}}]})

    client = Recorder()
    result = asyncio.run(extract_leftover_counts(
        JPEG, "image/jpeg", [cake["name"] for cake in CAKES],
        api_key="synthetic-stepfun-key", http_client=client))
    assert len(captured) == 1
    call = captured[0]
    assert call["url"] == MODEL_URL
    assert call["headers"]["Authorization"] == "Bearer synthetic-stepfun-key"
    prompt = call["json"]["messages"][0]["content"][0]["text"]
    assert "- Kue pandan" in prompt and "- Bolu choco" in prompt
    assert "never" in prompt.lower() and "unsure" in prompt
    assert result == {"items": [{"name": "Kue pandan", "count": 8}, {"name": "Bolu choco", "count": 11}], "unsure": ["kemasan tak terbaca"]}


def test_extract_leftover_counts_requires_a_catalog():
    client = FakeVisionClient([])
    with pytest.raises(StepFunVisionError):
        asyncio.run(extract_leftover_counts(JPEG, "image/jpeg", [], api_key="synthetic-stepfun-key", http_client=client))
    assert client.calls == 0


# --- Telegram end-to-end -------------------------------------------------------------


def test_leftover_photo_creates_a_pending_count_draft(tmp_path):
    client, telegram, vision, store = cake_client(tmp_path)
    with client:
        post_update(client, photo_update(901, "stok malam"))
        reply = wait_for_reply(telegram)
    assert vision.calls == 1
    assert reply is not None, "the bot never answered the leftover photo"
    ops = operations(store)
    assert len(ops) == 1
    op = ops[0]
    assert op["status"] == "review"
    lines = [(line["kind"], line["name"], line["quantity"], line["unit"]) for line in op["candidate"]["lines"]]
    assert lines == [("count", "kue pandan", "8", "pcs"), ("count", "bolu choco", "11", "pcs")]
    assert "roti sobek" not in json.dumps(op)  # unknown names are dropped, never invented
    # A pending draft only: nothing recorded until the owner approves.
    stocks = inventory(store)
    assert stocks["Kue pandan"] == "5" and stocks["Bolu choco"] == "9"
    assert "YA" in reply and op["id"][:8] in reply


def test_leftover_draft_records_only_after_an_explicit_approval(tmp_path):
    client, telegram, vision, store = cake_client(tmp_path)
    with client:
        post_update(client, photo_update(911, "sisa sore ini"))
        wait_for_reply(telegram)
    op = operations(store)[0]
    with client:
        post_update(client, text_update(912, f"YA {op['id'][:8]} V1"))
        wait_for_reply(telegram)
    stocks = inventory(store)
    assert stocks["Kue pandan"] == "8" and stocks["Bolu choco"] == "11"


def test_leftover_photo_without_readable_items_asks_for_a_typed_count(tmp_path):
    client, telegram, vision, store = cake_client(tmp_path, completions=[{"items": [{"name": "benda asing", "count": 4}], "unsure": []}])
    with client:
        post_update(client, photo_update(921, "stok malam"))
        reply = wait_for_reply(telegram)
    assert vision.calls == 1
    assert reply is not None and "tidak terbaca" in reply and "BATAL" in reply
    assert operations(store)[0]["status"] == "clarification"
    stocks = inventory(store)
    assert stocks["Kue pandan"] == "5" and stocks["Bolu choco"] == "9"


def test_photo_without_a_leftover_caption_keeps_purchase_intake(tmp_path):
    nota = {"supplier": "Pasar Kue", "items": [{"raw_name": "Telur Ayam", "quantity": "2", "unit": "kg", "line_total_idr": 60000}], "unreadable": []}
    client, telegram, vision, store = cake_client(tmp_path, completions=[nota])
    with client:
        post_update(client, photo_update(931))
        wait_for_reply(telegram)
    assert vision.calls == 1
    assert operations(store) == []
    drafts = [draft for draft in store.overview(TENANT)["drafts"] if draft["status"] == "pending_approval"]
    assert len(drafts) == 1 and [item["ingredient_id"] for item in drafts[0]["items"]] == ["telur"]


def test_purchase_caption_keeps_purchase_intake(tmp_path):
    nota = {"supplier": "Pasar Kue", "items": [{"raw_name": "Telur Ayam", "quantity": "1", "unit": "kg", "line_total_idr": 30000}], "unreadable": []}
    client, telegram, vision, store = cake_client(tmp_path, completions=[nota])
    with client:
        post_update(client, photo_update(941, "beli telur 2 kg"))
        wait_for_reply(telegram)
    assert operations(store) == []
    drafts = [draft for draft in store.overview(TENANT)["drafts"] if draft["status"] == "pending_approval"]
    assert len(drafts) == 1
