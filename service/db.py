"""SQLite authority for the local demo. No remote providers or payment writes.

Every mutation uses a fresh connection and BEGIN IMMEDIATE. Purchase posting,
inventory movements, audit activity, and the idempotent response commit together.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

from .extraction import extract_purchase, normalize_item
from .whatsapp import SIMULATOR, draft_reply


CATALOG = [
    {"id": "cabai", "name": "Cabai merah", "unit": "kg", "reorder_threshold": "5"},
    {"id": "telur", "name": "Telur ayam", "unit": "kg", "reorder_threshold": "5"},
    {"id": "beras", "name": "Beras", "unit": "kg", "reorder_threshold": "20"},
    {"id": "minyak", "name": "Minyak goreng", "unit": "l", "reorder_threshold": "5"},
    {"id": "bawang", "name": "Bawang merah", "unit": "kg", "reorder_threshold": "3"},
]
CATALOG_BY_ID = {item["id"]: item for item in CATALOG}
RESTAURANT = {"id": "demo-resto", "name": "Warung Nusantara"}
RESTAURANT_TIMEZONE = ZoneInfo("Asia/Jakarta")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def decimal_string(value):
    value = Decimal(value)
    return format(value.normalize(), "f") if value else "0"


def encode(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def draft_total(items, warnings):
    """Do not present a partial or unresolved invoice as a known total."""
    if warnings or not items:
        return None
    amounts = [item.get("line_total_idr") for item in items]
    if any(isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= 1_000_000_000_000 for amount in amounts):
        return None
    return sum(amounts)


class DomainError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class Store:
    def __init__(self, path, seed=True):
        self.path = str(path)
        if self.path == ":memory:":
            # A shared-memory URI keeps one database across per-request connections.
            self.path = f"file:restopilot-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = self.connect()
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._keeper = None
        self.initialize(seed)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None, uri=self.path.startswith("file:"))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self, seed):
        with self.transaction() as conn:
            for statement in (
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), status TEXT NOT NULL, body TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS purchases (id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id), supplier TEXT, created_at TEXT NOT NULL, body TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS inventory (ingredient_id TEXT PRIMARY KEY, quantity TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS movements (id TEXT PRIMARY KEY, purchase_id TEXT REFERENCES purchases(id), ingredient_id TEXT NOT NULL, quantity TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(purchase_id, ingredient_id))",
                "CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, event TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS whatsapp_threads (id TEXT PRIMARY KEY, channel TEXT NOT NULL, sender TEXT NOT NULL, transport TEXT NOT NULL, active_draft_id TEXT REFERENCES drafts(id), active_version INTEGER, UNIQUE(channel, sender))",
                "CREATE TABLE IF NOT EXISTS whatsapp_messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES whatsapp_threads(id), direction TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, transport TEXT NOT NULL, status TEXT NOT NULL, draft_id TEXT REFERENCES drafts(id))",
                "CREATE TABLE IF NOT EXISTS whatsapp_events (channel TEXT NOT NULL, event_id TEXT NOT NULL, thread_id TEXT NOT NULL REFERENCES whatsapp_threads(id), fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(channel, event_id))",
            ):
                conn.execute(statement)
            if not conn.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone():
                for ingredient in CATALOG:
                    conn.execute("INSERT INTO inventory VALUES (?, '0')", (ingredient["id"],))
                if seed:
                    self._seed(conn)
                conn.execute("INSERT INTO metadata VALUES ('initialized', ?)", (now(),))

    def _seed(self, conn):
        for ingredient, qty, total, age in [("cabai", "3", 129000, 3), ("telur", "10", 430000, 2)]:
            created_at = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
            draft = self._new_draft(conn, f"Synthetic history: {ingredient}", "Pasar Pagi", [
                {"ingredient_id": ingredient, "raw_name": CATALOG_BY_ID[ingredient]["name"], "quantity": qty, "unit": "kg", "line_total_idr": total}
            ], [], created_at=created_at)
            self._post(conn, draft, created_at=created_at)
        # Disclosed opening estimates; these are not receipt purchases or consumption.
        for ingredient, qty in [("beras", "12"), ("minyak", "3"), ("bawang", "1.5")]:
            conn.execute("UPDATE inventory SET quantity=? WHERE ingredient_id=?", (qty, ingredient))
            conn.execute("INSERT INTO movements VALUES (?, NULL, ?, ?, ?)", (uuid.uuid4().hex, ingredient, qty, now()))
        self._new_draft(conn, "Telur 3 kg 150rb", "Pasar Pagi", [
            {"ingredient_id": "telur", "raw_name": "Telur", "quantity": "3", "unit": "kg", "line_total_idr": 150000}
        ], [])
        self._audit(conn, "demo_seeded", "Synthetic sample history and estimated opening stock loaded.")

    def _audit(self, conn, event, message):
        conn.execute("INSERT INTO activity VALUES (?, ?, ?, ?)", (uuid.uuid4().hex, event, message, now()))

    def _normalize(self, items):
        normalized, warnings = [], []
        if not items:
            warnings.append("Add at least one purchase item.")
        for index, item in enumerate(items):
            result = dict(normalize_item(dict(item)))
            local_warnings = list(result.get("warnings", []))
            # The ledger enforces its own invariants even if an extractor changes.
            ingredient = CATALOG_BY_ID.get(result.get("ingredient_id"))
            if not ingredient:
                local_warnings.append("Select a known ingredient.")
            try:
                quantity = Decimal(str(result.get("canonical_quantity")))
                if not quantity.is_finite() or quantity <= 0 or quantity > Decimal("1000000000"):
                    raise InvalidOperation
                result["canonical_quantity"] = decimal_string(quantity)
            except (InvalidOperation, ValueError, TypeError):
                local_warnings.append("Enter a valid positive quantity.")
                result["canonical_quantity"] = None
            if ingredient and result.get("canonical_unit") != ingredient["unit"]:
                local_warnings.append(f"Use a unit convertible to {ingredient['unit']} for this ingredient.")
                result["canonical_quantity"] = None
            amount = result.get("line_total_idr")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0 or amount > 1_000_000_000_000:
                local_warnings.append("Enter a valid positive whole-rupiah line total.")
            if result.get("canonical_quantity") and isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
                result["unit_price_idr"] = decimal_string(Decimal(amount) / Decimal(result["canonical_quantity"]))
            else:
                result["unit_price_idr"] = None
            result["warnings"] = list(dict.fromkeys(local_warnings))
            warnings.extend(f"Item {index + 1}: {warning}" for warning in result["warnings"])
            normalized.append(result)
        return normalized, list(dict.fromkeys(warnings))

    def _comparison(self, conn, item, supplier):
        comparison = {"status": "insufficient_history", "change_percent": None, "baseline_unit_price_idr": None, "baseline_purchase_id": None,
                      "baseline_supplier": None, "baseline_created_at": None}
        if not item.get("unit_price_idr") or item.get("warnings") or not supplier:
            return comparison
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        rows = conn.execute("SELECT body FROM purchases WHERE supplier=? AND created_at>=? ORDER BY created_at DESC, id DESC", (supplier, cutoff))
        for row in rows:
            purchase = json.loads(row["body"])
            # Duplicate ingredient lines in an invoice form one weighted baseline.
            previous = [part for part in purchase["items"] if part["ingredient_id"] == item["ingredient_id"] and part["canonical_unit"] == item["canonical_unit"]]
            if not previous:
                continue
            # Compare integer/Decimal cross-products before division. Rounded
            # repeating unit prices must not misclassify the exact 15% boundary.
            with localcontext() as arithmetic:
                arithmetic.prec = 64
                quantity = sum((Decimal(part["canonical_quantity"]) for part in previous), Decimal(0))
                total = sum((Decimal(part["line_total_idr"]) for part in previous), Decimal(0))
                baseline = total / quantity
                numerator = Decimal(item["line_total_idr"]) * quantity - total * Decimal(item["canonical_quantity"])
                denominator = total * Decimal(item["canonical_quantity"])
                increase = numerator * 100 >= denominator * 15
                change = (numerator * 100 / denominator).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            comparison.update({
                "status": "price_increase" if increase else "comparable",
                "change_percent": format(change, "f"),
                "baseline_unit_price_idr": decimal_string(baseline),
                "baseline_purchase_id": purchase["id"],
                "baseline_supplier": purchase["supplier"],
                "baseline_created_at": purchase["created_at"],
                "basis": "same_supplier_last_30_days",
                "supplier": supplier,
            })
            break
        return comparison

    def _present(self, conn, draft):
        if draft["status"] == "recorded":
            # Preserve the comparison evidence visible when the owner approved.
            return draft
        # Also project older local-demo drafts safely after a service restart.
        draft["total_idr"] = draft_total(draft["items"], draft["warnings"])
        for item in draft["items"]:
            item["comparison"] = self._comparison(conn, item, draft["supplier"])
        draft["comparison"] = next((item["comparison"] for item in draft["items"] if item["comparison"]["status"] == "price_increase"), None)
        return draft

    def _new_draft(self, conn, text, supplier, items, warnings, created_at=None):
        items, field_warnings = self._normalize(items)
        all_warnings = list(dict.fromkeys(list(warnings) + field_warnings))
        draft = {
            "id": uuid.uuid4().hex, "version": 1,
            "status": "needs_clarification" if all_warnings else "pending_approval",
            "source_text": text, "supplier": supplier, "items": items,
            "total_idr": draft_total(items, all_warnings),
            "warnings": all_warnings, "created_at": created_at or now(),
        }
        self._save_draft(conn, draft, insert=True)
        self._audit(conn, "draft_created", f"Purchase draft created: {draft['id'][:8]}.")
        return draft

    def _save_draft(self, conn, draft, insert=False):
        if insert:
            conn.execute("INSERT INTO drafts VALUES (?, ?, ?, ?)", (draft["id"], draft["version"], draft["status"], encode(draft)))
        else:
            conn.execute("UPDATE drafts SET version=?, status=?, body=? WHERE id=?", (draft["version"], draft["status"], encode(draft), draft["id"]))

    def _load_draft(self, conn, draft_id):
        row = conn.execute("SELECT body FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "Draft not found.", 404)
        return json.loads(row["body"])

    def get_draft(self, draft_id):
        with self.transaction() as conn:
            return self._present(conn, self._load_draft(conn, draft_id))

    def _idempotent(self, key, operation, payload, action):
        fingerprint = hashlib.sha256(encode({"operation": operation, "payload": payload}).encode()).hexdigest()
        with self.transaction() as conn:
            existing = conn.execute("SELECT fingerprint, response FROM idempotency WHERE key=?", (key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise DomainError("idempotency_conflict", "This idempotency key was already used for a different request.")
                return json.loads(existing["response"])
            result = action(conn)
            conn.execute("INSERT INTO idempotency VALUES (?, ?, ?)", (key, fingerprint, encode(result)))
            return result

    def intake(self, key, payload):
        extracted = extract_purchase(payload["text"])
        def action(conn):
            return self._present(conn, self._new_draft(conn, payload["text"], payload.get("supplier"), extracted["items"], extracted["warnings"]))
        return self._idempotent(key, "intake", payload, action)

    def _check_editable(self, draft, version):
        if draft["version"] != version:
            raise DomainError("version_conflict", "The draft changed. Refresh it before continuing.")
        if draft["status"] not in ("pending_approval", "needs_clarification"):
            raise DomainError("draft_finalized", "A recorded or rejected draft cannot be changed.")

    def edit(self, draft_id, key, payload):
        def action(conn):
            draft = self._load_draft(conn, draft_id)
            self._check_editable(draft, payload["expected_version"])
            items, warnings = self._normalize(payload["items"])
            draft.update({"items": items, "supplier": payload["supplier"], "warnings": warnings,
                          "status": "needs_clarification" if warnings else "pending_approval",
                          "total_idr": draft_total(items, warnings),
                          "version": draft["version"] + 1})
            self._save_draft(conn, draft)
            self._audit(conn, "draft_edited", f"Draft {draft_id[:8]} reviewed and edited (version {draft['version']}).")
            return self._present(conn, draft)
        return self._idempotent(key, "edit:" + draft_id, payload, action)

    def _post(self, conn, draft, created_at=None):
        self._present(conn, draft)
        purchase_id = uuid.uuid4().hex
        created_at = created_at or now()
        purchase = {"id": purchase_id, "draft_id": draft["id"], "supplier": draft["supplier"],
                    "total_idr": draft["total_idr"], "items": draft["items"], "created_at": created_at,
                    "payment_status": "unknown"}
        conn.execute("INSERT INTO purchases VALUES (?, ?, ?, ?, ?)", (purchase_id, draft["id"], draft["supplier"], created_at, encode(purchase)))
        deltas = {}
        for item in draft["items"]:
            ingredient = item["ingredient_id"]
            deltas[ingredient] = deltas.get(ingredient, Decimal(0)) + Decimal(item["canonical_quantity"])
        for ingredient, quantity in deltas.items():
            stock = Decimal(conn.execute("SELECT quantity FROM inventory WHERE ingredient_id=?", (ingredient,)).fetchone()[0])
            conn.execute("UPDATE inventory SET quantity=? WHERE ingredient_id=?", (decimal_string(stock + quantity), ingredient))
            conn.execute("INSERT INTO movements VALUES (?, ?, ?, ?, ?)", (uuid.uuid4().hex, purchase_id, ingredient, decimal_string(quantity), created_at))
        draft.update({"status": "recorded", "version": draft["version"] + 1, "purchase_id": purchase_id})
        self._save_draft(conn, draft)
        self._audit(conn, "purchase_recorded", f"Purchase recorded for Rp {draft['total_idr']:,}; received stock added. Payment status unknown.")
        return draft

    def decision(self, draft_id, key, payload):
        def action(conn):
            draft = self._load_draft(conn, draft_id)
            return self._decide(conn, draft, payload)
        return self._idempotent(key, "decision:" + draft_id, payload, action)

    def _decide(self, conn, draft, payload):
        """Shared transaction-internal decision path for dashboard and WhatsApp."""
        self._check_editable(draft, payload["expected_version"])
        if payload["decision"] == "reject":
            draft.update({"status": "rejected", "version": draft["version"] + 1})
            self._save_draft(conn, draft)
            self._audit(conn, "draft_rejected", f"Draft {draft['id'][:8]} rejected; no purchase or inventory posted.")
        else:
            _, invalid = self._normalize(draft["items"])
            if draft["warnings"] or invalid:
                raise DomainError("clarification_required", "Review and correct all draft warnings before approval.", 422)
            if not payload["received"]:
                raise DomainError("receipt_required", "Confirm that the goods were received before recording stock.", 422)
            draft = self._post(conn, draft)
        return self._present(conn, draft)

    def _whatsapp_thread(self, conn, sender, channel, transport, create=False):
        row = conn.execute("SELECT * FROM whatsapp_threads WHERE channel=? AND sender=?", (channel, sender)).fetchone()
        if row:
            return dict(row)
        thread = {"id": hashlib.sha256((channel + "\0" + sender).encode()).hexdigest()[:32], "channel": channel,
                  "sender": sender, "transport": transport, "active_draft_id": None, "active_version": None}
        if create:
            conn.execute("INSERT INTO whatsapp_threads VALUES (?, ?, ?, ?, NULL, NULL)", (thread["id"], channel, sender, transport))
        return thread

    def _whatsapp_snapshot(self, conn, thread, draft=None, result=None):
        # A dashboard decision may have finalized the correlated draft.
        active = self._load_draft(conn, thread["active_draft_id"]) if thread["active_draft_id"] else None
        active_id = active["id"] if active and active["status"] in ("pending_approval", "needs_clarification") else None
        response = {"thread": {"id": thread["id"], "sender": thread["sender"], "transport": thread["transport"], "active_draft_id": active_id},
                    "messages": [dict(row) for row in reversed(conn.execute("SELECT id, direction, body, created_at, transport, status, draft_id FROM whatsapp_messages WHERE thread_id=? ORDER BY rowid DESC LIMIT 100", (thread["id"],)).fetchall())]}
        visible_draft = draft or (active if active_id else None)
        if visible_draft:
            response["draft"] = self._present(conn, visible_draft)
        if result:
            response["result"] = result
        return response

    def whatsapp_messages(self, sender="demo"):
        with self.transaction() as conn:
            return self._whatsapp_snapshot(conn, self._whatsapp_thread(conn, sender, SIMULATOR, SIMULATOR))

    def claim_whatsapp_message(self, message_id, expected_status, claimed_status):
        """Atomically claim one provider delivery so webhook retries do not resend it."""
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE whatsapp_messages SET status=? WHERE id=? AND direction='outbound' AND status=?",
                (claimed_status, message_id, expected_status),
            ).rowcount
            return changed == 1

    def set_whatsapp_message_status(self, message_id, status):
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE whatsapp_messages SET status=? WHERE id=? AND direction='outbound'",
                (status, message_id),
            ).rowcount
            if changed != 1:
                raise DomainError("whatsapp_message_not_found", "Outbound WhatsApp message was not found.", 404)

    def whatsapp_ingest(self, key, payload, *, transport=SIMULATOR, channel=None, outbound_status=None):
        """Persist inbound, decision/ledger, outbound and dedupe in one commit."""
        channel = channel or transport
        payload = {"sender": payload["sender"], "text": payload["text"], "event_id": payload.get("event_id"), "num_media": payload.get("num_media", 0)}
        event_id = payload["event_id"] or "sim:" + key
        fingerprint = hashlib.sha256(encode({"sender": payload["sender"], "text": payload["text"], "num_media": payload["num_media"]}).encode()).hexdigest()

        def action(conn):
            prior = conn.execute("SELECT fingerprint, response FROM whatsapp_events WHERE channel=? AND event_id=?", (channel, event_id)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DomainError("event_conflict", "This message event was already used for different content or sender.")
                return json.loads(prior["response"])
            thread = self._whatsapp_thread(conn, payload["sender"], channel, transport, create=True)
            draft = self._load_draft(conn, thread["active_draft_id"]) if thread["active_draft_id"] else None
            if draft and draft["status"] not in ("pending_approval", "needs_clarification"):
                draft = None
                thread.update(active_draft_id=None, active_version=None)
            command = payload["text"].strip().casefold()
            result = {}
            if payload["num_media"]:
                reply = "Foto/audio belum diproses. Kirim rincian pembelian sebagai teks atau gunakan dashboard. Tidak ada pembelian yang dicatat."
                result["action"] = "media_unsupported"
            elif command in ("ya", "batal"):
                if not draft:
                    reply = "Tidak ada draf aktif untuk nomor ini. Kirim rincian pembelian terlebih dahulu; YA tidak mencatat pembelian lain."
                    result["action"] = "no_active_draft"
                elif command == "batal":
                    draft = self._decide(conn, draft, {"expected_version": draft["version"], "decision": "reject", "received": False})
                    thread.update(active_draft_id=None, active_version=None)
                    reply = f"Draf {draft['id'][:8]} dibatalkan. Pembelian dan stok tidak berubah."
                    result["action"] = "rejected"
                elif draft["status"] == "needs_clarification":
                    reply = draft_reply(draft)
                    result["action"] = "review_required"
                elif draft["version"] != thread["active_version"]:
                    # Dashboard edits invalidate the version previously summarized.
                    thread["active_version"] = draft["version"]
                    reply = "Draf berubah sejak pesan sebelumnya. Tinjau ulang rincian berikut dan balas YA lagi jika benar.\n" + draft_reply(draft)
                    result["action"] = "review_required"
                else:
                    draft = self._decide(conn, draft, {"expected_version": thread["active_version"], "decision": "approve", "received": True})
                    thread.update(active_draft_id=None, active_version=None)
                    reply = f"Pembelian {draft['id'][:8]} tercatat. Barang diterima ditambahkan ke estimasi stok. Status pembayaran tidak diketahui."
                    result.update(action="approved", purchase_id=draft["purchase_id"])
            elif draft:
                reply = "Masih ada draf aktif; pesan baru ini tidak menggantikannya. Tinjau di dashboard, balas YA jika barang diterima, atau BATAL.\n" + draft_reply(draft)
                result["action"] = "active_draft_exists"
            else:
                extracted = extract_purchase(payload["text"])
                draft = self._new_draft(conn, payload["text"], None, extracted["items"], extracted["warnings"])
                thread.update(active_draft_id=draft["id"], active_version=draft["version"])
                reply = draft_reply(draft)
                result["action"] = "draft_created" if draft["status"] == "pending_approval" else "review_required"
            conn.execute("UPDATE whatsapp_threads SET active_draft_id=?, active_version=? WHERE id=?", (thread["active_draft_id"], thread["active_version"], thread["id"]))
            draft_id = draft["id"] if draft else None
            inbound_body = payload["text"] if not payload["num_media"] else f"[Media omitted: {payload['num_media']} attachment(s)] " + payload["text"]
            reply_status = outbound_status or ("simulated" if transport == SIMULATOR else "twiml_prepared")
            for direction, body, status in (("inbound", inbound_body, "received"), ("outbound", reply, reply_status)):
                conn.execute("INSERT INTO whatsapp_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (uuid.uuid4().hex, thread["id"], direction, body, now(), transport, status, draft_id))
            self._audit(conn, "whatsapp_message_processed", f"{transport} message processed: {result['action']}.")
            response = self._whatsapp_snapshot(conn, thread, draft=draft, result=result)
            conn.execute("INSERT INTO whatsapp_events VALUES (?, ?, ?, ?, ?)", (channel, event_id, thread["id"], fingerprint, encode(response)))
            return response

        return self._idempotent(key, "whatsapp:" + channel, payload, action)

    def overview(self):
        with self.transaction() as conn:
            drafts = [self._present(conn, json.loads(row["body"])) for row in conn.execute("SELECT body FROM drafts ORDER BY rowid DESC")]
            purchases = [json.loads(row["body"]) for row in conn.execute("SELECT body FROM purchases ORDER BY created_at DESC, id DESC")]
            inventory = []
            for ingredient in CATALOG:
                quantity = conn.execute("SELECT quantity FROM inventory WHERE ingredient_id=?", (ingredient["id"],)).fetchone()[0]
                inventory.append({"ingredient_id": ingredient["id"], "name": ingredient["name"], "quantity": quantity,
                                  "unit": ingredient["unit"], "reorder_threshold": ingredient["reorder_threshold"],
                                  "low_stock": Decimal(quantity) < Decimal(ingredient["reorder_threshold"])})
            today = datetime.now(RESTAURANT_TIMEZONE).date()
            return {
                "restaurant": RESTAURANT,
                "mode": {"transport": "manual", "extraction": "rules", "storage": "sqlite", "identity": "demo"},
                "metrics": {"purchases_today_idr": sum(row["total_idr"] for row in purchases if datetime.fromisoformat(row["created_at"]).astimezone(RESTAURANT_TIMEZONE).date() == today),
                            "pending_approvals": sum(row["status"] in ("pending_approval", "needs_clarification") for row in drafts),
                            "low_stock_count": sum(row["low_stock"] for row in inventory), "recorded_purchases": len(purchases)},
                "drafts": drafts, "purchases": purchases, "inventory": inventory,
                "activity": [dict(row) for row in conn.execute("SELECT * FROM activity ORDER BY created_at DESC, id DESC LIMIT 100")],
                "ingredients": CATALOG,
            }
