"""Local demonstration API with session login and tenant isolation.

Loopback-only by default. Pass public_demo=True for a hosted demo deployment
(e.g. Vercel). Users log in as a resto owner (their own restaurant's
dashboard) or platform admin (restaurant management, read-only dashboards).
WhatsApp numbers are linked to exactly one restaurant; unregistered numbers
are rejected before any processing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
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
from .data_qa import build_answer as build_data_answer
from .data_qa import phrase_data_answer, phrase_opname_answer
from .extraction import extract_purchase
from .intent_llm import classify_intent
from .kitchen_filter import classify_items, NON_KITCHEN_WARNING
from .notifications import build_alert_text, phrase_alert
from .pdfpages import PdfRenderError, render_pdf_pages
from .rules import effective_directives
from .text_llm import StepFunTextError, extract_message
from .stock_llm import phrase_stock_answer
from .telegram import (
    DEFAULT_API_BASE as TELEGRAM_DEFAULT_API_BASE,
    MAX_PDF_BYTES as TG_MAX_PDF_BYTES,
    OUTBOUND_PENDING as TG_OUTBOUND_PENDING,
    OUTBOUND_SENDING as TG_OUTBOUND_SENDING,
    OUTBOUND_SENT as TG_OUTBOUND_SENT,
    TELEGRAM_WEBHOOK_PATH,
    TRANSPORT as TG_TRANSPORT,
    TelegramError,
    bot_id_from_token,
    download_file as download_telegram_file,
    matches_secret as matches_telegram_secret,
    parse_update as parse_telegram_update,
    send_text as send_telegram_text,
    validate_bot_token as validate_telegram_token,
    validate_secret_token as validate_telegram_secret,
)
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
    download_pdf,
    matches_public_host,
    matches_verify_token,
    meta_event_key,
    parse_payment_followup,
    parse_webhook,
    stock_reply,
    public_origin,
    opname_reply,
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


def effective_asr_config(asr_api_key, asr_base_url, vision_api_key, vision_base_url):
    """Resolve the voice-note ASR provider: an explicit AUDIO_ASR_* override
    (e.g. a multilingual speech service) wins; otherwise ASR shares the StepFun
    account and base URL with vision, exactly as before."""
    key = (asr_api_key or "").strip() or vision_api_key
    base = validate_vision_base_url(asr_base_url) if asr_base_url else vision_base_url
    return key, base


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
    platform: Literal["whatsapp", "telegram"] = "whatsapp"
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
    # Server-side kitchen-filter annotation the dashboard echoes back on edit.
    non_kitchen: StrictBool | None = None


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

class ImagePart(Model):
    image_base64: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000_000)]
    media_type: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg"


class IntakeImages(Model):
    """Up to five nota photos submitted together as one purchase draft."""
    images: Annotated[list[ImagePart], Field(min_length=1, max_length=5)]
    supplier: ShortText | None = None

class PdfIntake(Model):
    pdf_base64: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=14_000_000)]
    supplier: ShortText | None = None

class StockCountCreate(Model):
    ingredient_id: ShortText
    counted_quantity: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    note: ShortText | None = None


class RuleCreate(Model):
    rule: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class RulePatch(Model):
    rule: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] | None = None
    enabled: StrictBool | None = None


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
    asr_api_key=None,
    asr_base_url=None,
    tts_model=None,
    tts_voice=None,
    text_llm_enabled=False,
    text_llm_model=None,
    text_llm_http_client=None,
    stock_llm_enabled=False,
    stock_llm_http_client=None,
    kitchen_filter_enabled=True,
    intent_llm_enabled=False,
    intent_llm_http_client=None,
    telegram_bot_token=None,
    telegram_webhook_secret=None,
    telegram_api_base=TELEGRAM_DEFAULT_API_BASE,
    telegram_http_client=None,
    telegram_group_seconds=4.0,
    proactive_check_interval_seconds=None,
):
    # The proactive loop closes over store/run_proactive_check by name; both
    # are bound below before any request (or lifespan call) can run.
    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        task = None
        if proactive_check_interval_seconds and proactive_check_interval_seconds > 0:
            async def proactive_loop():
                while True:
                    await asyncio.sleep(proactive_check_interval_seconds)
                    for resto in await run_in_threadpool(store.list_restaurants):
                        try:
                            await run_proactive_check(resto["id"])
                        except Exception as exc:  # noqa: BLE001 - one resto must not stop the sweep
                            logger.warning("Proactive check failed for %s: %s", resto["id"], exc)
            task = asyncio.create_task(proactive_loop())
            logger.info("Proactive low-stock checks scheduled every %s seconds.", proactive_check_interval_seconds)
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="RestoPilot local demo", version="0.2.0", description="Session login with resto owner and platform admin roles, per-restaurant tenant isolation, WhatsApp allowlist, rules extraction and SQLite.", lifespan=_lifespan)
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

    def _no_purchase_signal(extracted):
        """True when extraction read no usable purchase line from the message."""
        items = extracted.get("items") or []
        return all(item.get("ingredient_id") is None and item.get("quantity") is None and item.get("line_total_idr") is None for item in items)

    async def maybe_intent_hint(text, extracted):
        """Route a chatty message to approve/reject/summary when the words missed.

        Strictly bounded: fires only with the flag on and only when extraction
        found no purchase signal (a purchase note can never be reinterpreted);
        an unclear answer, an error, or a disabled flag returns None and the
        existing behavior stands untouched.
        """
        if not intent_llm_enabled or not vision_api_key or not _no_purchase_signal(extracted):
            return None
        try:
            intent = await classify_intent(
                text,
                api_key=vision_api_key,
                model=text_llm_model,
                base_url=vision_base_url,
                client=intent_llm_http_client,
            )
        except (StepFunTextError, ValueError) as exc:
            logger.warning("Intent routing failed; keeping the default flow: %s", exc)
            return None
        return intent if intent in ("approve", "reject", "summary") else None

    async def filter_kitchen_items(extracted, restaurant_id):
        """Mark non-kitchen goods (odol, sikat gigi, sabun, ...) in an extraction.

        One LLM call classifies the unmapped item names; catalog-mapped items
        are kitchen items by definition and skip the model. Flagged items stay
        on the draft with a warning so the owner decides — the filter never
        deletes a line by itself. Any provider problem leaves the extraction
        untouched.
        """
        items = extracted.get("items") or []
        if not items or not kitchen_filter_enabled or not vision_api_key:
            return extracted
        unmapped = [(index, item) for index, item in enumerate(items)
                    if not item.get("ingredient_id") and str(item.get("raw_name") or "").strip()]
        if not unmapped:
            return extracted
        # Items already matched to this restaurant's catalog are cooking
        # supplies; they are pre-classified as kitchen so the model only sees
        # unknown raw names. The model works in global item indices.
        catalog_ids = {ingredient["id"] for ingredient in store.catalog_list(restaurant_id)}
        pre_kitchen = [index for index, item in unmapped
                       if store.slugify_name(item["raw_name"]) in catalog_ids]
        unknown = [(index, item) for index, item in unmapped
                   if store.slugify_name(item["raw_name"]) not in catalog_ids]
        if not unknown:
            return extracted
        try:
            verdict = await classify_items(
                [(index, item["raw_name"]) for index, item in unknown],
                api_key=vision_api_key,
                model=text_llm_model,
                base_url=vision_base_url,
                client=stock_llm_http_client,
            )
        except (StepFunTextError, ValueError) as exc:
            logger.warning("Kitchen filter failed; keeping all items: %s", exc)
            return extracted
        warnings = list(extracted.get("warnings") or [])
        non_kitchen_positions = {position for position, (index, _item) in enumerate(unknown)
                                 if index in set(verdict["non_kitchen"])}
        for position, (index, item) in enumerate(unknown):
            if position in non_kitchen_positions:
                warnings.append(NON_KITCHEN_WARNING.format(index=index + 1, name=item["raw_name"]))
                item["non_kitchen"] = True
        extracted["warnings"] = list(dict.fromkeys(warnings))
        return extracted

    async def build_stock_answer(text, restaurant_id):
        """Answer data questions (spending, prices, stock) from the ledgers.

        Spending and price questions are checked first, then stock questions.
        Facts are scoped to the asking restaurant before anything reaches the
        model; with the stock flag on, the model only rephrases those facts
        and any configuration, transport, or validation problem keeps the
        deterministic text. Returns None when the message is none of them,
        leaving the purchase flow alone.
        """
        data = store.data_facts(text, restaurant_id)
        if data is not None:
            if not data["facts"]:
                return {"body": data["empty_text"]}
            reply = build_data_answer(data["facts"])
            if stock_llm_enabled and vision_api_key:
                try:
                    reply = await phrase_data_answer(
                        data["question"],
                        data["facts"],
                        store.enabled_rules_text(restaurant_id),
                        api_key=vision_api_key,
                        model=text_llm_model,
                        base_url=vision_base_url,
                        client=stock_llm_http_client,
                    )
                except (StepFunTextError, ValueError) as exc:
                    logger.warning("Data answer phrasing failed; keeping the deterministic reply: %s", exc)
            return {"body": reply}
        facts = store.stock_facts(text, restaurant_id)
        if facts is None:
            return None
        reply = stock_reply(facts["facts"])
        if stock_llm_enabled and vision_api_key:
            try:
                reply = await phrase_stock_answer(
                    facts["question"],
                    facts["facts"],
                    restaurant=facts.get("restaurant_name"),
                    api_key=vision_api_key,
                    model=text_llm_model,
                    base_url=vision_base_url,
                    client=stock_llm_http_client,
                )
            except (StepFunTextError, ValueError) as exc:
                logger.warning("StepFun stock phrasing failed; keeping the deterministic reply: %s", exc)
        return {"body": reply}

    async def build_opname_answer(text, restaurant_id):
        """Pre-phrase a physical stock count proposal; the store re-derives it.

        The parse itself is deterministic (see Store.parse_opname); this only
        lets the model reword the confirmation, and validate_data_reply keeps
        every ledger figure verbatim. Returns None when the message is not an
        opname, so the stock/data/purchase paths run unchanged.
        """
        parsed = store.parse_opname(text, restaurant_id)
        if parsed is None:
            return None
        reply = opname_reply(parsed["items"], parsed["invalid"])
        if parsed["items"] and stock_llm_enabled and vision_api_key and not parsed["invalid"]:
            try:
                reply = await phrase_opname_answer(
                    parsed["items"],
                    api_key=vision_api_key,
                    model=text_llm_model,
                    base_url=vision_base_url,
                    client=stock_llm_http_client,
                )
            except (StepFunTextError, ValueError) as exc:
                logger.warning("Opname phrasing failed; keeping the deterministic text: %s", exc)
        return {"body": reply, "items": parsed["items"]}

    async def run_proactive_check(restaurant_id, *, dry_run=False):
        """One proactive sweep for one restaurant: rules in, alert out.

        The owner's enabled rules are parsed deterministically (threshold
        overrides and mutes); candidates that survive the cooldown become one
        alert text built from the database first, optionally rephrased by the
        model under strict number validation. With dry_run nothing is sent,
        recorded, or marked as alerted.
        """
        catalog = store.catalog_list(restaurant_id)
        overrides, muted = effective_directives(store.list_rules(restaurant_id), catalog)
        candidates = [item for item in store.low_stock_candidates(restaurant_id, overrides) if item["ingredient_id"] not in muted]
        if not candidates:
            return {"restaurant_id": restaurant_id, "candidates": [], "body": None, "sent": [], "dry_run": bool(dry_run)}
        body_text = build_alert_text(candidates)
        if stock_llm_enabled and vision_api_key:
            try:
                body_text = await phrase_alert(
                    candidates,
                    store.enabled_rules_text(restaurant_id),
                    api_key=vision_api_key,
                    model=text_llm_model,
                    base_url=vision_base_url,
                    client=stock_llm_http_client,
                )
            except (StepFunTextError, ValueError) as exc:
                logger.warning("StepFun stock alert phrasing failed; keeping the deterministic alert: %s", exc)
        sent = []
        if not dry_run:
            # Each linked sender is reached on the platform it was linked to:
            # the same number may appear twice, once per platform.
            meta_ready = bool(meta_access_token and meta_phone_number_id)
            telegram_ready = bool(telegram_bot_token)
            for link in store.linked_numbers(restaurant_id):
                phone, platform = link["phone"], link["platform"]
                channel = platform if (platform == "telegram" and telegram_ready) or (platform == "whatsapp" and meta_ready) else None
                if channel is None:
                    logger.warning("Proactive alert for %s: %s link has no configured channel; recording only.", restaurant_id, platform)
                    store.record_proactive_message(restaurant_id, "proactive:" + restaurant_id, phone, "proactive", body_text, "proactive_no_channel")
                    sent.append({"sender": phone, "platform": platform, "channel": None, "ok": False})
                    continue
                message_id = store.record_proactive_message(restaurant_id, "proactive:" + restaurant_id, phone, "proactive", body_text, "proactive_pending")
                try:
                    if channel == "whatsapp":
                        await send_text(
                            meta_access_token, meta_phone_number_id, meta_graph_api_version, phone, body_text, client=meta_http_client)
                    else:
                        await send_telegram_text(telegram_bot_token, phone, body_text, api_base=telegram_api_base, client=telegram_http_client)
                except (MetaWhatsAppError, TelegramError, ValueError) as exc:
                    logger.warning("Proactive send to %s (%s) failed: %s", phone, platform, exc)
                    await run_in_threadpool(store.set_whatsapp_message_status, message_id, "proactive_failed")
                    sent.append({"sender": phone, "platform": platform, "channel": channel, "ok": False})
                else:
                    await run_in_threadpool(store.set_whatsapp_message_status, message_id, "proactive_sent")
                    sent.append({"sender": phone, "platform": platform, "channel": channel, "ok": True})
            store.mark_proactive_alerts(restaurant_id, candidates)
        return {"restaurant_id": restaurant_id, "candidates": candidates, "body": body_text, "sent": sent, "dry_run": bool(dry_run)}
    app.state.store = store
    configured_origin = public_origin(public_base_url)
    meta_waba_id = validate_meta_id(meta_waba_id, "META_WABA_ID") if meta_waba_id else None
    meta_phone_number_id = validate_meta_id(meta_phone_number_id, "META_PHONE_NUMBER_ID") if meta_phone_number_id else None
    meta_verify_token = validate_verify_token(meta_verify_token) if meta_verify_token else None
    meta_graph_api_version = validate_graph_version(meta_graph_api_version)
    # Telegram is optional: with no token the webhook route answers 503 and
    # the WhatsApp-only deployment is unchanged.
    telegram_bot_token = validate_telegram_token(telegram_bot_token) if telegram_bot_token else None
    telegram_webhook_secret = validate_telegram_secret(telegram_webhook_secret) if telegram_webhook_secret else None
    vision_model = validate_vision_model(vision_model) if vision_model else VISION_DEFAULT_MODEL
    vision_base_url = validate_vision_base_url(vision_base_url) if vision_base_url else VISION_DEFAULT_BASE_URL
    # ASR may run on its own provider (AUDIO_ASR_*); without an override it
    # shares the StepFun account and base URL with vision. TTS always stays on
    # the StepFun account.
    asr_model = validate_asr_model(asr_model) if asr_model else DEFAULT_ASR_MODEL
    asr_api_key, asr_base_url = effective_asr_config(asr_api_key, asr_base_url, vision_api_key, vision_base_url)
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
        extracted = await filter_kitchen_items(extracted, actor["restaurant_id"])
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
        extracted = await filter_kitchen_items(extracted, actor["restaurant_id"])
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

    @app.post("/v1/intake/images", status_code=201)
    async def intake_images(body: IntakeImages, actor=Depends(owner_actor), idempotency_key=Depends(key)):
        """Several nota photos in one request become one merged purchase draft."""
        if not vision_api_key:
            raise DomainError("vision_not_configured", "Set STEPFUN_API_KEY before reading invoice photos.", 503)
        merged = {"items": [], "warnings": []}
        supplier = body.supplier
        for part in body.images:
            try:
                photo = decode_vision_image(part.image_base64, part.media_type)
                extracted = await extract_invoice(
                    photo,
                    media_type=part.media_type,
                    restaurant=actor["restaurant_name"],
                    api_key=vision_api_key,
                    model=vision_model,
                    base_url=vision_base_url,
                    client=vision_http_client,
                )
            except StepFunVisionError as exc:
                logger.warning("StepFun vision multi-photo intake failed: %s", exc)
                raise DomainError("vision_failed", str(exc), exc.status)
            merged["items"].extend(extracted.get("items") or [])
            merged["warnings"].extend(extracted.get("warnings") or [])
            supplier = supplier or extracted.get("supplier")
        merged = await filter_kitchen_items(merged, actor["restaurant_id"])
        payload = {"text": f"Gabungan {len(body.images)} foto nota", "supplier": supplier}
        return {"data": await run_in_threadpool(store.intake, idempotency_key, payload, merged, actor["restaurant_id"])}

    @app.post("/v1/intake/pdf", status_code=201)
    async def intake_pdf(body: PdfIntake, actor=Depends(owner_actor), idempotency_key=Depends(key)):
        """A PDF nota: rendered to images locally, read page by page, one draft."""
        if not vision_api_key:
            raise DomainError("vision_not_configured", "Set STEPFUN_API_KEY before reading invoice PDFs.", 503)
        try:
            pdf_bytes = base64.b64decode(body.pdf_base64, validate=True)
        except Exception:
            raise DomainError("invalid_request", "pdf_base64 is not valid base64.", 422)
        try:
            pages = render_pdf_pages(pdf_bytes)
        except PdfRenderError as exc:
            raise DomainError("pdf_invalid", str(exc), 422)
        merged = {"items": [], "warnings": []}
        supplier = body.supplier
        for page in pages:
            try:
                extracted = await extract_invoice(
                    page,
                    media_type="image/jpeg",
                    restaurant=actor["restaurant_name"],
                    api_key=vision_api_key,
                    model=vision_model,
                    base_url=vision_base_url,
                    client=vision_http_client,
                )
            except StepFunVisionError as exc:
                logger.warning("StepFun vision PDF intake failed: %s", exc)
                raise DomainError("vision_failed", str(exc), exc.status)
            merged["items"].extend(extracted.get("items") or [])
            merged["warnings"].extend(extracted.get("warnings") or [])
            supplier = supplier or extracted.get("supplier")
        merged = await filter_kitchen_items(merged, actor["restaurant_id"])
        payload = {"text": f"Nota PDF ({len(pages)} halaman)", "supplier": supplier}
        return {"data": await run_in_threadpool(store.intake, idempotency_key, payload, merged, actor["restaurant_id"])}

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
            # A physical stock count takes priority over stock/data questions;
            # both are read-only pre-ingest lookups with the ledger as source.
            payload["opname_answer"] = await build_opname_answer(payload["text"], actor["restaurant_id"])
            if payload["opname_answer"] is None:
                payload["stock_answer"] = await build_stock_answer(payload["text"], actor["restaurant_id"])
            if payload["opname_answer"] is None and payload["stock_answer"] is None:
                extracted = await filter_kitchen_items(
                    await extract_text(payload["text"], actor["restaurant_name"]), actor["restaurant_id"])
                hint = await maybe_intent_hint(payload["text"], extracted)
                if hint:
                    payload["intent_hint"] = hint
                payload["extracted_text"] = extracted
        return {"data": await run_in_threadpool(store.whatsapp_ingest, idempotency_key, payload, restaurant_id=actor["restaurant_id"])}

    # ---- Per-restaurant agent rules and proactive checks -------------------

    @app.get("/v1/stock-counts")
    def stock_counts_list(actor=Depends(owner_actor)):
        return {"data": {"counts": store.recent_stock_counts(actor["restaurant_id"])}}

    @app.post("/v1/stock-counts", status_code=201)
    def stock_count_apply(body: StockCountCreate, actor=Depends(owner_actor), idempotency_key=Depends(key)):
        """Apply a dashboard physical stock count correction for one ingredient."""
        return {"data": store.record_stock_count(actor["restaurant_id"], body.ingredient_id,
                                                 body.counted_quantity, body.note)}

    @app.get("/v1/rules")
    def rules_list(actor=Depends(owner_actor)):
        return {"data": {"rules": store.list_rules(actor["restaurant_id"])}}

    @app.post("/v1/rules", status_code=201)
    def rules_add(body: RuleCreate, actor=Depends(owner_actor)):
        return {"data": store.add_rule(actor["restaurant_id"], body.rule)}

    @app.patch("/v1/rules/{rule_id}")
    def rules_edit(rule_id: str, body: RulePatch, actor=Depends(owner_actor)):
        if body.rule is None and body.enabled is None:
            raise DomainError("rule_no_change", "Provide a rule text or an enabled flag.", 422)
        return {"data": store.set_rule(actor["restaurant_id"], rule_id, rule_text=body.rule, enabled=body.enabled)}

    @app.delete("/v1/rules/{rule_id}")
    def rules_delete(rule_id: str, actor=Depends(owner_actor)):
        return {"data": store.delete_rule(actor["restaurant_id"], rule_id)}

    @app.post("/v1/notifications/check")
    async def notifications_check(dry_run: bool = False, actor=Depends(owner_actor)):
        return {"data": await run_proactive_check(actor["restaurant_id"], dry_run=dry_run)}

    # ---- Owner-managed channel links: one phone may serve many platforms ----

    @app.get("/v1/links")
    def links_list(actor=Depends(owner_actor)):
        return {"data": {"links": store.linked_numbers(actor["restaurant_id"])}}

    @app.post("/v1/links", status_code=201)
    def links_add(body: WhatsAppNumber, actor=Depends(owner_actor), idempotency_key=Depends(key)):
        return {"data": store.add_whatsapp_number(actor["restaurant_id"], idempotency_key, body.model_dump())}

    @app.delete("/v1/links/{phone}")
    def links_remove(phone: str, actor=Depends(owner_actor), idempotency_key=Depends(key), platform: Literal["whatsapp", "telegram"] = "whatsapp"):
        return {"data": store.remove_whatsapp_number(actor["restaurant_id"], phone, idempotency_key, platform=platform)}

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
    def admin_remove_number(restaurant_id: str, phone: str, actor=Depends(admin_actor), idempotency_key=Depends(key), platform: Literal["whatsapp", "telegram"] = "whatsapp"):
        return {"data": store.remove_whatsapp_number(restaurant_id, phone, idempotency_key, platform=platform)}

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
                    payload["extracted"] = await filter_kitchen_items(
                        await extract_invoice(
                            photo,
                            media_type=media_type,
                            restaurant=sender_restaurant["name"],
                            api_key=vision_api_key,
                            model=vision_model,
                            base_url=vision_base_url,
                            client=vision_http_client,
                        ),
                        restaurant_id,
                    )
                    payload["source_label"] = "Foto nota WhatsApp"
                except (MetaWhatsAppError, StepFunVisionError) as exc:
                    # A failed read falls back to the safe media reply; no draft.
                    logger.warning("WhatsApp photo intake failed: %s", exc)
            elif message.get("media_kind") == "pdf" and vision_api_key and sender_restaurant and message.get("media_id"):
                # A PDF nota: rendered to images locally, read page by page,
                # merged into one draft.
                try:
                    pdf_bytes = await download_pdf(
                        message["media_id"],
                        meta_access_token,
                        meta_graph_api_version,
                        client=meta_http_client,
                    )
                    pages = render_pdf_pages(pdf_bytes)
                    merged = {"items": [], "warnings": []}
                    supplier = None
                    for page in pages:
                        extracted = await filter_kitchen_items(
                            await extract_invoice(
                                page,
                                media_type="image/jpeg",
                                restaurant=sender_restaurant["name"],
                                api_key=vision_api_key,
                                model=vision_model,
                                base_url=vision_base_url,
                                client=vision_http_client,
                            ),
                            restaurant_id,
                        )
                        merged["items"].extend(extracted.get("items") or [])
                        merged["warnings"].extend(extracted.get("warnings") or [])
                        supplier = supplier or extracted.get("supplier")
                    if supplier:
                        merged["supplier"] = supplier
                    payload["extracted"] = merged
                    payload["source_label"] = f"PDF nota WhatsApp ({len(pages)} halaman)"
                except (PdfRenderError, MetaWhatsAppError, StepFunVisionError) as exc:
                    logger.warning("WhatsApp PDF intake failed: %s", exc)
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
                        api_key=asr_api_key,
                        model=asr_model,
                        base_url=asr_base_url,
                        client=vision_http_client,
                    )
                    if payload["transcript"].strip():
                        extracted = await filter_kitchen_items(
                            await extract_text(payload["transcript"], sender_restaurant["name"]), restaurant_id)
                        hint = await maybe_intent_hint(payload["transcript"], extracted)
                        if hint:
                            payload["intent_hint"] = hint
                        payload["extracted_text"] = extracted
                except (MetaWhatsAppError, StepFunAudioError) as exc:
                    logger.warning("WhatsApp voice intake failed: %s", exc)
            elif not message.get("media_kind") and payload["text"].strip() and sender_restaurant:
                # Plain text gets the same rules-then-model rescue so free-form
                # sentences reach the draft flow; commands skip the model. A
                # stock question is answered read-only from the sender's own
                # restaurant inventory before any of that.
                payload["opname_answer"] = await build_opname_answer(payload["text"], restaurant_id)
                if payload["opname_answer"] is None:
                    payload["stock_answer"] = await build_stock_answer(payload["text"], restaurant_id)
                if payload["opname_answer"] is None and payload["stock_answer"] is None:
                    extracted = await filter_kitchen_items(
                        await extract_text(payload["text"], sender_restaurant["name"]), restaurant_id)
                    hint = await maybe_intent_hint(payload["text"], extracted)
                    if hint:
                        payload["intent_hint"] = hint
                    payload["extracted_text"] = extracted
            result = await run_in_threadpool(
                store.whatsapp_ingest,
                meta_event_key(meta_phone_number_id, event_id),
                payload,
                transport=LIVE,
                channel=LIVE + ":" + meta_phone_number_id,
                outbound_status=OUTBOUND_PENDING,
                restaurant_id=restaurant_id,
                platform="whatsapp",
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

    @app.post(TELEGRAM_WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
        if not (telegram_bot_token and telegram_webhook_secret):
            raise DomainError("telegram_not_configured", "Set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET before accepting Telegram updates.", 503)
        if request.url.query:
            raise DomainError("invalid_webhook_url", "Configure the webhook without query parameters.", 400)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise DomainError("invalid_content_type", "Telegram webhook requires JSON.", 415)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise DomainError("webhook_too_large", "Webhook payload exceeds 64 KiB.", 413)
        # Telegram authenticates the hook with a shared secret header instead
        # of a body signature; the comparison is constant-time either way.
        if not matches_telegram_secret(request.headers.get("x-telegram-bot-api-secret-token", ""), telegram_webhook_secret):
            raise DomainError("invalid_telegram_secret", "Telegram webhook secret mismatch.", 403)
        try:
            messages = parse_telegram_update(bytes(raw))
        except TelegramError as exc:
            logger.warning("Rejected Telegram webhook: %s", exc)
            raise DomainError("invalid_telegram_update", str(exc), exc.status)

        for message in messages:
            payload = {key: message[key] for key in ("sender", "text", "event_id", "num_media")}
            payload["_restaurant_id"] = None
            # The same allowlist as WhatsApp, scoped to the Telegram platform:
            # the same digits may belong to another restaurant on WhatsApp.
            sender_restaurant = await run_in_threadpool(store.restaurant_for_sender, message["sender"], "telegram")
            restaurant_id = sender_restaurant["id"] if sender_restaurant else None
            payload["_restaurant_id"] = restaurant_id
            if message.get("media_group_id") and telegram_group_seconds > 0:
                # Part of a multi-photo send: extract now, buffer the result,
                # and let the quiet-period flush deliver one merged draft.
                group_key = f"{message['sender']}:{message['media_group_id']}"
                async with telegram_group_lock:
                    entry = telegram_groups.setdefault(group_key, {
                        "update_ids": set(), "parts": [], "sender": message["sender"],
                        "restaurant_id": restaurant_id, "event_id": message["event_id"]})
                    replay = message["event_id"] in entry["update_ids"]
                    entry["update_ids"].add(message["event_id"])
                if replay:
                    continue  # a retried update; the flush owns the one reply
                part = {"extracted": None}
                if message.get("media_kind") == "image" and vision_api_key and sender_restaurant and message.get("media_id"):
                    try:
                        photo, _media_type = await download_telegram_file(
                            telegram_bot_token,
                            message["media_id"],
                            api_base=telegram_api_base,
                            client=telegram_http_client,
                        )
                        part["extracted"] = await filter_kitchen_items(
                            await extract_invoice(
                                photo,
                                media_type="image/jpeg",
                                restaurant=sender_restaurant["name"],
                                api_key=vision_api_key,
                                model=vision_model,
                                base_url=vision_base_url,
                                client=vision_http_client,
                            ),
                            restaurant_id,
                        )
                    except (TelegramError, StepFunVisionError) as exc:
                        logger.warning("Telegram grouped photo intake failed: %s", exc)
                async with telegram_group_lock:
                    entry = telegram_groups.get(group_key)
                    if entry is None:
                        continue  # flushed between the two locks
                    entry["parts"].append(part)
                    old_task = entry.get("task")
                if old_task:
                    old_task.cancel()
                entry["task"] = asyncio.create_task(_flush_group_later(group_key))
                continue
            if message.get("media_kind") == "pdf" and vision_api_key and sender_restaurant and message.get("media_id"):
                # A PDF nota: render pages locally, read each with vision,
                # merge the line items into one draft.
                try:
                    pdf_bytes, _mime = await download_telegram_file(
                        telegram_bot_token,
                        message["media_id"],
                        api_base=telegram_api_base,
                        client=telegram_http_client,
                        max_bytes=TG_MAX_PDF_BYTES,
                    )
                    pages = render_pdf_pages(pdf_bytes)
                    merged = {"items": [], "warnings": []}
                    supplier = None
                    for page in pages:
                        extracted = await filter_kitchen_items(
                            await extract_invoice(
                                page,
                                media_type="image/jpeg",
                                restaurant=sender_restaurant["name"],
                                api_key=vision_api_key,
                                model=vision_model,
                                base_url=vision_base_url,
                                client=vision_http_client,
                            ),
                            restaurant_id,
                        )
                        merged["items"].extend(extracted.get("items") or [])
                        merged["warnings"].extend(extracted.get("warnings") or [])
                        supplier = supplier or extracted.get("supplier")
                    if supplier:
                        merged["supplier"] = supplier
                    payload["extracted"] = merged
                    payload["source_label"] = f"PDF nota Telegram ({len(pages)} halaman)"
                except (TelegramError, PdfRenderError, StepFunVisionError) as exc:
                    logger.warning("Telegram PDF intake failed: %s", exc)
            elif message.get("media_kind") == "image" and vision_api_key and sender_restaurant and message.get("media_id"):
                try:
                    photo, _media_type = await download_telegram_file(
                        telegram_bot_token,
                        message["media_id"],
                        api_base=telegram_api_base,
                        client=telegram_http_client,
                    )
                    payload["extracted"] = await filter_kitchen_items(
                        await extract_invoice(
                            photo,
                            media_type="image/jpeg",
                            restaurant=sender_restaurant["name"],
                            api_key=vision_api_key,
                            model=vision_model,
                            base_url=vision_base_url,
                            client=vision_http_client,
                        ),
                        restaurant_id,
                    )
                    payload["source_label"] = "Foto nota Telegram"
                except (TelegramError, StepFunVisionError) as exc:
                    logger.warning("Telegram photo intake failed: %s", exc)
            elif message.get("media_kind") == "audio" and asr_api_key and sender_restaurant and message.get("media_id"):
                try:
                    audio, _mime = await download_telegram_file(
                        telegram_bot_token,
                        message["media_id"],
                        api_base=telegram_api_base,
                        client=telegram_http_client,
                    )
                    payload["transcript"] = await transcribe_audio(
                        audio,
                        "audio/ogg",
                        api_key=asr_api_key,
                        model=asr_model,
                        base_url=asr_base_url,
                        client=vision_http_client,
                    )
                    if payload["transcript"].strip():
                        extracted = await filter_kitchen_items(
                            await extract_text(payload["transcript"], sender_restaurant["name"]), restaurant_id)
                        hint = await maybe_intent_hint(payload["transcript"], extracted)
                        if hint:
                            payload["intent_hint"] = hint
                        payload["extracted_text"] = extracted
                except (TelegramError, StepFunAudioError) as exc:
                    logger.warning("Telegram voice intake failed: %s", exc)
            elif not message.get("media_kind") and payload["text"].strip() and sender_restaurant:
                # Text walks the exact same rules-then-rescue-then-stock path
                # as WhatsApp text; only the transport differs.
                payload["opname_answer"] = await build_opname_answer(payload["text"], restaurant_id)
                if payload["opname_answer"] is None:
                    payload["stock_answer"] = await build_stock_answer(payload["text"], restaurant_id)
                if payload["opname_answer"] is None and payload["stock_answer"] is None:
                    extracted = await filter_kitchen_items(
                        await extract_text(payload["text"], sender_restaurant["name"]), restaurant_id)
                    hint = await maybe_intent_hint(payload["text"], extracted)
                    if hint:
                        payload["intent_hint"] = hint
                    payload["extracted_text"] = extracted
            await deliver_telegram(payload, message)


    bot_channel = ("telegram:" + bot_id_from_token(telegram_bot_token)) if telegram_bot_token else None
    # Photos sent together as one Telegram media group share a group id;
    # they arrive as separate updates, so each photo's extraction is
    # buffered and flushed once the group goes quiet — one merged draft,
    # one reply. The in-process buffer is enough for the single-process
    # demo; update ids dedupe Telegram's webhook retries.
    telegram_groups = {}
    telegram_group_lock = asyncio.Lock()
    import traceback


    async def _flush_group_later(group_key):
        await asyncio.sleep(telegram_group_seconds)
        await flush_telegram_group(group_key)

    async def flush_telegram_group(group_key):
        # The entry stays in the buffer so Telegram's webhook retries of
        # the same update ids keep deduplicating; only parts not yet
        # delivered are flushed, so a late-arriving photo becomes its own
        # follow-up draft instead of duplicating lines.
        async with telegram_group_lock:
            entry = telegram_groups.get(group_key)
            if not entry:
                return
            delivered = entry.get("delivered", 0)
            parts = entry["parts"][delivered:]
            if not parts:
                return
            entry["delivered"] = delivered + len(parts)

        payload = {"sender": entry["sender"], "text": "", "event_id": entry["event_id"],
                   "num_media": max(1, len(parts)), "_restaurant_id": entry["restaurant_id"]}
        extracted_parts = [part for part in parts if part.get("extracted")]
        if extracted_parts:
            merged_items, merged_warnings, supplier = [], [], None
            for part in extracted_parts:
                merged_items.extend(part["extracted"].get("items") or [])
                merged_warnings.extend(part["extracted"].get("warnings") or [])
                supplier = supplier or part["extracted"].get("supplier")
            payload["extracted"] = {"items": merged_items, "warnings": merged_warnings, "supplier": supplier}
            payload["source_label"] = f"{len(parts)} foto nota Telegram"
        try:
            await deliver_telegram(payload, {"sender": entry["sender"]})
        except DomainError as exc:
            logger.warning("Telegram media group delivery failed: %s", exc)

    async def deliver_telegram(payload, message):
        result = await run_in_threadpool(
            store.whatsapp_ingest,
            "telegram:" + bot_channel + ":" + payload["event_id"],
            payload,
            transport=TG_TRANSPORT,
            channel=bot_channel,
            outbound_status=TG_OUTBOUND_PENDING,
            restaurant_id=payload.get("_restaurant_id"),
            platform="telegram",
        )
        outbound = result["messages"][-1]
        claimed = await run_in_threadpool(
            store.claim_whatsapp_message,
            outbound["id"],
            TG_OUTBOUND_PENDING,
            TG_OUTBOUND_SENDING,
        )
        if not claimed:
            # A Telegram retry of an update already answered; the stored
            # response replays but the bot must not double-send.
            return
        try:
            await send_telegram_text(
                telegram_bot_token,
                message["sender"],
                outbound["body"],
                api_base=telegram_api_base,
                client=telegram_http_client,
            )
        except (TelegramError, ValueError) as exc:
            await run_in_threadpool(store.set_whatsapp_message_status, outbound["id"], TG_OUTBOUND_PENDING)
            status = exc.status if isinstance(exc, TelegramError) else 500
            raise DomainError("telegram_send_failed", str(exc), status)
        await run_in_threadpool(store.set_whatsapp_message_status, outbound["id"], TG_OUTBOUND_SENT)


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
