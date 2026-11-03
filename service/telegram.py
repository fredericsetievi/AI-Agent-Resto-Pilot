"""Telegram Bot API transport: the second messaging channel next to Meta WhatsApp.

The conversation brain stays in Store.whatsapp_ingest — this module only
normalizes Telegram updates into the same message shape the Meta webhook
produces, sends the resulting text reply, and downloads media. Telegram
authenticates its webhooks with a shared secret token header instead of a
signature, and replays updates until they get a 2xx, so the same idempotent
ingest + claim-then-send machinery that keeps Meta replies exactly-once is
reused here.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger("restopilot.telegram")

TELEGRAM_WEBHOOK_PATH = "/webhooks/telegram"
TRANSPORT = "telegram_bot"
OUTBOUND_PENDING = "telegram_send_pending"
OUTBOUND_SENDING = "telegram_sending"
OUTBOUND_SENT = "telegram_sent"
DEFAULT_API_BASE = "https://api.telegram.org"
MAX_MEDIA_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 4000  # Telegram rejects messages over 4096 characters.

_TOKEN = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{25,64}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_MEDIA_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class TelegramError(Exception):
    """Safe provider error that never includes the bot token or response bodies."""

    def __init__(self, message, status=502):
        self.status = status
        super().__init__(message)


def validate_bot_token(value):
    value = (value or "").strip()
    if not _TOKEN.fullmatch(value):
        raise ValueError("TELEGRAM_BOT_TOKEN must look like '123456789:AA...your BotFather token'.")
    return value


def validate_secret_token(value):
    value = (value or "").strip()
    if not value or not _SECRET.fullmatch(value):
        raise ValueError("TELEGRAM_WEBHOOK_SECRET uses 1-256 letters, digits, underscores, or dashes.")
    return value


def bot_id_from_token(token):
    bot_id = token.split(":", 1)[0]
    if not bot_id.isdigit():
        raise TelegramError("The configured Telegram bot token is malformed.", 500)
    return bot_id


def matches_secret(candidate, configured):
    return bool(candidate and configured) and hmac.compare_digest(candidate, configured)


def parse_update(raw):
    """Return normalized text/media messages from one Telegram webhook update.

    Only new private/group messages are handled; edits, callbacks and channel
    posts return []. The shape matches the Meta normalizer: sender is the chat
    id string, event_id is the update id Telegram uses for retry dedupe.
    """
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise TelegramError("Malformed Telegram webhook JSON.", 400)
    if not isinstance(payload, dict) or "update_id" not in payload:
        raise TelegramError("Invalid Telegram webhook envelope.", 400)
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    chat = message.get("chat")
    sender = str(chat.get("id")) if isinstance(chat, dict) and isinstance(chat.get("id"), int) else None
    if not sender or len(sender) > 200:
        raise TelegramError("Telegram message carries an unusable chat id.", 400)
    event_id = str(payload["update_id"])
    if not event_id.isdigit() or len(event_id) > 20:
        raise TelegramError("Telegram message carries an unusable update id.", 400)

    text, num_media, media_id, media_kind = message.get("text", ""), 0, None, None
    if isinstance(text, str):
        if not text.strip() or len(text) > 10_000:
            text = ""
    else:
        text = ""
    photo = message.get("photo")
    if isinstance(photo, list) and photo and all(isinstance(item, dict) for item in photo):
        largest = max(photo, key=lambda item: item.get("file_size") or 0)
        media_id = largest.get("file_id")
        text, num_media, media_kind = "", 1, "image"
    elif isinstance(message.get("voice"), dict):
        media_id = message["voice"].get("file_id")
        text, num_media, media_kind = "", 1, "audio"
    elif isinstance(message.get("audio"), dict):
        media_id = message["audio"].get("file_id")
        text, num_media, media_kind = "", 1, "audio"
    elif isinstance(message.get("document"), dict) and message["document"].get("mime_type") == "application/pdf":
        media_id = message["document"].get("file_id")
        text, num_media, media_kind = "", 1, "pdf"
    elif not text:
        text, num_media = "", 1
    if media_id is not None and (not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id)):
        media_id, media_kind, num_media = None, None, 1
    group_id = message.get("media_group_id")
    if isinstance(group_id, bool) or group_id in (None, ""):
        group_id = None
    else:
        group_id = str(group_id)
        if not group_id.strip() or len(group_id) > 64:
            group_id = None
    return [{"sender": sender, "text": text, "event_id": event_id, "num_media": num_media, "media_id": media_id,
             "media_kind": media_kind, "media_group_id": group_id}]


async def send_text(token, chat_id, text, *, api_base=DEFAULT_API_BASE, client=None):
    """Send one text reply; Telegram caps a message at 4096 characters."""
    if not token or not token.strip():
        raise TelegramError("Telegram Bot API is not configured.", 503)
    body = text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS - 1] + "…"
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    try:
        response = await client.post(
            api_base + f"/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": body},
        )
    except httpx.HTTPError:
        raise TelegramError("Telegram Bot API could not be reached.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise TelegramError(f"Telegram rejected the reply (HTTP {response.status_code}).", status)
    try:
        ok = response.json()["ok"]
    except (ValueError, TypeError):
        raise TelegramError("Telegram returned an invalid success response.")
    if ok is not True:
        raise TelegramError("Telegram reported the reply as failed.")


async def download_file(token, file_id, *, api_base=DEFAULT_API_BASE, client=None, max_bytes=None):
    """Resolve one file_id through getFile and return (bytes, None).

    Telegram serves media in two hops: the getFile metadata carries a
    relative file_path that is then fetched from the file endpoint. The
    Telegram bot API caps downloads at 20 MiB; this app keeps Meta's 5 MiB
    budget so one cap covers both channels.
    """
    if not token or not token.strip():
        raise TelegramError("Telegram Bot API is not configured.", 503)
    if not isinstance(file_id, str) or not _MEDIA_ID.fullmatch(file_id):
        raise TelegramError("Invalid Telegram media id.", 400)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        try:
            meta = await client.get(api_base + f"/bot{token}/getFile", params={"file_id": file_id})
        except httpx.HTTPError:
            raise TelegramError("Telegram media lookup could not be reached.")
        if meta.status_code != 200:
            raise TelegramError(f"Telegram rejected the media lookup (HTTP {meta.status_code}).")
        try:
            file_path = meta.json()["result"]["file_path"]
        except (ValueError, KeyError, TypeError):
            raise TelegramError("Telegram returned an unusable media response.")
        if not isinstance(file_path, str) or not file_path:
            raise TelegramError("Telegram media has no file path.")
        parsed = urlsplit(api_base)
        file_url = f"{parsed.scheme}://{parsed.netloc}/file/bot{token}/{file_path}"
        try:
            download = await client.get(file_url)
        except httpx.HTTPError:
            raise TelegramError("Telegram media download failed.")
        if download.status_code != 200:
            raise TelegramError(f"Telegram media download failed (HTTP {download.status_code}).")
        raw = download.content
        cap = max_bytes or MAX_MEDIA_BYTES
        if not raw or len(raw) > cap:
            raise TelegramError(f"Telegram media is empty or exceeds {cap // (1024 * 1024)} MiB.")
        return raw, None
    finally:
        if owns_client:
            await client.aclose()
