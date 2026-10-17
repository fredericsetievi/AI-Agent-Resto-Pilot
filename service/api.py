"""Loopback-only local demonstration API; demo identity is not authentication."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints
from starlette.exceptions import HTTPException
from starlette.concurrency import run_in_threadpool

from .db import DomainError, Store
from .whatsapp import LIVE, WEBHOOK_PATH, matches_public_host, public_origin, twilio_event_key, twiml_reply, validate_signature


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Quantity = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)]
Sender = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Intake(Model):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
    supplier: ShortText | None = None


class Item(Model):
    ingredient_id: ShortText | None
    raw_name: ShortText
    quantity: Quantity | None
    unit: ShortText | None
    line_total_idr: StrictInt | None


class Edit(Model):
    expected_version: Annotated[StrictInt, Field(gt=0)]
    items: Annotated[list[Item], Field(min_length=1, max_length=20)]
    supplier: ShortText | None


class Decision(Model):
    expected_version: Annotated[StrictInt, Field(gt=0)]
    decision: Literal["approve", "reject"]
    received: StrictBool


class SimulatedMessage(Model):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
    sender: Sender = "demo"
    event_id: Sender | None = None


def error(code, message, status):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _is_loopback_host(host):
    try:
        parsed = urlsplit("http://" + host)
        parsed.port  # Reject malformed or out-of-range port strings.
        return parsed.hostname in ("localhost", "127.0.0.1", "::1") and parsed.username is None and parsed.password is None and not (parsed.path or parsed.query or parsed.fragment)
    except ValueError:
        return False


def create_app(db_path, seed=True, twilio_auth_token=None, public_base_url=None):
    app = FastAPI(title="RestoPilot local demo", version="0.1.0", description="Manual intake, rules extraction and SQLite. Synthetic demo identity; not production authentication.")
    store = Store(db_path, seed=seed)
    app.state.store = store
    configured_origin = public_origin(public_base_url)

    @app.middleware("http")
    async def loopback_boundary(request: Request, call_next):
        public_webhook = request.method == "POST" and request.url.path == WEBHOOK_PATH and matches_public_host(request.headers.get("host", ""), configured_origin)
        if not _is_loopback_host(request.headers.get("host", "")) and not public_webhook:
            return error("invalid_host", "This local demo accepts loopback hosts only.", 403)
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                host = urlsplit("http://" + request.headers["host"])
                # Browser requests must come from the exact local server origin.
                if parsed.scheme != request.url.scheme or parsed.hostname != host.hostname or (parsed.port or (443 if parsed.scheme == "https" else 80)) != (host.port or 80) or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                    return error("invalid_origin", "Use this demo's own local browser origin.", 403)
            except ValueError:
                return error("invalid_origin", "Invalid browser origin.", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return error("cross_site_request", "Cross-site requests are not allowed in this local demo.", 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return error(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        fields = ", ".join(".".join(str(part) for part in issue["loc"] if part != "body") for issue in exc.errors()[:5])
        return error("invalid_request", f"Invalid or missing request fields: {fields}.", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error("not_found" if exc.status_code == 404 else "http_error", str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return error("internal_error", "The operation could not be completed. Refresh and retry using the same idempotency key.", 500)

    def identity(x_demo_role: str | None = Header(default=None)):
        if x_demo_role not in (None, "owner", "staff"):
            raise DomainError("invalid_demo_role", "Demo role must be owner or staff.", 403)
        return {"id": "demo-owner" if x_demo_role != "staff" else "demo-staff", "role": x_demo_role or "owner", "restaurant_id": "demo-resto"}

    def owner(actor=Depends(identity)):
        if actor["role"] != "owner":
            raise DomainError("owner_required", "Only the demo owner may edit or decide purchase drafts.", 403)
        return actor

    def key(idempotency_key: str | None = Header(default=None)):
        if not idempotency_key or len(idempotency_key) > 200 or not idempotency_key.strip() or any(ord(char) < 33 or ord(char) > 126 for char in idempotency_key):
            raise DomainError("idempotency_key_required", "Provide a printable Idempotency-Key header (1–200 characters).", 400)
        return idempotency_key

    @app.get("/health/live")
    def live():
        return {"data": {"status": "ok", "mode": "local_demo"}}

    @app.get("/health/ready")
    def ready():
        with store.transaction() as conn:
            conn.execute("SELECT 1 FROM metadata").fetchone()
        return {"data": {"status": "ready", "storage": "sqlite"}}

    @app.get("/v1/overview")
    def overview(actor=Depends(identity)):
        return {"data": store.overview()}

    @app.get("/v1/drafts/{draft_id}")
    def draft(draft_id: str, actor=Depends(identity)):
        return {"data": store.get_draft(draft_id)}

    @app.post("/v1/intake", status_code=201)
    def intake(body: Intake, idempotency_key=Depends(key), actor=Depends(identity)):
        return {"data": store.intake(idempotency_key, body.model_dump())}

    @app.patch("/v1/drafts/{draft_id}")
    def edit(draft_id: str, body: Edit, actor=Depends(owner), idempotency_key=Depends(key)):
        return {"data": store.edit(draft_id, idempotency_key, body.model_dump())}

    @app.post("/v1/drafts/{draft_id}/decision")
    def decide(draft_id: str, body: Decision, actor=Depends(owner), idempotency_key=Depends(key)):
        return {"data": store.decision(draft_id, idempotency_key, body.model_dump())}

    @app.get("/v1/whatsapp/messages")
    def whatsapp_messages(sender: Annotated[str, Query(min_length=1, max_length=200)] = "demo", actor=Depends(owner)):
        return {"data": store.whatsapp_messages(sender)}

    @app.post("/v1/whatsapp/simulate")
    def simulate_whatsapp(body: SimulatedMessage, actor=Depends(owner), idempotency_key=Depends(key)):
        return {"data": store.whatsapp_ingest(idempotency_key, body.model_dump())}

    @app.post(WEBHOOK_PATH)
    async def twilio_whatsapp(request: Request):
        if not twilio_auth_token or not configured_origin:
            raise DomainError("twilio_not_configured", "Set TWILIO_AUTH_TOKEN and an HTTPS PUBLIC_BASE_URL before accepting live Twilio webhooks.", 503)
        if request.url.query:
            raise DomainError("invalid_webhook_url", "Configure the webhook without query parameters.", 400)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise DomainError("invalid_content_type", "Twilio webhook requires form-urlencoded data.", 415)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise DomainError("webhook_too_large", "Webhook payload exceeds 64 KiB.", 413)
        try:
            pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=100, errors="strict")
            params = dict(pairs)
        except (UnicodeError, ValueError):
            raise DomainError("invalid_webhook_form", "Malformed Twilio webhook form.", 400)
        if len(params) != len(pairs):
            raise DomainError("invalid_webhook_form", "Repeated webhook fields are not supported.", 400)
        try:
            valid = validate_signature(twilio_auth_token, configured_origin + WEBHOOK_PATH, params, request.headers.get("x-twilio-signature", ""))
        except ImportError:
            raise DomainError("twilio_sdk_unavailable", "Install the pinned Twilio SDK before accepting live webhooks.", 503)
        if not valid:
            raise DomainError("invalid_twilio_signature", "Invalid Twilio request signature.", 403)
        sender, recipient, sid = params.get("From", ""), params.get("To", ""), params.get("MessageSid", "")
        if not re.fullmatch(r"whatsapp:\+[1-9]\d{6,14}", sender) or not re.fullmatch(r"whatsapp:\+[1-9]\d{6,14}", recipient) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sid):
            raise DomainError("invalid_twilio_message", "Valid WhatsApp From, To and MessageSid fields are required.", 400)
        media_text, text = params.get("NumMedia", "0"), params.get("Body", "").strip()
        if not re.fullmatch(r"\d{1,2}", media_text) or int(media_text) > 10 or len(text) > 10000 or (not text and not int(media_text)):
            raise DomainError("invalid_twilio_message", "Provide a bounded text message or supported media count.", 400)
        channel = LIVE + ":" + recipient
        event_key = twilio_event_key(recipient, sid)
        payload = {"sender": sender, "text": text, "event_id": sid, "num_media": int(media_text)}
        result = await run_in_threadpool(store.whatsapp_ingest, event_key, payload, transport=LIVE, channel=channel)
        return Response(twiml_reply(result["messages"][-1]["body"]), media_type="application/xml")

    frontend = Path(__file__).resolve().parent.parent / "frontend"
    if frontend.exists():
        app.mount("/assets", StaticFiles(directory=frontend), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        if not (frontend / "index.html").exists():
            return error("frontend_unavailable", "Frontend is not installed.", 503)
        return FileResponse(frontend / "index.html")

    return app
