"""PDF invoice intake: local rendering, channel transport, dashboard endpoint."""

import base64
import json
import json as jsonlib
from uuid import uuid4

import pymupdf
import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, login_owner
from service.api import create_app
from service.db import RESTAURANT
from service.pdfpages import PdfRenderError, render_pdf_pages
from service.telegram import parse_update


TENANT = RESTAURANT["id"]
BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExam"
SECRET = "pdf-secret"
CHAT_ID = "987654321"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"}
NOTA = {"supplier": "Pasar PDF", "items": [{"raw_name": "Cabai Merah", "quantity": "2", "unit": "kg", "line_total_idr": 100000}], "unreadable": []}


class FakeResponse:
    def __init__(self, body=None, content=b""):
        self.status_code = 200
        self._body = body if body is not None else {"ok": True}
        self.content = content

    def json(self):
        return self._body


class FakeTelegramAPI:
    """getFile returns metadata; the file download returns the queued bytes."""

    def __init__(self, download_bytes):
        self.download_bytes = download_bytes
        self.sends = []

    async def post(self, url, *, headers=None, json=None):
        self.sends.append({"url": url, "json": json})
        return FakeResponse()

    async def get(self, url, *, headers=None, params=None):
        if "/file/bot" in url:
            return FakeResponse(body={"ok": True}, content=self.download_bytes)
        return FakeResponse(body={"ok": True, "result": {"file_path": f"notas/{uuid4().hex}.pdf"}})


class FakeVisionClient:
    def __init__(self, completions):
        self.completions = list(completions)
        self.calls = 0

    async def post(self, url, *, headers=None, json=None):
        self.calls += 1
        payload = self.completions[min(self.calls - 1, len(self.completions) - 1)]
        return FakeResponse(body={"choices": [{"message": {"content": jsonlib.dumps(payload)}}]})


def make_pdf(page_texts, *, corrupt=False):
    """Build a real PDF with one text line per page (or corrupt bytes)."""
    if corrupt:
        return b"%PDF-1.4 this is not a real pdf body"
    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page()
        page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


# --- Rendering -------------------------------------------------------------------


def test_render_pdf_pages_renders_each_page():
    pages = render_pdf_pages(make_pdf(["halaman 1", "halaman 2"]))
    assert len(pages) == 2
    assert all(page.startswith(b"\xff\xd8") for page in pages)  # JPEG magic


def test_render_pdf_pages_rejects_unreadable_and_capped():
    with pytest.raises(PdfRenderError):
        render_pdf_pages(make_pdf(["ok"], corrupt=True))
    with pytest.raises(PdfRenderError):
        render_pdf_pages(b"")
    with pytest.raises(PdfRenderError) as error:
        render_pdf_pages(make_pdf([f"halaman {i}" for i in range(6)]))
    assert "5 halaman" in str(error.value)


# --- Telegram document flow --------------------------------------------------------


def test_parse_update_reads_pdf_documents():
    update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 987654321}, "date": 1,
              "document": {"file_id": "pdf-file", "mime_type": "application/pdf"}}}
    parsed = parse_update(json.dumps(update).encode())
    assert parsed[0]["media_kind"] == "pdf" and parsed[0]["media_id"] == "pdf-file"
    zip_update = {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 987654321}, "date": 1,
                  "document": {"file_id": "zip-file", "mime_type": "application/zip"}}}
    assert parse_update(json.dumps(zip_update).encode())[0]["media_kind"] is None


def test_telegram_pdf_document_creates_one_draft(tmp_path):
    pdf_bytes = make_pdf(["nota halaman 1"])
    telegram = FakeTelegramAPI(pdf_bytes)
    vision = FakeVisionClient([NOTA])
    app = create_app(
        tmp_path / "pdf.sqlite3", seed=False,
        vision_api_key="synthetic-key", vision_http_client=vision,
        telegram_bot_token=BOT_TOKEN, telegram_webhook_secret=SECRET,
        telegram_http_client=telegram, telegram_group_seconds=0,
    )
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    admin = TestClient(app, base_url="http://127.0.0.1")
    login_admin(admin)
    admin.post(f"/v1/admin/restaurants/{TENANT}/numbers", headers={"Idempotency-Key": uuid4().hex},
               json={"whatsapp_number": CHAT_ID, "platform": "telegram"})

    update = {"update_id": 501, "message": {"message_id": 501, "chat": {"id": int(CHAT_ID)}, "date": 1,
              "document": {"file_id": "pdf-file", "mime_type": "application/pdf"}}}
    response = client.post("/webhooks/telegram", headers=HEADERS, content=json.dumps(update).encode())
    assert response.status_code == 200, response.text

    drafts = [d for d in client.get("/v1/overview").json()["data"]["drafts"]
              if d["status"] in ("pending_approval", "needs_clarification")]
    assert len(drafts) == 1
    assert [item["ingredient_id"] for item in drafts[0]["items"]] == ["cabai"]
    assert len(telegram.sends) == 2  # typing hint + the draft reply
    assert "Cabai merah" in telegram.sends[-1]["json"]["text"]


# --- Dashboard endpoint -------------------------------------------------------------


def dashboard_client(tmp_path, completions=(NOTA, NOTA)):
    vision = FakeVisionClient(list(completions))
    app = create_app(tmp_path / "pdfdash.sqlite3", seed=False, vision_api_key="synthetic-key", vision_http_client=vision)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client, vision


def test_intake_pdf_creates_one_merged_draft(tmp_path):
    pdf_bytes = make_pdf(["nota halaman 1", "nota halaman 2"])
    client, vision = dashboard_client(tmp_path, completions=[NOTA, NOTA])
    response = client.post("/v1/intake/pdf", headers={"Idempotency-Key": uuid4().hex},
                           json={"pdf_base64": base64.b64encode(pdf_bytes).decode()})
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    assert [item["ingredient_id"] for item in draft["items"]] == ["cabai", "cabai"]
    assert vision.calls == 2  # one vision call per rendered page


def test_intake_pdf_rejects_corrupt_and_invalid_base64(tmp_path):
    client, _vision = dashboard_client(tmp_path, completions=[NOTA])
    corrupt = base64.b64encode(make_pdf(["x"], corrupt=True)).decode()
    assert client.post("/v1/intake/pdf", headers={"Idempotency-Key": uuid4().hex}, json={"pdf_base64": corrupt}).status_code == 422
    assert client.post("/v1/intake/pdf", headers={"Idempotency-Key": uuid4().hex}, json={"pdf_base64": "bukan base64!!"}).status_code == 422


def test_intake_pdf_requires_vision(tmp_path):
    app = create_app(tmp_path / "novision.sqlite3", seed=False)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    response = client.post("/v1/intake/pdf", headers={"Idempotency-Key": uuid4().hex},
                           json={"pdf_base64": base64.b64encode(make_pdf(["x"])).decode()})
    assert response.status_code == 503
