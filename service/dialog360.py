"""360dialog sandbox transport helpers.

The sandbox is intentionally separate from the production 360dialog API.  It
has one API key/recipient, a 200-message cap, and no signed webhook delivery.
RestoPilot therefore uses an unguessable token in the callback path while this
test-only transport is enabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re

import httpx


SANDBOX_API_ORIGIN = "https://waba-sandbox.360dialog.io"
SANDBOX_MESSAGES_URL = SANDBOX_API_ORIGIN + "/v1/messages"
SANDBOX_WEBHOOK_PREFIX = "/webhooks/360dialog/sandbox"
SANDBOX = "dialog360_sandbox"
OUTBOUND_PENDING = "dialog360_sandbox_send_pending"
OUTBOUND_SENDING = "dialog360_sandbox_sending"
OUTBOUND_SENT = "dialog360_sandbox_sent"

_WEBHOOK_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,128}")
_PHONE = re.compile(r"[1-9]\d{6,14}")
_BSUID = re.compile(r"[A-Za-z0-9._:-]{1,200}")
_MESSAGE_ID = re.compile(r"[\x21-\x7e]{1,500}")


class Dialog360Error(Exception):
    """Safe provider error that never includes credentials or response bodies."""

    def __init__(self, message, status=502):
        self.status = status
        super().__init__(message)


def sandbox_webhook_path(token):
    token = (token or "").strip()
    if not _WEBHOOK_TOKEN.fullmatch(token):
        raise ValueError("DIALOG360_WEBHOOK_TOKEN must be 24-128 URL-safe characters.")
    return f"{SANDBOX_WEBHOOK_PREFIX}/{token}"


def matches_webhook_token(candidate, configured):
    return bool(candidate and configured) and hmac.compare_digest(candidate, configured)


def sandbox_event_key(message_id):
    return "dialog360:" + hashlib.sha256((SANDBOX + "\0" + message_id).encode()).hexdigest()


def parse_sandbox_webhook(raw):
    """Return normalized inbound messages; status-only callbacks return []."""
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Dialog360Error("Malformed 360dialog webhook JSON.", 400)
    if not isinstance(payload, dict):
        raise Dialog360Error("Invalid 360dialog webhook envelope.", 400)

    normalized = []
    if "entry" in payload:
        entries = payload.get("entry")
        if not isinstance(entries, list):
            raise Dialog360Error("Invalid 360dialog webhook envelope.", 400)
        if len(entries) > 10:
            raise Dialog360Error("360dialog webhook contains too many entries.", 413)
        values = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list) or len(entry["changes"]) > 20:
                raise Dialog360Error("Invalid 360dialog webhook changes.", 400)
            for change in entry["changes"]:
                value = change.get("value") if isinstance(change, dict) else None
                if not isinstance(value, dict):
                    raise Dialog360Error("Invalid 360dialog webhook value.", 400)
                values.append(value)
    elif "messages" in payload or "statuses" in payload:
        # Older 360dialog sandbox callbacks omit Meta's entry/changes wrapper.
        values = [payload]
    else:
        raise Dialog360Error("Invalid 360dialog webhook envelope.", 400)
    for value in values:
        messages = value.get("messages", [])
        if not isinstance(messages, list) or len(messages) > 20:
            raise Dialog360Error("Invalid 360dialog webhook messages.", 400)
        for message in messages:
            normalized.append(_normalize_message(message))
    return normalized


def _normalize_message(message):
    if not isinstance(message, dict):
        raise Dialog360Error("Invalid 360dialog message.", 400)
    phone, bsuid = message.get("from"), message.get("from_user_id")
    if phone:
        # Some sandbox callbacks include both identifiers even though the
        # documentation presents them as alternatives. Prefer the shared phone
        # number because this key can reply through the API's `to` field.
        recipient_field, sender = "to", str(phone).removeprefix("+")
        if not _PHONE.fullmatch(sender):
            raise Dialog360Error("Invalid 360dialog phone-number sender.", 400)
    elif bsuid:
        recipient_field, sender = "recipient", str(bsuid)
        if not _BSUID.fullmatch(sender):
            raise Dialog360Error("Invalid 360dialog business-scoped sender.", 400)
    else:
        raise Dialog360Error("360dialog message does not identify its sender.", 400)

    message_id = str(message.get("id", ""))
    if not _MESSAGE_ID.fullmatch(message_id):
        raise Dialog360Error("Invalid 360dialog message ID.", 400)
    message_type = message.get("type")
    if message_type == "text":
        text = message.get("text", {}).get("body") if isinstance(message.get("text"), dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
            raise Dialog360Error("Invalid 360dialog text message.", 400)
        text, num_media = text.strip(), 0
    else:
        text, num_media = "", 1
    return {
        "sender": sender,
        "recipient_field": recipient_field,
        "text": text,
        "event_id": message_id,
        "num_media": num_media,
    }


async def send_sandbox_text(api_key, recipient_field, recipient, body, *, client=None):
    if not api_key or not api_key.strip():
        raise Dialog360Error("360dialog sandbox is not configured.", 503)
    if recipient_field not in ("to", "recipient"):
        raise Dialog360Error("Invalid 360dialog recipient type.", 500)
    payload = {
        "messaging_product": "whatsapp",
        recipient_field: recipient,
        "type": "text",
        "text": {"body": body},
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=3.5, follow_redirects=False)
    try:
        response = await client.post(
            SANDBOX_MESSAGES_URL,
            headers={"Content-Type": "application/json", "D360-API-KEY": api_key},
            json=payload,
        )
    except httpx.HTTPError:
        raise Dialog360Error("360dialog sandbox could not be reached.")
    finally:
        if owns_client:
            await client.aclose()
    # The sandbox documentation shows 201, while the live sandbox currently
    # returns 200 for accepted messages. Both are successful create outcomes.
    if response.status_code not in (200, 201):
        if response.status_code == 429:
            raise Dialog360Error("The 360dialog sandbox 200-message limit has been reached.", 429)
        raise Dialog360Error(f"360dialog sandbox rejected the reply (HTTP {response.status_code}).")
    try:
        message_id = response.json()["messages"][0]["id"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise Dialog360Error("360dialog sandbox returned an invalid success response.")
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
        raise Dialog360Error("360dialog sandbox returned an invalid message ID.")
    return message_id
