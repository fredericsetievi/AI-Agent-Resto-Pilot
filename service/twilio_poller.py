"""Poll Twilio's Messages API when Try out WhatsApp exposes no webhook field.

This is a trial/demo fallback, not a production transport. It intentionally does
not send an outbound WhatsApp reply: the local simulator remains the two-way
conversation surface until a supported sender is available.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import signal
import threading
from dataclasses import dataclass
from pathlib import Path

from .db import Store
from .whatsapp import LIVE, TRIAL_POLL_REPLY_STATUS, twilio_event_key


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WHATSAPP_ADDRESS = re.compile(r"whatsapp:\+[1-9]\d{6,14}")
MESSAGE_SID = re.compile(r"[A-Za-z0-9_-]{1,100}")
ACCOUNT_SID = re.compile(r"AC[0-9a-fA-F]{32}")
MIN_INTERVAL_SECONDS = 2.0
MAX_INTERVAL_SECONDS = 300.0
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 100

logger = logging.getLogger("restopilot.twilio_poller")


@dataclass(frozen=True)
class PollResult:
    scanned: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0


def environment_config(environ=None):
    """Read polling credentials from the process environment only."""
    environ = os.environ if environ is None else environ
    account_sid = environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = environ.get("TWILIO_AUTH_TOKEN", "").strip()
    recipient = environ.get("TWILIO_WHATSAPP_TO", "").strip() or None
    if not ACCOUNT_SID.fullmatch(account_sid):
        raise ValueError("TWILIO_ACCOUNT_SID must be a valid Account SID.")
    if not auth_token:
        raise ValueError("TWILIO_AUTH_TOKEN must be set in the process environment.")
    if recipient and not WHATSAPP_ADDRESS.fullmatch(recipient):
        raise ValueError("TWILIO_WHATSAPP_TO must use the form whatsapp:+<E.164 number>.")
    return account_sid, auth_token, recipient


def make_twilio_client(account_sid, auth_token):
    from twilio.rest import Client

    return Client(account_sid, auth_token)


def _bounded_number(value, minimum, maximum, name):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


class TwilioTrialPoller:
    def __init__(self, store, client, *, recipient=None, interval=5.0, batch_size=50):
        if recipient and not WHATSAPP_ADDRESS.fullmatch(recipient):
            raise ValueError("recipient must use the form whatsapp:+<E.164 number>.")
        self.store = store
        self.client = client
        self.recipient = recipient
        self.interval = _bounded_number(float(interval), MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS, "interval")
        self.batch_size = _bounded_number(int(batch_size), MIN_BATCH_SIZE, MAX_BATCH_SIZE, "batch size")

    @staticmethod
    def _message_payload(message):
        direction = str(getattr(message, "direction", "") or "").casefold()
        sender = str(getattr(message, "from_", "") or "").strip()
        recipient = str(getattr(message, "to", "") or "").strip()
        sid = str(getattr(message, "sid", "") or "").strip()
        body = str(getattr(message, "body", "") or "").strip()
        media_text = str(getattr(message, "num_media", "0") or "0").strip()
        if direction != "inbound" or not WHATSAPP_ADDRESS.fullmatch(sender) or not WHATSAPP_ADDRESS.fullmatch(recipient):
            return None
        if not MESSAGE_SID.fullmatch(sid) or len(body) > 10_000 or not re.fullmatch(r"\d{1,2}", media_text):
            return None
        num_media = int(media_text)
        if num_media > 10 or (not body and not num_media):
            return None
        return sender, recipient, sid, body, num_media

    def poll_once(self):
        # Twilio returns the most recent records first. Reverse them so a purchase
        # message is ingested before a later YA in the same polling window.
        filters = {"limit": self.batch_size}
        if self.recipient:
            filters["to"] = self.recipient
        messages = list(self.client.messages.list(**filters))
        processed = skipped = failed = 0
        for message in reversed(messages):
            payload = self._message_payload(message)
            if payload is None:
                skipped += 1
                continue
            sender, recipient, sid, body, num_media = payload
            if self.recipient and recipient != self.recipient:
                skipped += 1
                continue
            channel = LIVE + ":" + recipient
            key = twilio_event_key(recipient, sid)
            try:
                self.store.whatsapp_ingest(
                    key,
                    {"sender": sender, "text": body, "event_id": sid, "num_media": num_media},
                    transport=LIVE,
                    channel=channel,
                    outbound_status=TRIAL_POLL_REPLY_STATUS,
                )
                processed += 1
            except Exception as exc:  # Continue past a single malformed/conflicting provider record.
                failed += 1
                logger.error("Twilio message could not be processed (%s).", type(exc).__name__)
        return PollResult(scanned=len(messages), processed=processed, skipped=skipped, failed=failed)

    def run_forever(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            try:
                result = self.poll_once()
                logger.info(
                    "Twilio poll complete: scanned=%d processed=%d skipped=%d failed=%d",
                    result.scanned,
                    result.processed,
                    result.skipped,
                    result.failed,
                )
            except Exception as exc:
                # Do not interpolate the provider exception: it may contain request
                # context. The exception class is enough for a safe demo diagnostic.
                logger.error("Twilio poll request failed (%s); retrying.", type(exc).__name__)
            stop_event.wait(self.interval)


def main():
    parser = argparse.ArgumentParser(description="RestoPilot Twilio Trial inbound polling fallback")
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "restopilot.sqlite3")
    parser.add_argument("--interval", type=float, default=5.0, help="Poll interval, 2-300 seconds")
    parser.add_argument("--batch-size", type=int, default=50, help="Recent messages per poll, 1-100")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--empty", action="store_true", help="Skip seed data when creating a new database")
    args = parser.parse_args()

    try:
        account_sid, auth_token, recipient = environment_config()
        store = Store(args.db, seed=not args.empty)
        poller = TwilioTrialPoller(
            store,
            make_twilio_client(account_sid, auth_token),
            recipient=recipient,
            interval=args.interval,
            batch_size=args.batch_size,
        )
    except ValueError as exc:
        parser.error(str(exc))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        result = poller.poll_once()
        logger.info(
            "Twilio poll complete: scanned=%d processed=%d skipped=%d failed=%d",
            result.scanned,
            result.processed,
            result.skipped,
            result.failed,
        )
        return

    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logger.info("RestoPilot Twilio Trial poller started; press Ctrl-C to stop.")
    poller.run_forever(stop_event)


if __name__ == "__main__":
    main()
