"""Local demonstration API with session login and tenant isolation.

Loopback-only by default. Pass public_demo=True for a hosted demo deployment
(e.g. Vercel). Users log in as a resto owner (their own restaurant's
dashboard) or platform admin (restaurant management, read-only dashboards).
WhatsApp numbers are linked to exactly one restaurant; unregistered numbers
are rejected before any processing.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints
from starlette.exceptions import HTTPException
from starlette.concurrency import run_in_threadpool

from .audio import (
    DEFAULT_ASR_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    StepFunAudioError,
    synthesize_speech,
    transcribe_audio,
    validate_asr_model,
    validate_tts_model,
)
from .db import APPROVE_COMMANDS, DomainError, REJECT_COMMANDS, SESSION_TTL, Store, SUMMARY_COMMANDS
from .pg_store import PostgresStore
from .extraction import extract_purchase
from .text_llm import StepFunTextError, extract_message
from .vision import (
    DEFAULT_BASE_URL as VISION_DEFAULT_BASE_URL,
    DEFAULT_MODEL as VISION_DEFAULT_MODEL,
    StepFunVisionError,
    decode_image as decode_vision_image,
    extract_invoice,
    validate_base_url as validate_vision_base_url,
    validate_model as validate_vision_model,
)
from .whatsapp import (
    LIVE,
    OUTBOUND_PENDING,
    OUTBOUND_SENDING,
    OUTBOUND_SENT,
    WEBHOOK_PATH,
    MetaWhatsAppError,
    download_audio,
    download_image,
    matches_public_host,
    matches_verify_token,
    meta_event_key,
    parse_payment_followup,
    parse_webhook,
    public_origin,
    send_audio_message,
    send_text,
    upload_audio,
    validate_graph_version,
    validate_meta_id,
    validate_verify_token,
    validate_webhook_signature,
)


logger = logging.getLogger("restopilot.api")

SESSION_COOKIE = "restopilot_session"


def _test_sqlite_fallback():
    """Tests force the SQLite path by importing service.api under FORCE_SQLITE=1."""
    import os
    return os.environ.get("FORCE_SQLITE", "").strip().lower() in {"1", "true", "yes", "on"}


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Quantity = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)]
Sender = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=50)]
Password = Annotated[str, StringConstraints(min_length=8, max_length=200)]
PhoneNumber = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"[1-9]\d{6,14}")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Model):
    username: Username
    password: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class RestaurantCreate(Model):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    address: ShortText | None = None
    owner_username: Username
    owner_password: Password
    whatsapp_number: PhoneNumber | None = None
    number_label: ShortText | None = None


class RestaurantUpdate(Model):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    address: ShortText | None = None


class WhatsAppNumber(Model):
    whatsapp_number: PhoneNumber
    label: ShortText | None = None


class OwnerPasswordReset(Model):
    username: Username
    new_password: Password


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


class ImageIntake(Model):
    image_base64: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000_000)]
    media_type: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg"
    supplier: ShortText | None = None
    source_label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] = "Foto nota"


def error(code, message, status):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _is_loopback_host(host):
    try:
        parsed = urlsplit("http://" + host)
        parsed.port  # Reject malformed or out-of-range port strings.
        return parsed.hostname in ("localhost", "127.0.0.1", "::1") and parsed.username is None and parsed.password is None and not (parsed.path or parsed.query or parsed.fragment)
    except ValueError:
        return False


def create_app(
    db_path,
    seed=True,
    public_base_url=None,
    public_demo=False,
    meta_access_token=None,
    meta_app_secret=None,
    meta_verify_token=None,
    meta_waba_id=None,
    meta_phone_number_id=None,
    meta_graph_api_version="v23.0",
    meta_http_client=None,
    vision_api_key=None,
    vision_model=None,
    vision_base_url=None,
    vision_http_client=None,
    asr_model=None,
    tts_model=None,
    tts_voice=None,
    text_llm_enabled=False,
    text_llm_model=None,
    text_llm_http_client=None,
):
    app = FastAPI(title="RestoPilot local demo", version="0.2.0", description="Session login with resto owner and platform admin roles, per-restaurant tenant isolation, WhatsApp allowlist, rules extraction and SQLite.")
    # The authority is Supabase Postgres when configured; the local SQLite
    # file stays as an offline/demo fallback so the app still boots without it.
    store = None
    if not _test_sqlite_fallback():
        try:
            store = PostgresStore(seed=seed)
            storage = "supabase"
        except RuntimeError:
            store = None
    if store is None:
        store = Store(db_path, seed=seed)
        storage = "sqlite"

    # Text words that mean "operate on the previous draft" never reach the
    # model: a bare "ya" must stay a zero-cost command.
    _text_command_words = APPROVE_COMMANDS | REJECT_COMMANDS | set(SUMMARY_COMMANDS) | {"skip", "lewati"}

    async def extract_text(text, restaurant_name=None):
        """Rules first; the model only rescues lines the parser could not read.

        With LLM_TEXT_EXTRACTION enabled, a rules result that carries warnings
        (missing fields, ambiguous clauses) gets one model attempt; a model
        error or an empty model answer keeps the rules result, so behavior
        with the feature off is unchanged. The restaurant name rides along so
        the model prompt carries the tenant rule as defense in depth.
        """
        extracted = extract_purchase(text)
        if not text_llm_enabled or not vision_api_key or not extracted["warnings"]:
            return extracted
        if text.strip().casefold() in _text_command_words or parse_payment_followup(text):
            return extracted
        try:
            rescued = await extract_message(
                text,
                restaurant=restaurant_name,
                api_key=vision_api_key,
                model=text_llm_model,
                base_url=vision_base_url,
                client=text_llm_http_client,
            )
        except (StepFunTextError, ValueError) as exc:
            logger.warning("StepFun text rescue failed; keeping the rules result: %s", exc)
            return extracted
        return rescued if rescued["items"] else extracted
    app.state.store = store
    configured_origin = public_origin(public_base_url)
    meta_waba_id = validate_meta_id(meta_waba_id, "META_WABA_ID") if meta_waba_id else None
    meta_phone_number_id = validate_meta_id(meta_phone_number_id, "META_PHONE_NUMBER_ID") if meta_phone_number_id else None
    meta_verify_token = validate_verify_token(meta_verify_token) if meta_verify_token else None
    meta_graph_api_version = validate_graph_version(meta_graph_api_version)
    vision_model = validate_vision_model(vision_model) if vision_model else VISION_DEFAULT_MODEL
    vision_base_url = validate_vision_base_url(vision_base_url) if vision_base_url else VISION_DEFAULT_BASE_URL
    # ASR/TTS share the StepFun account: the vision key and base URL serve audio too.
    asr_model = validate_asr_model(asr_model) if asr_model else DEFAULT_ASR_MODEL
    tts_model = validate_tts_model(tts_model) if tts_model else DEFAULT_TTS_MODEL
    tts_voice = (tts_voice or DEFAULT_TTS_VOICE).strip() or DEFAULT_TTS_VOICE

    @app.middleware("http")
    async def loopback_boundary(request: Request, call_next):
        public_webhook = request.method in ("GET", "POST") and request.url.path == WEBHOOK_PATH and matches_public_host(request.headers.get("host", ""), configured_origin)
        # public_demo relaxes the loopback boundary for hosted demo deployments
        # (e.g. Vercel). It is opt-in per deployment; the local default stays
        # loopback-only. Meta webhook signature validation is unaffected either way.
        if not public_demo:
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

    def current_user(request: Request):
        """Session-cookie authentication; replaces the old demo identity header."""
        token = request.cookies.get(SESSION_COOKIE)
        user = store.session_user(token) if token else None
        if not user:
            raise DomainError("unauthorized", "Log in to continue.", 401)
        return user

    def owner_actor(actor=Depends(current_user)):
        if actor["role"] != "owner":
            raise DomainError("owner_required", "Only a restaurant owner may edit or decide purchase drafts.", 403)
        return actor

    def admin_actor(actor=Depends(current_user)):
        if actor["role"] != "admin":
            raise DomainError("admin_required", "Only the platform admin may manage restaurants.", 403)
        return actor

    def key(idempotency_key: str | None = Header(default=None)):
        if not idempotency_key or len(idempotency_key) > 200 or not idempotency_key.strip() or any(ord(char) < 33 or ord(char) > 126 for char in idempotency_key):
            raise DomainError("idempotency_key_required", "Provide a printable Idempotency-Key header (1–200 characters).", 400)
        return idempotency_key

    @app.post("/v1/auth/login")
    def login(body: Login, response: Response):
        user = store.verify_login(body.username, body.password)
        token = store.create_session(user["id"])
        response.set_cookie(
            SESSION_COOKIE, token,
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True, samesite="lax", path="/",
        )
        return {"data": user}

    @app.post("/v1/auth/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store.delete_session(token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"data": {"ok": True}}

    @app.get("/v1/auth/me")
    def me(actor=Depends(current_user)):
        return {"data": actor}

    @app.get("/health/live")
    def live():
        return {"data": {"status": "ok", "mode": "local_demo"}}

    @app.get("/health/ready")
    def ready():
        if storage == "supabase":
            from .pg import pg_connection
            with pg_connection() as conn:
                conn.cursor().execute("SELECT 1 FROM app_metadata")
        else:
            with store.transaction() as conn:
                conn.execute("SELECT 1 FROM metadata").fetchone()
        return {"data": {"status": "ready", "storage": storage}}

    @app.get("/v1/overview")
    def overview(actor=Depends(current_user)):
        return {"data": store.overview(actor["restaurant_id"])}

    @app.get("/v1/drafts/{draft_id}")
    def draft(draft_id: str, actor=Depends(current_user)):
        return {"data": store.get_draft(draft_id, actor["restaurant_id"])}

    @app.post("/v1/intake", status_code=201)
    async def intake(body: Intake, idempotency_key=Depends(key), actor=Depends(owner_actor)):
        extracted = await extract_text(body.text, actor["restaurant_name"])
        return {"data": await run_in_threadpool(store.intake, idempotency_key, body.model_dump(), extracted, actor["restaurant_id"])}

    @app.post("/v1/intake/image", status_code=201)
    async def intake_image(body: ImageIntake, idempotency_key=Depends(key), actor=Depends(owner_actor)):
        if not vision_api_key:
            raise DomainError("vision_not_configured", "Set STEPFUN_API_KEY before reading invoice photos.", 503)
        try:
            photo = decode_vision_image(body.image_base64, body.media_type)
            extracted = await extract_invoice(
                photo,
                media_type=body.media_type,
                restaurant=actor["restaurant_name"],
                api_key=vision_api_key,
                model=vision_model,
                base_url=vision_base_url,
                client=vision_http_client,
            )
        except StepFunVisionError as exc:
            logger.warning("StepFun vision intake failed: %s", exc)
            raise DomainError("vision_failed", str(exc), exc.status)
        # Only the photo digest enters the idempotency record, never the image.
        return {"data": await run_in_threadpool(
            store.intake_image,
            idempotency_key,
            hashlib.sha256(photo).hexdigest(),
            body.source_label,
            body.supplier or extracted["supplier"],
            extracted["items"],
            extracted["warnings"],
            actor["restaurant_id"],
        )}

    @app.patch("/v1/drafts/{draft_id}")
    def edit(draft_id: str, body: Edit, actor=Depends(owner_actor), idempotency_key=Depends(key)):
        return {"data": store.edit(draft_id, idempotency_key, body.model_dump(), actor["restaurant_id"])}

    @app.post("/v1/drafts/{draft_id}/decision")
    def decide(draft_id: str, body: Decision, actor=Depends(owner_actor), idempotency_key=Depends(key)):
        return {"data": store.decision(draft_id, idempotency_key, body.model_dump(), actor["restaurant_id"])}

    @app.get("/v1/whatsapp/messages")
    def whatsapp_messages(sender: Annotated[str, Query(min_length=1, max_length=200)] = "demo", actor=Depends(owner_actor)):
        return {"data": store.whatsapp_messages(sender, actor["restaurant_id"])}

    @app.post("/v1/whatsapp/simulate")
    async def simulate_whatsapp(body: SimulatedMessage, actor=Depends(owner_actor), idempotency_key=Depends(key)):
        # The simulator may only speak as one of this restaurant's linked numbers.
        sender_restaurant = store.restaurant_for_sender(body.sender)
        if not sender_restaurant or sender_restaurant["id"] != actor["restaurant_id"]:
            raise DomainError("sender_not_registered", "This WhatsApp number is not linked to your restaurant.", 403)
        payload = body.model_dump()
        if payload["text"].strip():
            payload["extracted_text"] = await extract_text(payload["text"], actor["restaurant_name"])
        return {"data": await run_in_threadpool(store.whatsapp_ingest, idempotency_key, payload, restaurant_id=actor["restaurant_id"])}

    def csv_attachment(rows, filename):
        buffer = io.StringIO()
        csv.writer(buffer).writerows(rows)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/v1/export/purchases.csv")
    def export_purchases_csv(actor=Depends(owner_actor)):
        rows = store.export_purchases(actor["restaurant_id"])
        return csv_attachment(
            [["id", "created_at", "supplier", "item_count", "total_idr", "status"]] + [
                [row["id"], row["created_at"], row["supplier"], row["item_count"], row["total_idr"], row["status"]]
                for row in rows
            ],
            "purchases.csv",
        )

    @app.get("/v1/export/stock.csv")
    def export_stock_csv(actor=Depends(owner_actor)):
        rows = store.export_stock(actor["restaurant_id"])
        return csv_attachment(
            [["item", "unit", "current_stock", "last_unit_price"]] + [
                [row["item"], row["unit"], row["current_stock"], row["last_unit_price"]]
                for row in rows
            ],
            "stock.csv",
        )

    # ---- Admin console: restaurants, their logins and WhatsApp numbers ----

    @app.get("/v1/admin/restaurants")
    def admin_restaurants(actor=Depends(admin_actor)):
        return {"data": {"restaurants": store.list_restaurants(), "rejected_attempts": store.recent_rejections()}}

    @app.post("/v1/admin/restaurants", status_code=201)
    def admin_create_restaurant(body: RestaurantCreate, actor=Depends(admin_actor), idempotency_key=Depends(key)):
        return {"data": store.create_restaurant(idempotency_key, body.model_dump())}

    @app.get("/v1/admin/restaurants/{restaurant_id}")
    def admin_restaurant_detail(restaurant_id: str, actor=Depends(admin_actor)):
        return {"data": store.restaurant_detail(restaurant_id)}

    @app.patch("/v1/admin/restaurants/{restaurant_id}")
    def admin_update_restaurant(restaurant_id: str, body: RestaurantUpdate, actor=Depends(admin_actor), idempotency_key=Depends(key)):
        return {"data": store.update_restaurant(restaurant_id, idempotency_key, body.model_dump())}

    @app.get("/v1/admin/restaurants/{restaurant_id}/overview")
    def admin_restaurant_overview(restaurant_id: str, actor=Depends(admin_actor)):
        return {"data": store.overview(restaurant_id)}

    @app.post("/v1/admin/restaurants/{restaurant_id}/numbers")
    def admin_add_number(restaurant_id: str, body: WhatsAppNumber, actor=Depends(admin_actor), idempotency_key=Depends(key)):
        return {"data": store.add_whatsapp_number(restaurant_id, idempotency_key, body.model_dump())}

    @app.delete("/v1/admin/restaurants/{restaurant_id}/numbers/{phone}")
    def admin_remove_number(restaurant_id: str, phone: str, actor=Depends(admin_actor), idempotency_key=Depends(key)):
        return {"data": store.remove_whatsapp_number(restaurant_id, phone, idempotency_key)}

    @app.post("/v1/admin/restaurants/{restaurant_id}/reset-owner-password")
    def admin_reset_owner_password(restaurant_id: str, body: OwnerPasswordReset, actor=Depends(admin_actor), idempotency_key=Depends(key)):
        return {"data": store.reset_owner_password(restaurant_id, idempotency_key, body.model_dump())}

    @app.get(WEBHOOK_PATH)
    async def verify_meta_whatsapp(request: Request):
        if not meta_verify_token or not configured_origin:
            raise DomainError("meta_not_configured", "Set META_WEBHOOK_VERIFY_TOKEN and an HTTPS PUBLIC_BASE_URL before verifying the Meta webhook.", 503)
        params = request.query_params
        required = ("hub.mode", "hub.verify_token", "hub.challenge")
        if any(len(params.getlist(name)) != 1 for name in required):
            raise DomainError("invalid_meta_verification", "Invalid Meta webhook verification request.", 400)
        challenge = params.get("hub.challenge", "")
        if params.get("hub.mode") != "subscribe" or not matches_verify_token(params.get("hub.verify_token"), meta_verify_token):
            raise DomainError("invalid_meta_verification", "Meta webhook verification failed.", 403)
        if not challenge.isdigit() or len(challenge) > 200:
            raise DomainError("invalid_meta_verification", "Invalid Meta webhook verification challenge.", 400)
        return Response(challenge, media_type="text/plain")

    @app.post(WEBHOOK_PATH)
    async def meta_whatsapp(request: Request):
        if not all((meta_access_token, meta_app_secret, meta_verify_token, meta_waba_id, meta_phone_number_id, configured_origin)):
            raise DomainError("meta_not_configured", "Configure all Meta WhatsApp settings and PUBLIC_BASE_URL before accepting live webhooks.", 503)
        if request.url.query:
            raise DomainError("invalid_webhook_url", "Configure the webhook without query parameters.", 400)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise DomainError("invalid_content_type", "Meta WhatsApp webhook requires JSON.", 415)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise DomainError("webhook_too_large", "Webhook payload exceeds 64 KiB.", 413)
        if not validate_webhook_signature(meta_app_secret, bytes(raw), request.headers.get("x-hub-signature-256", "")):
            raise DomainError("invalid_meta_signature", "Invalid Meta webhook signature.", 403)
        try:
            messages = parse_webhook(bytes(raw), waba_id=meta_waba_id, phone_number_id=meta_phone_number_id)
        except MetaWhatsAppError as exc:
            logger.warning("Rejected Meta WhatsApp webhook: %s", exc)
            raise DomainError("invalid_meta_webhook", str(exc), exc.status)

        async def summary_voice(text):
            """Speak the summary and upload it; returns a Meta media ID or None.

            TTS is an optional enhancement: any configuration, synthesis,
            upload, or send problem degrades to the plain text reply and never
            raises out of the webhook.
            """
            try:
                speech = await synthesize_speech(
                    text,
                    api_key=vision_api_key,
                    model=tts_model,
                    voice=tts_voice,
                    base_url=vision_base_url,
                    client=vision_http_client,
                )
                return await upload_audio(
                    meta_access_token,
                    meta_phone_number_id,
                    meta_graph_api_version,
                    speech,
                    client=meta_http_client,
                )
            except (MetaWhatsAppError, StepFunAudioError, ValueError) as exc:
                logger.warning("WhatsApp summary voice reply unavailable: %s", exc)
                return None

        for message in messages:
            event_id = message["event_id"]
            payload = {key: message[key] for key in ("sender", "text", "event_id", "num_media")}
            # The allowlist is checked before any media download or model call:
            # unregistered numbers cost nothing to process and learn nothing.
            sender_restaurant = await run_in_threadpool(store.restaurant_for_sender, message["sender"])
            restaurant_id = sender_restaurant["id"] if sender_restaurant else None
            if message.get("media_kind") == "image" and vision_api_key and sender_restaurant:
                # A photo is read before the draft is created so the owner gets
                # the same review-and-confirm flow as typed text.
                try:
                    photo, media_type = await download_image(
                        message["media_id"],
                        meta_access_token,
                        meta_graph_api_version,
                        client=meta_http_client,
                    )
                    payload["extracted"] = await extract_invoice(
                        photo,
                        media_type=media_type,
                        restaurant=sender_restaurant["name"],
                        api_key=vision_api_key,
                        model=vision_model,
                        base_url=vision_base_url,
                        client=vision_http_client,
                    )
                    payload["source_label"] = "Foto nota WhatsApp"
                except (MetaWhatsAppError, StepFunVisionError) as exc:
                    # A failed read falls back to the safe media reply; no draft.
                    logger.warning("WhatsApp photo intake failed: %s", exc)
            elif message.get("media_kind") == "audio" and vision_api_key and sender_restaurant:
                # A voice note is transcribed and then parsed like typed text;
                # a failed read falls back to the safe media reply; no draft.
                try:
                    audio, mime = await download_audio(
                        message["media_id"],
                        meta_access_token,
                        meta_graph_api_version,
                        client=meta_http_client,
                    )
                    payload["transcript"] = await transcribe_audio(
                        audio,
                        mime,
                        api_key=vision_api_key,
                        model=asr_model,
                        base_url=vision_base_url,
                        client=vision_http_client,
                    )
                    if payload["transcript"].strip():
                        payload["extracted_text"] = await extract_text(payload["transcript"], sender_restaurant["name"])
                except (MetaWhatsAppError, StepFunAudioError) as exc:
                    logger.warning("WhatsApp voice intake failed: %s", exc)
            elif not message.get("media_kind") and payload["text"].strip() and sender_restaurant:
                # Plain text gets the same rules-then-model rescue so free-form
                # sentences reach the draft flow; commands skip the model.
                payload["extracted_text"] = await extract_text(payload["text"], sender_restaurant["name"])
            result = await run_in_threadpool(
                store.whatsapp_ingest,
                meta_event_key(meta_phone_number_id, event_id),
                payload,
                transport=LIVE,
                channel=LIVE + ":" + meta_phone_number_id,
                outbound_status=OUTBOUND_PENDING,
                restaurant_id=restaurant_id,
            )
            outbound = result["messages"][-1]
            claimed = await run_in_threadpool(
                store.claim_whatsapp_message,
                outbound["id"],
                OUTBOUND_PENDING,
                OUTBOUND_SENDING,
            )
            if not claimed:
                continue
            # whatsapp_ingest nests the ingest result (action/summary) under "result".
            summary_result = result.get("result") or {}
            audio_id = None
            if summary_result.get("action") == "weekly_summary" and summary_result.get("summary"):
                audio_id = await summary_voice(outbound["body"])
            if audio_id:
                try:
                    await send_audio_message(
                        meta_access_token,
                        meta_phone_number_id,
                        meta_graph_api_version,
                        message["sender"],
                        audio_id,
                        client=meta_http_client,
                    )
                except (MetaWhatsAppError, ValueError) as exc:
                    logger.warning("WhatsApp audio reply failed; falling back to text: %s", exc)
                    audio_id = None
            if not audio_id:
                try:
                    await send_text(
                        meta_access_token,
                        meta_phone_number_id,
                        meta_graph_api_version,
                        message["sender"],
                        outbound["body"],
                        client=meta_http_client,
                    )
                except (MetaWhatsAppError, ValueError) as exc:
                    await run_in_threadpool(store.set_whatsapp_message_status, outbound["id"], OUTBOUND_PENDING)
                    status = exc.status if isinstance(exc, MetaWhatsAppError) else 500
                    raise DomainError("meta_send_failed", str(exc), status)
            await run_in_threadpool(store.set_whatsapp_message_status, outbound["id"], OUTBOUND_SENT)
        return {"ok": True}

    frontend = Path(__file__).resolve().parent.parent / "frontend"
    landing = Path(__file__).resolve().parent.parent / "landing"
    if frontend.exists():
        app.mount("/assets", StaticFiles(directory=frontend), name="assets")
    if landing.exists():
        app.mount("/landing-assets", StaticFiles(directory=landing), name="landing-assets")

    @app.get("/", include_in_schema=False)
    def index():
        if not (landing / "index.html").exists():
            if not (frontend / "index.html").exists():
                return error("frontend_unavailable", "Frontend is not installed.", 503)
            return RedirectResponse("/app", status_code=307)
        return FileResponse(landing / "index.html")

    @app.get("/app", include_in_schema=False)
    def app_page():
        if not (frontend / "index.html").exists():
            return error("frontend_unavailable", "Frontend is not installed.", 503)
        return FileResponse(frontend / "index.html")

    return app
