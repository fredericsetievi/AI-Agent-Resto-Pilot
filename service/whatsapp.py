"""Meta WhatsApp Cloud API transport helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from decimal import Decimal, InvalidOperation
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
AUDIO_MEDIA_TYPES = ("audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr")
MAX_MEDIA_BYTES = 5 * 1024 * 1024
_AUDIO_FILENAMES = {
    "audio/ogg": "audio.ogg",
    "audio/mpeg": "audio.mp3",
    "audio/mp4": "audio.m4a",
    "audio/aac": "audio.aac",
    "audio/amr": "audio.amr",
}


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
        text, num_media, media_id, media_kind = text.strip(), 0, None, None
    elif message.get("type") == "image":
        image = message.get("image")
        media_id = image.get("id") if isinstance(image, dict) else None
        if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
            raise MetaWhatsAppError("Invalid Meta WhatsApp image message.", 400)
        text, num_media, media_kind = "", 1, "image"
    elif message.get("type") in ("audio", "voice"):
        audio = message.get(message["type"])
        media_id = audio.get("id") if isinstance(audio, dict) else None
        if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
            raise MetaWhatsAppError("Invalid Meta WhatsApp audio message.", 400)
        text, num_media, media_kind = "", 1, "audio"
    else:
        text, num_media, media_id, media_kind = "", 1, None, None
    return {"sender": sender, "text": text, "event_id": message_id, "num_media": num_media, "media_id": media_id, "media_kind": media_kind}


async def _download_media(media_id, access_token, graph_version, *, allowed_types, label, cap, client=None):
    """Fetch one inbound attachment: resolve the media URL, then download the bytes.

    Meta serves media in two hops and requires the access token on both. Only
    the documented types for this label and the given byte cap are accepted.
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
        if mime not in allowed_types:
            raise MetaWhatsAppError(f"Meta WhatsApp sent an unsupported {label} type.")
        try:
            download = await client.get(url, headers=headers)
        except httpx.HTTPError:
            raise MetaWhatsAppError("Meta WhatsApp media download failed.")
        if download.status_code != 200:
            raise MetaWhatsAppError(f"Meta WhatsApp media download failed (HTTP {download.status_code}).")
        raw = download.content
        if not raw or len(raw) > cap:
            raise MetaWhatsAppError(f"Meta WhatsApp {label} is empty or exceeds {cap // (1024 * 1024)} MiB.")
        return raw, mime
    finally:
        if owns_client:
            await client.aclose()


async def download_image(media_id, access_token, graph_version, *, client=None):
    """Fetch one inbound photo: resolve the media URL, then download the bytes.

    Meta serves media in two hops and requires the access token on both. Only
    the documented image types and a 5 MiB cap are accepted.
    """
    return await _download_media(
        media_id,
        access_token,
        graph_version,
        allowed_types=IMAGE_MEDIA_TYPES,
        label="image",
        cap=MAX_MEDIA_BYTES,
        client=client,
    )


async def download_audio(media_id, access_token, graph_version, *, client=None):
    """Fetch one inbound voice note: resolve the media URL, then download the bytes.

    Meta serves media in two hops and requires the access token on both. Only
    the documented audio types and a 5 MiB cap are accepted.
    """
    return await _download_media(
        media_id,
        access_token,
        graph_version,
        allowed_types=AUDIO_MEDIA_TYPES,
        label="audio",
        cap=MAX_MEDIA_BYTES,
        client=client,
    )


async def _send(access_token, phone_number_id, graph_version, recipient, payload, *, client=None):
    if not access_token or not access_token.strip():
        raise MetaWhatsAppError("Meta WhatsApp Cloud API is not configured.", 503)
    phone_number_id = validate_meta_id(phone_number_id, "META_PHONE_NUMBER_ID")
    graph_version = validate_graph_version(graph_version)
    if not _PHONE.fullmatch(recipient):
        raise MetaWhatsAppError("Invalid Meta WhatsApp recipient.", 500)
    url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
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


async def send_text(access_token, phone_number_id, graph_version, recipient, body, *, client=None):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    return await _send(access_token, phone_number_id, graph_version, recipient, payload, client=client)


async def send_audio_message(access_token, phone_number_id, graph_version, recipient, audio_id, *, client=None):
    """Send a previously uploaded audio attachment (a Meta media ID) as a reply."""
    if not isinstance(audio_id, str) or not _MEDIA_ID.fullmatch(audio_id):
        raise MetaWhatsAppError("Invalid Meta WhatsApp audio media ID.", 500)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "audio",
        "audio": {"id": audio_id},
    }
    return await _send(access_token, phone_number_id, graph_version, recipient, payload, client=client)


