"""Twilio transport helpers. This module never calls the Twilio REST API."""

import hashlib
import re
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring


WEBHOOK_PATH = "/webhooks/twilio/whatsapp"
SIMULATOR = "twilio_simulator"
LIVE = "twilio_whatsapp"
TRIAL_POLL_REPLY_STATUS = "not_delivered_trial_poll"


def twilio_event_key(recipient, sid):
    """Return the same opaque idempotency key for webhook and polling intake."""
    channel = LIVE + ":" + recipient
    return "twilio:" + hashlib.sha256((channel + "\0" + sid).encode()).hexdigest()


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


def validate_signature(token, url, params, signature):
    # The official helper owns Twilio's URL/form canonicalization and comparison.
    from twilio.request_validator import RequestValidator

    return bool(signature) and RequestValidator(token).validate(url, params, signature)


def twiml_reply(body):
    root = Element("Response")
    message = SubElement(root, "Message")
    # XML 1.0 excludes control characters even when HTML escaping is applied.
    message.text = re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", body)
    return tostring(root, encoding="unicode", xml_declaration=True)


def draft_reply(draft):
    reference = f"Draf {draft['id'][:8]} v{draft['version']}"
    if draft["status"] == "needs_clarification":
        return f"{reference} perlu dilengkapi. Buka dashboard untuk koreksi dan simpan. Setelah valid, balas YA untuk mencatat barang diterima, atau BATAL untuk menolak."
    names = {"cabai": "Cabai", "telur": "Telur", "beras": "Beras", "minyak": "Minyak", "bawang": "Bawang"}
    lines = [f"{names.get(item['ingredient_id'], 'Item')} {item['quantity']} {item['unit']}: Rp{item['line_total_idr']:,}".replace(",", ".") for item in draft["items"]]
    return f"{reference}\n" + "\n".join(lines) + f"\nTotal Rp{draft['total_idr']:,}. Balas YA hanya jika detail benar dan barang sudah diterima; BATAL untuk menolak. Status pembayaran tidak diketahui.".replace(",", ".")
