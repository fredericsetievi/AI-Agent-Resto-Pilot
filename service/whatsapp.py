"""Meta WhatsApp Cloud API transport helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger("restopilot.whatsapp")

WEBHOOK_PATH = "/webhooks/meta/whatsapp"
SIMULATOR = "whatsapp_simulator"
LIVE = "meta_whatsapp_cloud"
OUTBOUND_PENDING = "meta_send_pending"
OUTBOUND_SENDING = "meta_sending"
OUTBOUND_SENT = "meta_sent"

_META_ID = re.compile(r"[1-9]\d{5,30}")
_MESSAGE_ID = re.compile(r"[\x21-\x7e]{1,500}")
_PHONE = re.compile(r"[1-9]\d{6,14}")
_GRAPH_VERSION = re.compile(r"v[1-9]\d{0,2}\.0")
# Media IDs are opaque strings; accept the documented alphabet and bound length.
_MEDIA_ID = re.compile(r"[A-Za-z0-9_-]{5,200}")
IMAGE_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")
MAX_MEDIA_BYTES = 5 * 1024 * 1024


class MetaWhatsAppError(Exception):
    """Safe provider error that never includes credentials or response bodies."""

    def __init__(self, message, status=502):
        self.status = status
        super().__init__(message)


def public_origin(value):
    if not value:
        return None
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    parsed.port
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin without a path, query, or credentials.")
    return value


def matches_public_host(host, origin):
    if not origin:
        return False
    try:
        expected = urlsplit(origin)
        actual = urlsplit("https://" + host)
        return actual.hostname == expected.hostname and (actual.port or 443) == (expected.port or 443) and not (actual.username or actual.password or actual.path or actual.query or actual.fragment)
    except ValueError:
        return False


def validate_meta_id(value, name):
    value = (value or "").strip()
    if not _META_ID.fullmatch(value):
        raise ValueError(f"{name} must be a numeric Meta identifier.")
    return value


def validate_graph_version(value):
    value = (value or "").strip()
    if not _GRAPH_VERSION.fullmatch(value):
        raise ValueError("META_GRAPH_API_VERSION must use the form v<major>.0.")
    return value


def validate_verify_token(value):
    value = (value or "").strip()
    if len(value) < 16 or len(value) > 256 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("META_WEBHOOK_VERIFY_TOKEN must be 16-256 printable non-space characters.")
    return value


def matches_verify_token(candidate, configured):
    return bool(candidate and configured) and hmac.compare_digest(candidate, configured)


def validate_webhook_signature(app_secret, raw, signature):
    if not app_secret or not signature or not signature.startswith("sha256="):
        return False
    supplied = signature.removeprefix("sha256=")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied.lower(), expected)


def meta_event_key(phone_number_id, message_id):
    channel = LIVE + ":" + phone_number_id
    return "meta:" + hashlib.sha256((channel + "\0" + message_id).encode()).hexdigest()


def parse_webhook(raw, *, waba_id, phone_number_id):
    """Return normalized inbound messages; delivery/status-only callbacks return []."""
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MetaWhatsAppError("Malformed Meta WhatsApp webhook JSON.", 400)
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        raise MetaWhatsAppError("Invalid Meta WhatsApp webhook envelope.", 400)
    entries = payload.get("entry")
    if not isinstance(entries, list) or not entries or len(entries) > 10:
        raise MetaWhatsAppError("Invalid Meta WhatsApp webhook entries.", 400)

    normalized = []
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get("id", "")) != waba_id:
            raise MetaWhatsAppError("Meta WhatsApp webhook has an unexpected account ID.", 403)
        changes = entry.get("changes")
        if not isinstance(changes, list) or len(changes) > 20:
            raise MetaWhatsAppError("Invalid Meta WhatsApp webhook changes.", 400)
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "messages" or not isinstance(change.get("value"), dict):
                raise MetaWhatsAppError("Invalid Meta WhatsApp webhook change.", 400)
            value = change["value"]
            metadata = value.get("metadata")
            if not isinstance(metadata, dict) or str(metadata.get("phone_number_id", "")) != phone_number_id:
                raise MetaWhatsAppError("Meta WhatsApp webhook has an unexpected phone number ID.", 403)
            messages = value.get("messages", [])
            if not isinstance(messages, list) or len(messages) > 20:
                raise MetaWhatsAppError("Invalid Meta WhatsApp webhook messages.", 400)
            for message in messages:
                normalized.append(_normalize_message(message))
            statuses = value.get("statuses", [])
            if not isinstance(statuses, list) or len(statuses) > 50:
                raise MetaWhatsAppError("Invalid Meta WhatsApp webhook statuses.", 400)
            for status in statuses:
                if not isinstance(status, dict):
                    raise MetaWhatsAppError("Invalid Meta WhatsApp status update.", 400)
                state = status.get("status")
                if state not in ("sent", "delivered", "read", "failed", "deleted"):
                    raise MetaWhatsAppError("Invalid Meta WhatsApp message status.", 400)
                errors = status.get("errors", [])
                error_codes = [item.get("code") for item in errors if isinstance(item, dict) and isinstance(item.get("code"), int)] if isinstance(errors, list) else []
                logger.warning("Meta WhatsApp delivery status: %s error_codes=%s", state, error_codes)
    return normalized


def _normalize_message(message):
    if not isinstance(message, dict):
        raise MetaWhatsAppError("Invalid Meta WhatsApp message.", 400)
    sender = str(message.get("from", ""))
    message_id = str(message.get("id", ""))
    if not _PHONE.fullmatch(sender):
        raise MetaWhatsAppError("Invalid Meta WhatsApp sender.", 400)
    if not _MESSAGE_ID.fullmatch(message_id):
        raise MetaWhatsAppError("Invalid Meta WhatsApp message ID.", 400)
    if message.get("type") == "text":
        text_value = message.get("text")
        text = text_value.get("body") if isinstance(text_value, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
            raise MetaWhatsAppError("Invalid Meta WhatsApp text message.", 400)
        text, num_media = text.strip(), 0
        media_id = None
    elif message.get("type") == "image":
        image = message.get("image")
        media_id = image.get("id") if isinstance(image, dict) else None
        if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
            raise MetaWhatsAppError("Invalid Meta WhatsApp image message.", 400)
        text, num_media = "", 1
    else:
        text, num_media, media_id = "", 1, None
    return {"sender": sender, "text": text, "event_id": message_id, "num_media": num_media, "media_id": media_id}


async def download_image(media_id, access_token, graph_version, *, client=None):
    """Fetch one inbound photo: resolve the media URL, then download the bytes.

    Meta serves media in two hops and requires the access token on both. Only
    the documented image types and a 5 MiB cap are accepted.
    """
    if not access_token or not access_token.strip():
        raise MetaWhatsAppError("Meta WhatsApp Cloud API is not configured.", 503)
    if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
        raise MetaWhatsAppError("Invalid Meta WhatsApp media ID.", 400)
    graph_version = validate_graph_version(graph_version)
    headers = {"Authorization": f"Bearer {access_token}"}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        try:
            lookup = await client.get(f"https://graph.facebook.com/{graph_version}/{media_id}", headers=headers)
        except httpx.HTTPError:
            raise MetaWhatsAppError("Meta WhatsApp media could not be reached.")
        if lookup.status_code != 200:
            raise MetaWhatsAppError(f"Meta WhatsApp rejected the media lookup (HTTP {lookup.status_code}).")
        try:
            media = lookup.json()
        except ValueError:
            raise MetaWhatsAppError("Meta WhatsApp returned an invalid media response.")
        url = media.get("url") if isinstance(media, dict) else None
        mime = media.get("mime_type") if isinstance(media, dict) else None
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != "https" or not parsed.hostname:
            raise MetaWhatsAppError("Meta WhatsApp returned an unusable media URL.")
        if mime not in IMAGE_MEDIA_TYPES:
            raise MetaWhatsAppError("Meta WhatsApp sent an unsupported image type.")
        try:
            download = await client.get(url, headers=headers)
        except httpx.HTTPError:
            raise MetaWhatsAppError("Meta WhatsApp media download failed.")
        if download.status_code != 200:
            raise MetaWhatsAppError(f"Meta WhatsApp media download failed (HTTP {download.status_code}).")
        raw = download.content
        if not raw or len(raw) > MAX_MEDIA_BYTES:
            raise MetaWhatsAppError("Meta WhatsApp image is empty or exceeds 5 MiB.")
        return raw, mime
    finally:
        if owns_client:
            await client.aclose()


async def send_text(access_token, phone_number_id, graph_version, recipient, body, *, client=None):
    if not access_token or not access_token.strip():
        raise MetaWhatsAppError("Meta WhatsApp Cloud API is not configured.", 503)
    phone_number_id = validate_meta_id(phone_number_id, "META_PHONE_NUMBER_ID")
    graph_version = validate_graph_version(graph_version)
    if not _PHONE.fullmatch(recipient):
        raise MetaWhatsAppError("Invalid Meta WhatsApp recipient.", 500)
    url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=5.0, follow_redirects=False)
    try:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.HTTPError:
        raise MetaWhatsAppError("Meta WhatsApp Cloud API could not be reached.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise MetaWhatsAppError(f"Meta WhatsApp Cloud API rejected the reply (HTTP {response.status_code}).", status)
    try:
        message_id = response.json()["messages"][0]["id"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise MetaWhatsAppError("Meta WhatsApp Cloud API returned an invalid success response.")
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
        raise MetaWhatsAppError("Meta WhatsApp Cloud API returned an invalid message ID.")
    return message_id


def draft_reply(draft):
    reference = f"Draf {draft['id'][:8]} v{draft['version']}"
    if draft["status"] == "needs_clarification":
        return f"{reference} perlu dilengkapi. Buka dashboard untuk koreksi dan simpan. Setelah valid, balas YA untuk mencatat barang diterima, atau BATAL untuk menolak."
    names = {"cabai": "Cabai", "telur": "Telur", "beras": "Beras", "minyak": "Minyak", "bawang": "Bawang"}
    lines = [f"{names.get(item['ingredient_id'], 'Item')} {item['quantity']} {item['unit']}: Rp{item['line_total_idr']:,}".replace(",", ".") for item in draft["items"]]
    return f"{reference}\n" + "\n".join(lines) + f"\nTotal Rp{draft['total_idr']:,}. Balas YA hanya jika detail benar dan barang sudah diterima; BATAL untuk menolak. Status pembayaran tidak diketahui.".replace(",", ".")