async def upload_audio(access_token, phone_number_id, graph_version, data, *, mime="audio/mpeg", client=None):
    """Upload one audio attachment and return the Meta media ID used to send it."""
    if not access_token or not access_token.strip():
        raise MetaWhatsAppError("Meta WhatsApp Cloud API is not configured.", 503)
    phone_number_id = validate_meta_id(phone_number_id, "META_PHONE_NUMBER_ID")
    graph_version = validate_graph_version(graph_version)
    if not data:
        raise MetaWhatsAppError("Meta WhatsApp refuses an empty audio upload.", 400)
    url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/media"
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        try:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                files={"file": (_AUDIO_FILENAMES.get(mime, "audio"), data, mime)},
                data={"messaging_product": "whatsapp", "type": "audio"},
            )
        except httpx.HTTPError:
            raise MetaWhatsAppError("Meta WhatsApp media upload could not be reached.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise MetaWhatsAppError(f"Meta WhatsApp rejected the media upload (HTTP {response.status_code}).", status)
    try:
        media_id = response.json()["id"]
    except (ValueError, KeyError, TypeError):
        raise MetaWhatsAppError("Meta WhatsApp returned an invalid media upload response.")
    if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
        raise MetaWhatsAppError("Meta WhatsApp returned an invalid media ID.")
    return media_id


CATALOG_NAMES = {
    "cabai": "Cabai merah",
    "telur": "Telur ayam",
    "beras": "Beras",
    "minyak": "Minyak goreng",
    "bawang": "Bawang merah",
    "bawang_putih": "Bawang putih",
    "kecombrang": "Kecombrang",
    "gula_merah": "Gula merah",
    "daun_pisang": "Daun pisang",
}
PAYMENT_FOLLOWUP_PROMPT = (
    "Untuk melengkapi catatan: balas 'lunas' atau 'utang' — boleh plus nama penjual, "
    "contoh: 'lunas, dari Pasar Pagi'. Balas 'skip' untuk lewati."
)
FOLLOWUP_ACK = "Oke, dicatat tanpa detail pembayaran."
# Greetings and chat noise parse to zero purchase lines; invite the owner in
# instead of filing an empty draft on the dashboard.
HELP_REPLY = (
    "Halo! 👋 Saya asisten pembelian RestoPilot. Kirim catatan belanja seperti "
    "'Beli bawang 2 kg 80 rb', foto nota, atau voice note — saya langsung buatkan drafnya."
)
# Sent to WhatsApp numbers the admin has not linked to any restaurant. Nothing
# about the sender's message is processed, stored as a draft, or answered.
UNREGISTERED_REPLY = (
    "Maaf, nomor WhatsApp ini belum terdaftar di RestoPilot. "
    "Minta admin untuk menghubungkan nomor Anda dengan resto terlebih dahulu."
)
_PAYMENT_PAID = re.compile(r"(?i)\b(?:lunas(?:\s+semua)?|paid|sudah\s+(?:bayar|dibayar)|udah\s+(?:bayar|lunas))\b")
_PAYMENT_UNPAID = re.compile(r"(?i)\b(?:belum\s+(?:lunas|bayar|dibayar)|utang|hutang|kredit|credit)\b")
_PAYMENT_SKIP = ("skip", "lewati")
_SUPPLIER_LEAD = re.compile(r"(?i)^\s*(?:dari|di)\s+(.+)$")
_SUPPLIER_TAIL = re.compile(r"(?i)\bdari\s+(.{1,100})")
_SUPPLIER_DI_TAIL = re.compile(r"(?i)\bdi\s+(.{1,100})")


def _idr(value):
    """Format an integer rupiah amount with Indonesian thousands separators."""
    return f"{int(value):,}".replace(",", ".")


def _item_line(item, catalog_names=None):
    name = (catalog_names or CATALOG_NAMES).get(item.get("ingredient_id"))
    if not name:
        raw = str(item.get("raw_name") or "").strip()
        name = raw.capitalize() if raw else "Item"
    total = item.get("line_total_idr")
    head = name if item.get("quantity") is None else f"{name} — {item['quantity']} {item.get('unit') or ''}".rstrip()
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        return f"• {head}"
    price = item.get("unit_price_idr")
    if price:
        try:
            unit_price = Decimal(str(price))
        except InvalidOperation:
            unit_price = None
        if unit_price is not None and unit_price > 0 and unit_price == unit_price.to_integral_value():
            return f"• {head} × Rp{_idr(unit_price)} = Rp{_idr(total)}"
    return f"• {head} = Rp{_idr(total)}"


# Raw extraction warnings are admin-facing English; the owner-facing reply
# already covers unmapped items, so only flag "other" problems generically and
# keep line-level detail on the dashboard.
_UNMAPPED_WARNING = re.compile(r"unknown ingredient|not mapped", re.IGNORECASE)


def _draft_total(draft):
    total = draft.get("total_idr")
    if isinstance(total, int) and not isinstance(total, bool) and total > 0:
        return total
    parts = [
        item["line_total_idr"]
        for item in draft.get("items", [])
        if isinstance(item.get("line_total_idr"), int)
        and not isinstance(item["line_total_idr"], bool)
        and item["line_total_idr"] > 0
    ]
    return sum(parts) if parts else None


def draft_reply(draft, catalog_names=None):
    # Display names come from the caller's restaurant catalog when given, so
    # custom bahan from other restaurants can never surface in a reply.
    lines = [_item_line(item, catalog_names) for item in draft["items"]]
    unmapped = [
        str(item["raw_name"]).strip()
        for item in draft["items"]
        if not item.get("ingredient_id") and item.get("raw_name")
    ]
    notes = []
    if unmapped:
        notes.append(
            "Catatan: " + ", ".join(unmapped)
            + " belum ada di daftar bahan — akan ditambahkan otomatis saat kamu balas YA."
        )
    fuzzy = []
    for item in draft.get("items", []):
        typed = str(item.get("matched_from") or "").strip()
        if not typed:
            continue
        name = (catalog_names or CATALOG_NAMES).get(item.get("ingredient_id")) or str(item.get("raw_name") or "").strip()
        fuzzy.append(f"'{typed}' saya baca sebagai {name}")
    if fuzzy:
        notes.append("Catatan: " + "; ".join(fuzzy) + " — tolong dicek dulu, benar kan? Balas YA jika sudah benar.")
    if any(_UNMAPPED_WARNING.search(str(warning)) is None for warning in draft.get("warnings", [])):
        notes.append("Catatan: ada baris yang perlu dicek di dashboard (harga, satuan, atau keterbacaan foto).")
    supplier = str(draft.get("supplier") or "").strip()
    total = _draft_total(draft)
    if draft["status"] == "needs_clarification":
        parts = ["Draf pembelian (perlu konfirmasi):"] + lines
        if supplier:
            parts.append(f"Pemasok: {supplier}")
        if total:
            parts.append(f"Total sementara: Rp{_idr(total)}")
        parts += notes
        if unmapped:
            parts.append("Balas YA untuk konfirmasi semua item di atas — item baru otomatis masuk daftar bahan. Atau perbaiki dulu di dashboard. BATAL untuk batal.")
        else:
            parts.append("Perbaiki di dashboard, lalu balas YA kalau sudah benar & barang diterima. BATAL untuk batal.")
        return "\n".join(parts)
    parts = ["Draf pembelian:"] + lines
    if supplier:
        parts.append(f"Pemasok: {supplier}")
    if total:
        parts.append(f"Total: Rp{_idr(total)}")
    parts += notes
    parts.append("Balas YA jika semua barang sudah diterima, BATAL untuk batal.")
    return "\n".join(parts)


def parse_payment_followup(text):
    """Parse the one-shot payment/supplier reply asked after a recording.

    Returns None when the message does not look like a follow-up answer, so
    callers fall through to normal message handling (e.g. a new draft).
    """
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not candidate:
        return None
    if candidate.casefold() in _PAYMENT_SKIP:
        return {"payment_status": None, "supplier": None, "skip": True}
    unpaid = _PAYMENT_UNPAID.search(candidate)
    paid = _PAYMENT_PAID.search(candidate)
    payment_status = "unpaid" if unpaid else "paid" if paid else None
    supplier = None
    # A message that only opens with "dari/di X" names the supplier outright;
    # mid-sentence "di X" is a common purchase-message shape, so it only
    # counts as a supplier once a payment keyword made the reply explicit.
    match = _SUPPLIER_LEAD.search(candidate) or _SUPPLIER_TAIL.search(candidate) or (_SUPPLIER_DI_TAIL.search(candidate) if payment_status else None)
    if match:
        supplier = re.sub(r"[\s.,;:!?\-\"']+$", "", match.group(1)).strip()[:100] or None
    if payment_status is None and supplier is None:
        return None
    return {"payment_status": payment_status, "supplier": supplier, "skip": False}


def weekly_summary_reply(summary, days=7):
    """Format the weekly ledger summary as a short Indonesian WhatsApp reply."""
    total = f"Rp{summary['total_spend_idr']:,}".replace(",", ".")
    parts = [f"Ringkasan {days} hari: {summary['purchase_count']} pembelian, total {total}."]
    top = summary.get("top_item")
    if top:
        parts.append(f"Terbanyak: {top['name']} {top['quantity']} {top['unit']}.")
    spike = summary.get("biggest_price_spike")
    if spike:
        parts.append(f"Lonjakan harga terbesar: {spike['name']} +{spike['change_percent'].replace('.', ',')}%.")
    return " ".join(parts)
