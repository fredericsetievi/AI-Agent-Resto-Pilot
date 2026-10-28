"""SQLite authority for the local demo. No remote providers or payment writes.

Every mutation uses a fresh connection and BEGIN IMMEDIATE. Purchase posting,
inventory movements, audit activity, and the idempotent response commit together.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

from .inventory import InventoryMixin, SQLITE_SCHEMA
from .extraction import catalog_aliases, extract_purchase, normalize_item
from .onboarding import restaurant_selection
from .whatsapp import FOLLOWUP_ACK, HELP_REPLY, LIVE, PAYMENT_FOLLOWUP_PROMPT, SIMULATOR, UNREGISTERED_REPLY, draft_reply, is_stock_question, parse_payment_followup, parse_remove_item, stock_reply, weekly_summary_reply


CATALOG = [
    {"id": "cabai", "name": "Cabai merah", "unit": "kg", "reorder_threshold": "5"},
    {"id": "telur", "name": "Telur ayam", "unit": "kg", "reorder_threshold": "5"},
    {"id": "beras", "name": "Beras", "unit": "kg", "reorder_threshold": "20"},
    {"id": "minyak", "name": "Minyak goreng", "unit": "l", "reorder_threshold": "5"},
    {"id": "bawang", "name": "Bawang merah", "unit": "kg", "reorder_threshold": "3"},
    {"id": "bawang_putih", "name": "Bawang putih", "unit": "kg", "reorder_threshold": "2"},
    {"id": "kecombrang", "name": "Kecombrang", "unit": "kg", "reorder_threshold": "1"},
    {"id": "gula_merah", "name": "Gula merah", "unit": "kg", "reorder_threshold": "5"},
    {"id": "daun_pisang", "name": "Daun pisang", "unit": "kg", "reorder_threshold": "2"},
]
CATALOG_BY_ID = {item["id"]: item for item in CATALOG}
RESTAURANT = {"id": "demo-resto", "name": "Warung Bu Sari"}
RESTAURANT_TIMEZONE = ZoneInfo("Asia/Jakarta")
SUMMARY_COMMANDS = ("ringkas", "ringkasan", "summary")
# Casual confirmations owners actually type; anything ambiguous falls through
# to normal message handling instead of guessing.
APPROVE_COMMANDS = frozenset({"ya", "y", "ya benar", "ok", "oke", "betul", "setuju", "sip", "approve"})
REJECT_COMMANDS = frozenset({"batal", "cancel", "tidak", "jangan"})
SUMMARY_WINDOW_DAYS = 7
MAX_TRANSCRIPT_CHARS = 10_000
SCHEMA_VERSION = "2"
PBKDF2_ITERATIONS = 200_000
SESSION_TTL = timedelta(days=7)
# Seeded demo logins: documented in the README and printed at startup. Demo
# credentials only — a production deployment would provision real users.
DEMO_USERS = (
    {"username": "admin", "password": "admin123", "role": "admin", "restaurant_id": None, "display_name": "Platform admin"},
    {"username": "owner", "password": "owner123", "role": "owner", "restaurant_id": RESTAURANT["id"], "display_name": "Bu Sari"},
)
DEMO_NUMBERS = ({"phone": "628123456789", "label": "Bu Sari"},)
_PHONE = re.compile(r"[1-9]\d{6,14}")
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,49}")


def hash_password(password):
    """Hash a password with PBKDF2-HMAC-SHA256 and a per-user random salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    """Constant-time password check against a stored pbkdf2_sha256 record."""
    try:
        scheme, iterations, salt_hex, digest_hex = str(stored).split("$")
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def validate_phone_number(phone):
    """WhatsApp numbers are E.164 digits without '+'; one number, one restaurant."""
    phone = str(phone or "").strip()
    if not _PHONE.fullmatch(phone):
        raise DomainError("invalid_phone_number", "WhatsApp numbers use digits only, country code first (e.g. 628123456789).", 422)
    return phone


def validate_username(username):
    username = str(username or "").strip()
    if not _USERNAME.fullmatch(username):
        raise DomainError("invalid_username", "Usernames use 3-50 letters, digits, dots, dashes, or underscores.", 422)
    return username


def validate_password(password):
    password = str(password or "")
    if not 8 <= len(password) <= 200:
        raise DomainError("invalid_password", "Passwords use 8-200 characters.", 422)
    return password


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


def normalize_items_for_catalog(items, catalog):
    """Shared item normalization used by both the SQLite and Postgres stores."""
    normalized, warnings = [], []
    if not items:
        warnings.append("Add at least one purchase item.")
    for index, item in enumerate(items):
        result = dict(normalize_item(dict(item)))
        # The kitchen filter's flag rides through normalization and a
        # dashboard edit that echoes it back.
        if item.get("non_kitchen"):
            result["non_kitchen"] = True
        local_warnings = list(result.get("warnings", []))
        # The ledger enforces its own invariants even if an extractor changes.
        # An unmapped item that kept its raw name self-heals: the ingredient
        # is auto-created from that name when the draft is approved.
        ingredient = catalog["by_id"].get(result.get("ingredient_id"))
        if not ingredient:
            if not str(result.get("raw_name") or "").strip():
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


class Store(InventoryMixin):
    def __init__(self, path, seed=True):
        self.path = str(path)
        # Each restaurant owns a catalog: the shared base plus the ingredients
        # auto-created from its own approved drafts. Per-tenant copies keep one
        # restaurant's custom bahan from matching another's messages.
        self._catalogs = {}
        if self.path == ":memory:":
            # A shared-memory URI keeps one database across per-request connections.
            self.path = f"file:restopilot-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = self.connect()
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._keeper = None
        self.initialize(seed)
        self._load_custom_ingredients()

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

    def _catalog_for(self, restaurant_id):
        entry = self._catalogs.get(restaurant_id)
        if entry is None:
            entry = {"list": [dict(item) for item in CATALOG], "by_id": dict(CATALOG_BY_ID)}
            self._catalogs[restaurant_id] = entry
        return entry

    def catalog_names(self, restaurant_id):
        """Display names for one restaurant's catalog, for WhatsApp replies."""
        return {item["id"]: item["name"] for item in self._catalog_for(restaurant_id)["list"]}

    def catalog_list(self, restaurant_id):
        """The restaurant's full catalog (base + custom), for filter checks."""
        return self._catalog_for(restaurant_id)["list"]

    @staticmethod
    def slugify_name(raw_name):
        return Store._slugify(raw_name)

    def initialize(self, seed):
        with self.transaction() as conn:
            for statement in (
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS restaurants (id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT COLLATE NOCASE NOT NULL UNIQUE, password_hash TEXT NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','owner')), restaurant_id TEXT REFERENCES restaurants(id), created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, expires_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS whatsapp_links (id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL REFERENCES restaurants(id), phone TEXT NOT NULL UNIQUE, label TEXT, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), status TEXT NOT NULL, body TEXT NOT NULL, restaurant_id TEXT NOT NULL DEFAULT 'demo-resto')",
                "CREATE TABLE IF NOT EXISTS purchases (id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id), supplier TEXT, created_at TEXT NOT NULL, body TEXT NOT NULL, restaurant_id TEXT NOT NULL DEFAULT 'demo-resto')",
                "CREATE TABLE IF NOT EXISTS inventory (restaurant_id TEXT NOT NULL DEFAULT 'demo-resto', ingredient_id TEXT NOT NULL, quantity TEXT NOT NULL, PRIMARY KEY(restaurant_id, ingredient_id))",
                "CREATE TABLE IF NOT EXISTS movements (id TEXT PRIMARY KEY, purchase_id TEXT REFERENCES purchases(id), ingredient_id TEXT NOT NULL, quantity TEXT NOT NULL, created_at TEXT NOT NULL, restaurant_id TEXT NOT NULL DEFAULT 'demo-resto', UNIQUE(purchase_id, ingredient_id))",
                "CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, event TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, restaurant_id TEXT)",
                "CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS whatsapp_threads (id TEXT PRIMARY KEY, channel TEXT NOT NULL, sender TEXT NOT NULL, transport TEXT NOT NULL, active_draft_id TEXT REFERENCES drafts(id), active_version INTEGER, pending_followup TEXT, restaurant_id TEXT REFERENCES restaurants(id), UNIQUE(channel, sender))",
                "CREATE TABLE IF NOT EXISTS whatsapp_messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES whatsapp_threads(id), direction TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, transport TEXT NOT NULL, status TEXT NOT NULL, draft_id TEXT REFERENCES drafts(id))",
                "CREATE TABLE IF NOT EXISTS whatsapp_events (channel TEXT NOT NULL, event_id TEXT NOT NULL, thread_id TEXT NOT NULL REFERENCES whatsapp_threads(id), fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(channel, event_id))",
                "CREATE TABLE IF NOT EXISTS custom_ingredients (id TEXT PRIMARY KEY, name TEXT NOT NULL, unit TEXT NOT NULL, reorder_threshold TEXT NOT NULL, created_at TEXT NOT NULL, restaurant_id TEXT NOT NULL DEFAULT 'demo-resto')",
            ):
                conn.execute(statement)
            for statement in SQLITE_SCHEMA:
                conn.execute(statement)
            movement_columns = {row[1] for row in conn.execute("PRAGMA table_info(movements)")}
            if "inventory_event_id" not in movement_columns:
                conn.execute("ALTER TABLE movements ADD COLUMN inventory_event_id TEXT")
            # Databases created before the follow-up state existed keep their
            # whatsapp_threads table; add the column in place.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(whatsapp_threads)")}
            if "pending_followup" not in columns:
                conn.execute("ALTER TABLE whatsapp_threads ADD COLUMN pending_followup TEXT")
            self._migrate_tenants(conn)
            self._ensure_demo_tenant(conn)
            if not conn.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone():
                for ingredient in CATALOG:
                    conn.execute("INSERT INTO inventory (restaurant_id, ingredient_id, quantity) VALUES (?, ?, '0')", (RESTAURANT["id"], ingredient["id"]))
                if seed:
                    self._seed(conn)
                conn.execute("INSERT INTO metadata VALUES ('initialized', ?)", (now(),))
            conn.execute("INSERT INTO metadata VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SCHEMA_VERSION,))

    def _migrate_tenants(self, conn):
        """v1 databases predate restaurants: stamp legacy rows into the demo tenant.

        ALTER TABLE cannot add a foreign key, so migrated tables carry the
        column without one; tenant integrity is enforced in Store queries.
        """
        for table in ("drafts", "purchases", "movements", "custom_ingredients"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "restaurant_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN restaurant_id TEXT NOT NULL DEFAULT '{RESTAURANT['id']}'")
        for table in ("activity", "whatsapp_threads"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "restaurant_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN restaurant_id TEXT")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(whatsapp_threads)")}
        if "restaurant_id" in columns:
            conn.execute("UPDATE whatsapp_threads SET restaurant_id=? WHERE restaurant_id IS NULL", (RESTAURANT["id"],))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(inventory)")}
        if "restaurant_id" not in columns:
            # SQLite cannot change a primary key in place: rebuild the table
            # with (restaurant_id, ingredient_id) and copy the legacy rows.
            conn.execute("ALTER TABLE inventory RENAME TO inventory_legacy")
            conn.execute("CREATE TABLE inventory (restaurant_id TEXT NOT NULL, ingredient_id TEXT NOT NULL, quantity TEXT NOT NULL, PRIMARY KEY(restaurant_id, ingredient_id))")
            conn.execute("INSERT INTO inventory (restaurant_id, ingredient_id, quantity) SELECT ?, ingredient_id, quantity FROM inventory_legacy", (RESTAURANT["id"],))
            conn.execute("DROP TABLE inventory_legacy")

    def _ensure_demo_tenant(self, conn):
        """Idempotently seed the demo restaurant, its logins, and WhatsApp number."""
        conn.execute(
            "INSERT OR IGNORE INTO restaurants (id, name, address, created_at) VALUES (?, ?, NULL, ?)",
            (RESTAURANT["id"], RESTAURANT["name"], now()),
        )
        for user in DEMO_USERS:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, password_hash, display_name, role, restaurant_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, user["username"], hash_password(user["password"]), user["display_name"], user["role"], user["restaurant_id"], now()),
            )
        for number in DEMO_NUMBERS:
            conn.execute(
                "INSERT OR IGNORE INTO whatsapp_links (id, restaurant_id, phone, label, created_at) VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, RESTAURANT["id"], number["phone"], number["label"], now()),
            )

    def _load_custom_ingredients(self):
        """Reload chat-created ingredients so each restaurant's catalog survives a restart."""
        with self.transaction() as conn:
            for row in conn.execute("SELECT restaurant_id, id, name, unit, reorder_threshold FROM custom_ingredients ORDER BY rowid"):
                entry = {"id": row["id"], "name": row["name"], "unit": row["unit"], "reorder_threshold": row["reorder_threshold"]}
                catalog = self._catalog_for(row["restaurant_id"] or RESTAURANT["id"])
                catalog["list"].append(entry)
                catalog["by_id"][entry["id"]] = entry

    def _seed(self, conn):
        for ingredient, qty, total, age in [("cabai", "3", 129000, 3), ("telur", "10", 430000, 2)]:
            created_at = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
            draft = self._new_draft(conn, f"Synthetic history: {ingredient}", "Pasar Pagi", [
                {"ingredient_id": ingredient, "raw_name": CATALOG_BY_ID[ingredient]["name"], "quantity": qty, "unit": "kg", "line_total_idr": total}
            ], [], created_at=created_at, restaurant_id=RESTAURANT["id"])
            self._post(conn, draft, created_at=created_at, restaurant_id=RESTAURANT["id"])
        # Disclosed opening estimates; these are not receipt purchases or consumption.
        for ingredient, qty in [("beras", "12"), ("minyak", "3"), ("bawang", "1.5")]:
            conn.execute("UPDATE inventory SET quantity=? WHERE restaurant_id=? AND ingredient_id=?", (qty, RESTAURANT["id"], ingredient))
            conn.execute("INSERT INTO movements (id, purchase_id, ingredient_id, quantity, created_at, restaurant_id) VALUES (?, NULL, ?, ?, ?, ?)", (uuid.uuid4().hex, ingredient, qty, now(), RESTAURANT["id"]))
        self._new_draft(conn, "Telur 3 kg 150rb", "Pasar Pagi", [
            {"ingredient_id": "telur", "raw_name": "Telur", "quantity": "3", "unit": "kg", "line_total_idr": 150000}
        ], [], restaurant_id=RESTAURANT["id"])
        self._audit(conn, "demo_seeded", "Synthetic sample history and estimated opening stock loaded.", RESTAURANT["id"])

    def _audit(self, conn, event, message, restaurant_id=None):
        conn.execute("INSERT INTO activity (id, event, message, created_at, restaurant_id) VALUES (?, ?, ?, ?, ?)", (uuid.uuid4().hex, event, message, now(), restaurant_id))

    def _normalize(self, items, catalog):
        return normalize_items_for_catalog(items, catalog)

    def _comparison(self, conn, item, supplier, restaurant_id):
        comparison = {"status": "insufficient_history", "change_percent": None, "baseline_unit_price_idr": None, "baseline_purchase_id": None,
                      "baseline_supplier": None, "baseline_created_at": None}
        if not item.get("unit_price_idr") or item.get("warnings") or not supplier:
            return comparison
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        rows = conn.execute("SELECT body FROM purchases WHERE restaurant_id=? AND supplier=? AND created_at>=? ORDER BY created_at DESC, id DESC", (restaurant_id, supplier, cutoff))
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

    def _present(self, conn, draft, restaurant_id):
        if draft["status"] == "recorded":
            # Preserve the comparison evidence visible when the owner approved.
            return draft
        # Also project older local-demo drafts safely after a service restart.
        draft["total_idr"] = draft_total(draft["items"], draft["warnings"])
        for item in draft["items"]:
            item["comparison"] = self._comparison(conn, item, draft["supplier"], restaurant_id)
        draft["comparison"] = next((item["comparison"] for item in draft["items"] if item["comparison"]["status"] == "price_increase"), None)
        return draft

    def _new_draft(self, conn, text, supplier, items, warnings, created_at=None, restaurant_id=None):
        if not restaurant_id:
            raise DomainError("restaurant_required", "A draft needs a restaurant.", 500)
        items, field_warnings = self._normalize(items, self._catalog_for(restaurant_id))
        all_warnings = list(dict.fromkeys(list(warnings) + field_warnings))
        draft = {
            "id": uuid.uuid4().hex, "version": 1,
            "status": "needs_clarification" if all_warnings else "pending_approval",
            "source_text": text, "supplier": supplier, "items": items,
            "total_idr": draft_total(items, all_warnings),
            "warnings": all_warnings, "created_at": created_at or now(),
        }
        self._save_draft(conn, draft, insert=True, restaurant_id=restaurant_id)
        self._audit(conn, "draft_created", f"Purchase draft created: {draft['id'][:8]}.", restaurant_id)
        return draft

    def _save_draft(self, conn, draft, insert=False, restaurant_id=None):
        if insert:
            conn.execute("INSERT INTO drafts (id, version, status, body, restaurant_id) VALUES (?, ?, ?, ?, ?)", (draft["id"], draft["version"], draft["status"], encode(draft), restaurant_id))
        else:
            conn.execute("UPDATE drafts SET version=?, status=?, body=? WHERE id=?", (draft["version"], draft["status"], encode(draft), draft["id"]))

    def _load_draft(self, conn, draft_id, restaurant_id):
        row = conn.execute("SELECT body, restaurant_id FROM drafts WHERE id=?", (draft_id,)).fetchone()
        # A wrong-tenant id is indistinguishable from a missing one: no existence leak.
        if not row or row["restaurant_id"] != restaurant_id:
            raise DomainError("not_found", "Draft not found.", 404)
        return json.loads(row["body"])

    def get_draft(self, draft_id, restaurant_id):
        with self.transaction() as conn:
            return self._present(conn, self._load_draft(conn, draft_id, restaurant_id), restaurant_id)

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

    def intake(self, key, payload, extracted=None, restaurant_id=None):
        # The API layer may pass a pre-extracted result (rules or rules-then-
        # model rescue); the shape is identical to extract_purchase output.
        if extracted is None or not isinstance(extracted.get("items"), list):
            extracted = extract_purchase(payload["text"])
        def action(conn):
            return self._present(conn, self._new_draft(conn, payload["text"], payload.get("supplier"), extracted["items"], extracted["warnings"], restaurant_id=restaurant_id), restaurant_id)
        return self._idempotent(key, "intake", payload, action)

    def intake_image(self, key, image_sha256, source_label, supplier, items, warnings, restaurant_id=None):
        """Create a draft from a vision extractor; the photo digest keys idempotency."""
        payload = {"image_sha256": image_sha256, "source_label": source_label, "supplier": supplier}
        def action(conn):
            return self._present(conn, self._new_draft(conn, source_label, supplier, items, warnings, restaurant_id=restaurant_id), restaurant_id)
        return self._idempotent(key, "intake_image", payload, action)

    def _check_editable(self, draft, version):
        if draft["version"] != version:
            raise DomainError("version_conflict", "The draft changed. Refresh it before continuing.")
        if draft["status"] not in ("pending_approval", "needs_clarification"):
            raise DomainError("draft_finalized", "A recorded or rejected draft cannot be changed.")

    def edit(self, draft_id, key, payload, restaurant_id=None):
        def action(conn):
            draft = self._load_draft(conn, draft_id, restaurant_id)
            self._check_editable(draft, payload["expected_version"])
            items, warnings = self._normalize(payload["items"], self._catalog_for(restaurant_id))
            draft.update({"items": items, "supplier": payload["supplier"], "warnings": warnings,
                          "status": "needs_clarification" if warnings else "pending_approval",
                          "total_idr": draft_total(items, warnings),
                          "version": draft["version"] + 1})
            self._save_draft(conn, draft)
            self._audit(conn, "draft_edited", f"Draft {draft_id[:8]} reviewed and edited (version {draft['version']}).", restaurant_id)
            return self._present(conn, draft, restaurant_id)
        return self._idempotent(key, "edit:" + draft_id, payload, action)

    def _post(self, conn, draft, created_at=None, restaurant_id=None):
        self._present(conn, draft, restaurant_id)
        purchase_id = uuid.uuid4().hex
        created_at = created_at or now()
        purchase = {"id": purchase_id, "draft_id": draft["id"], "supplier": draft["supplier"],
                    "total_idr": draft["total_idr"], "items": draft["items"], "created_at": created_at,
                    "payment_status": "unknown"}
        conn.execute("INSERT INTO purchases (id, draft_id, supplier, created_at, body, restaurant_id) VALUES (?, ?, ?, ?, ?, ?)", (purchase_id, draft["id"], draft["supplier"], created_at, encode(purchase), restaurant_id))
        deltas = {}
        for item in draft["items"]:
            ingredient = item["ingredient_id"]
            deltas[ingredient] = deltas.get(ingredient, Decimal(0)) + Decimal(item["canonical_quantity"])
        for ingredient, quantity in deltas.items():
            row = conn.execute("SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id=?", (restaurant_id, ingredient)).fetchone()
            stock = Decimal(row[0]) if row else Decimal(0)
            if row:
                conn.execute("UPDATE inventory SET quantity=? WHERE restaurant_id=? AND ingredient_id=?", (decimal_string(stock + quantity), restaurant_id, ingredient))
            else:
                conn.execute("INSERT INTO inventory (restaurant_id, ingredient_id, quantity) VALUES (?, ?, ?)", (restaurant_id, ingredient, decimal_string(quantity)))
            conn.execute("INSERT INTO movements (id, purchase_id, ingredient_id, quantity, created_at, restaurant_id) VALUES (?, ?, ?, ?, ?, ?)", (uuid.uuid4().hex, purchase_id, ingredient, decimal_string(quantity), created_at, restaurant_id))
        draft.update({"status": "recorded", "version": draft["version"] + 1, "purchase_id": purchase_id})
        self._save_draft(conn, draft)
        self._audit(conn, "purchase_recorded", f"Purchase recorded for Rp {draft['total_idr']:,}; received stock added. Payment status unknown.", restaurant_id)
        return draft

    def decision(self, draft_id, key, payload, restaurant_id=None):
        def action(conn):
            draft = self._load_draft(conn, draft_id, restaurant_id)
            return self._decide(conn, draft, payload, restaurant_id)

        draft = self._idempotent(key, "decision:" + draft_id, payload, action)
        self.maybe_cloud_mirror(draft, restaurant_id)
        return draft

    @staticmethod
    def _slugify(raw_name):
        slug = re.sub(r"[^a-z0-9]+", "_", str(raw_name).casefold()).strip("_")
        return re.sub(r"_+", "_", slug)[:100]

    def _resolve_new_ingredients(self, conn, draft, restaurant_id):
        """Link unmapped draft items to catalog entries, creating missing ones.

        Runs inside the approval transaction before posting: every recorded
        item must own a real ingredient for stock and history to line up.
        New ingredients belong to this restaurant only. Returns the display
        names of ingredients created by this approval.
        """
        catalog = self._catalog_for(restaurant_id)
        created = []
        for item in draft["items"]:
            if item.get("ingredient_id") or not str(item.get("raw_name") or "").strip():
                continue
            raw_name = item["raw_name"]
            slug = self._slugify(raw_name)
            if not slug:
                raise DomainError("clarification_required", "The ingredient name is not usable; correct it on the dashboard.", 422)
            if slug not in catalog["by_id"]:
                name = str(raw_name).strip().capitalize()
                unit = item.get("canonical_unit") or "kg"
                conn.execute("INSERT INTO custom_ingredients (id, name, unit, reorder_threshold, created_at, restaurant_id) VALUES (?, ?, ?, ?, ?, ?)", (slug, name, unit, "0", now(), restaurant_id))
                conn.execute("INSERT INTO inventory (restaurant_id, ingredient_id, quantity) VALUES (?, ?, '0')", (restaurant_id, slug))
                entry = {"id": slug, "name": name, "unit": unit, "reorder_threshold": "0"}
                catalog["list"].append(entry)
                catalog["by_id"][slug] = entry
                self._audit(conn, "ingredient_created", f"Bahan baru '{name}' ditambahkan ke katalog otomatis.", restaurant_id)
                created.append(name)
            item["ingredient_id"] = slug
        if created:
            draft["new_ingredients"] = created
        return draft

    def _decide(self, conn, draft, payload, restaurant_id):
        """Shared transaction-internal decision path for dashboard and WhatsApp."""
        self._check_editable(draft, payload["expected_version"])
        if payload["decision"] == "reject":
            draft.update({"status": "rejected", "version": draft["version"] + 1})
            self._save_draft(conn, draft)
            self._audit(conn, "draft_rejected", f"Draft {draft['id'][:8]} rejected; no purchase or inventory posted.", restaurant_id)
        else:
            _, invalid = self._normalize(draft["items"], self._catalog_for(restaurant_id))
            if draft["warnings"] or invalid:
                raise DomainError("clarification_required", "Review and correct all draft warnings before approval.", 422)
            if not payload["received"]:
                raise DomainError("receipt_required", "Confirm that the goods were received before recording stock.", 422)
            draft = self._resolve_new_ingredients(conn, draft, restaurant_id)
            draft = self._post(conn, draft, restaurant_id=restaurant_id)
        return self._present(conn, draft, restaurant_id)

    def _whatsapp_thread(self, conn, sender, channel, transport, restaurant_id=None, create=False):
        row = conn.execute("SELECT * FROM whatsapp_threads WHERE channel=? AND sender=?", (channel, sender)).fetchone()
        if row:
            thread = dict(row)
            # A number registered after it messaged unclaimed keeps its history
            # but gains the restaurant binding from the new link.
            if restaurant_id and thread.get("restaurant_id") is None:
                thread["restaurant_id"] = restaurant_id
                conn.execute("UPDATE whatsapp_threads SET restaurant_id=? WHERE id=?", (restaurant_id, thread["id"]))
            return thread
        thread = {"id": hashlib.sha256((channel + "\0" + sender).encode()).hexdigest()[:32], "channel": channel,
                  "sender": sender, "transport": transport, "active_draft_id": None, "active_version": None,
                  "pending_followup": None, "restaurant_id": restaurant_id}
        if create:
            conn.execute("INSERT INTO whatsapp_threads (id, channel, sender, transport, active_draft_id, active_version, pending_followup, restaurant_id) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?)", (thread["id"], channel, sender, transport, restaurant_id))
        return thread

    def _whatsapp_snapshot(self, conn, thread, draft=None, result=None):
        tenant = thread.get("restaurant_id")
        # A dashboard decision may have finalized the correlated draft.
        active = self._load_draft(conn, thread["active_draft_id"], tenant) if thread["active_draft_id"] and tenant else None
        active_id = active["id"] if active and active["status"] in ("pending_approval", "needs_clarification") else None
        response = {"thread": {"id": thread["id"], "sender": thread["sender"], "transport": thread["transport"], "active_draft_id": active_id},
                    "messages": [dict(row) for row in reversed(conn.execute("SELECT id, direction, body, created_at, transport, status, draft_id FROM whatsapp_messages WHERE thread_id=? ORDER BY rowid DESC LIMIT 100", (thread["id"],)).fetchall())]}
        visible_draft = draft or (active if active_id else None)
        if visible_draft:
            response["draft"] = self._present(conn, visible_draft, tenant)
        if result:
            response["result"] = result
        return response

    def whatsapp_messages(self, sender="demo", restaurant_id=None):
        with self.transaction() as conn:
            thread = self._whatsapp_thread(conn, sender, SIMULATOR, SIMULATOR)
            if thread.get("restaurant_id") != restaurant_id:
                raise DomainError("not_found", "No conversation for this sender.", 404)
            return self._whatsapp_snapshot(conn, thread)

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

    def stock_facts(self, text, restaurant_id=None):
        """Read-only inventory lookup behind stock questions like 'berapa stok cabai?'.

        Facts come only from the asking restaurant's inventory and catalog —
        that scoping is the tenant guard for the whole answer. Returns
        {"question", "facts"} when the message is a stock question, else None.
        Read-only: no draft, no ledger.
        """
        if not is_stock_question(text) or not restaurant_id:
            return None
        catalog = self._catalog_for(restaurant_id)
        with self.transaction() as conn:
            named = self._stock_ingredient_ids(text, restaurant_id)
            ingredients = [catalog["by_id"][ingredient_id] for ingredient_id in named if ingredient_id in catalog["by_id"]] if named else catalog["list"]
            facts = []
            for ingredient in ingredients:
                row = conn.execute("SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id=?", (restaurant_id, ingredient["id"])).fetchone()
                quantity = row[0] if row else "0"
                facts.append({"ingredient_id": ingredient["id"], "name": ingredient["name"], "quantity": quantity,
                              "unit": ingredient["unit"], "reorder_threshold": ingredient["reorder_threshold"],
                              "low_stock": Decimal(quantity) < Decimal(ingredient["reorder_threshold"])})
        return {"question": str(text).strip(), "facts": facts, "restaurant_name": self.restaurant_name(restaurant_id)}

    def _stock_ingredient_ids(self, text, restaurant_id):
        """Catalog ids whose names appear in the message, longest name first.

        Uses the same alias mapping as purchase extraction, extended with the
        display names and slugs of this restaurant's runtime ingredients, so a
        custom ingredient created from an approved draft is askable — and only
        by the restaurant that created it.
        """
        lowered = " " + re.sub(r"\s+", " ", str(text).strip().casefold()) + " "
        aliases = list(catalog_aliases())
        covered = {ingredient_id for _alias, ingredient_id in aliases}
        for ingredient in self._catalog_for(restaurant_id)["list"]:
            if ingredient["id"] in covered:
                continue
            for surface in (str(ingredient["id"]).replace("_", " "), str(ingredient["name"]).casefold()):
                surface = surface.strip()
                if surface:
                    aliases.append((surface, ingredient["id"]))
        found, seen = [], set()
        claimed = []
        for alias, ingredient_id in sorted(aliases, key=lambda pair: len(pair[0]), reverse=True):
            if ingredient_id in seen:
                continue
            for match in re.finditer(r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])", lowered):
                # Longest aliases claim their span first, so "bawang putih"
                # matches only bawang_putih and never also bawang.
                if any(match.start() < end and start < match.end() for start, end in claimed):
                    continue
                claimed.append((match.start(), match.end()))
                found.append(ingredient_id)
                seen.add(ingredient_id)
                break
        return found

    def restaurant_name(self, restaurant_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT name FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        return row[0] if row else None

    def whatsapp_ingest(self, key, payload, *, transport=SIMULATOR, channel=None, outbound_status=None, restaurant_id=None, open_onboarding=False):
        """Persist inbound, decision/ledger, outbound and dedupe in one commit.

        The WhatsApp allowlist is the tenant gate: a sender's phone number must
        be linked to exactly one restaurant, and everything this message does —
        drafts, approval, summaries — is scoped to that restaurant.
        Unregistered senders get a polite rejection and an audit row for the
        admin console; their message is never processed.
        """
        channel = channel or transport
        extracted_photo = payload.get("extracted")
        source_label = payload.get("source_label") or "Foto nota WhatsApp"
        # A voice transcript, like a vision read, is not byte-identical across
        # Meta retries (ASR is non-deterministic), so it stays out of both
        # dedupe fingerprints below; the Meta event itself is the replay guard.
        transcript = payload.get("transcript")
        transcript = transcript.strip()[:MAX_TRANSCRIPT_CHARS] if isinstance(transcript, str) else ""
        # A rules-or-model extraction made by the API layer for this text or
        # transcript; it is not part of the dedupe fingerprint for the same
        # non-determinism reason as the transcript itself.
        stock_answer = payload.get("stock_answer")
        if not isinstance(stock_answer, dict) or not isinstance(stock_answer.get("body"), str) or not stock_answer["body"].strip():
            stock_answer = None
        extracted_text = payload.get("extracted_text")
        if not isinstance(extracted_text, dict) or not isinstance(extracted_text.get("items"), list) or not isinstance(extracted_text.get("warnings"), list):
            extracted_text = None
        payload = {"sender": payload["sender"], "text": payload["text"], "event_id": payload.get("event_id"), "num_media": payload.get("num_media", 0)}
        event_id = payload["event_id"] or "sim:" + key
        fingerprint = hashlib.sha256(encode({"sender": payload["sender"], "text": payload["text"], "num_media": payload["num_media"]}).encode()).hexdigest()

        def action(conn):
            prior = conn.execute("SELECT fingerprint, response FROM whatsapp_events WHERE channel=? AND event_id=?", (channel, event_id)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DomainError("event_conflict", "This message event was already used for different content or sender.")
                return json.loads(prior["response"])
            link = conn.execute("SELECT restaurant_id FROM whatsapp_links WHERE phone=?", (payload["sender"],)).fetchone()
            linked = link["restaurant_id"] if link else None
            onboarding_reply = None
            if linked is None and open_onboarding:
                rows = [dict(row) for row in conn.execute("SELECT id, name FROM restaurants ORDER BY name, id")]
                choices = [row for row in rows if restaurant_id is None or row["id"] == restaurant_id]
                selected, onboarding_reply = restaurant_selection(choices, payload["text"])
                if selected:
                    phone = validate_phone_number(payload["sender"])
                    conn.execute("INSERT OR IGNORE INTO whatsapp_links (id, restaurant_id, phone, label, created_at) VALUES (?, ?, ?, ?, ?)",
                                 (uuid.uuid4().hex, selected["id"], phone, "WhatsApp self-registration", now()))
                    linked = conn.execute("SELECT restaurant_id FROM whatsapp_links WHERE phone=?", (phone,)).fetchone()[0]
                    if linked != selected["id"]:
                        raise DomainError("sender_link_changed", "This number was linked elsewhere. Please send your message again.", 409)
            if restaurant_id is not None and linked != restaurant_id and not (linked is None and open_onboarding):
                raise DomainError("sender_not_registered", "This WhatsApp number is not linked to your restaurant.", 403)
            tenant = linked
            thread = self._whatsapp_thread(conn, payload["sender"], channel, transport, restaurant_id=tenant, create=True)
            names = self.catalog_names(tenant) if tenant else None
            draft = self._load_draft(conn, thread["active_draft_id"], tenant) if thread["active_draft_id"] and tenant else None
            if draft and draft["status"] not in ("pending_approval", "needs_clarification"):
                draft = None
                thread.update(active_draft_id=None, active_version=None)
            # The payment/supplier follow-up is a one-shot offer: the very next
            # message either answers it or the opportunity lapses.
            pending_purchase = thread.get("pending_followup")
            thread["pending_followup"] = None
            command = payload["text"].strip().casefold()
            result = {}
            if onboarding_reply is not None:
                reply = onboarding_reply
                result["action"] = "restaurant_selected" if tenant else "restaurant_selection_required"
            elif tenant is None:
                # Unregistered sender: a read-only rejection. No draft, no
                # ledger, no model call happened upstream; the admin sees the
                # attempt in the console and can register the number.
                reply = UNREGISTERED_REPLY
                result["action"] = "unregistered_sender"
            elif extracted_photo:
                if draft:
                    reply = "Foto diterima. Masih ada draf yang menunggu konfirmasimu dulu, jadi foto ini belum dicatat:\n" + draft_reply(draft, names)
                    result["action"] = "active_draft_exists"
                else:
                    draft = self._new_draft(conn, source_label, extracted_photo.get("supplier"), extracted_photo.get("items") or [], extracted_photo.get("warnings") or [], restaurant_id=tenant)
                    thread.update(active_draft_id=draft["id"], active_version=draft["version"])
                    reply = "Nota diterima. Cek draf pembelian ini:\n" + draft_reply(draft, names)
                    result["action"] = "draft_created" if draft["status"] == "pending_approval" else "review_required"
            elif transcript:
                # A transcribed voice note follows the exact same review flow as
                # typed text: parse it, propose a draft, wait for YA/BATAL.
                if draft:
                    reply = "Pesan suara diterima. Masih ada draf yang menunggu konfirmasimu dulu, jadi pesan ini belum dicatat:\n" + draft_reply(draft, names)
                    result["action"] = "active_draft_exists"
                else:
                    extracted = extracted_text if extracted_text is not None else extract_purchase(transcript)
                    draft = self._new_draft(conn, transcript, None, extracted["items"], extracted["warnings"], restaurant_id=tenant)
                    thread.update(active_draft_id=draft["id"], active_version=draft["version"])
                    reply = draft_reply(draft, names)
                    result["action"] = "draft_created" if draft["status"] == "pending_approval" else "review_required"
            elif payload["num_media"]:
                reply = "Foto/audio belum diproses. Kirim rincian pembelian sebagai teks atau gunakan dashboard. Tidak ada pembelian yang dicatat."
                result["action"] = "media_unsupported"
            elif pending_purchase and not draft and (followup := parse_payment_followup(payload["text"])):
                purchase, supplier_new = self._update_purchase_payment(conn, pending_purchase, followup["payment_status"], followup["supplier"], tenant)
                if followup["skip"]:
                    reply = FOLLOWUP_ACK
                    result["action"] = "followup_skipped"
                else:
                    details = []
                    if followup["payment_status"] == "paid":
                        details.append("Pembayaran dicatat: lunas.")
                    elif followup["payment_status"] == "unpaid":
                        details.append("Pembayaran dicatat: utang (belum dibayar).")
                    if purchase.get("supplier"):
                        suffix = " (pemasok baru)." if supplier_new else "."
                        details.append(f"Penjual: {purchase['supplier']}{suffix}")
                    reply = "Oke, catatan diperbarui. " + " ".join(details) if details else FOLLOWUP_ACK
                    result["action"] = "purchase_updated"
            elif command in APPROVE_COMMANDS or command in REJECT_COMMANDS:
                if not draft:
                    reply = "Tidak ada draf aktif untuk nomor ini. Kirim rincian pembelian terlebih dahulu; YA tidak mencatat pembelian lain."
                    result["action"] = "no_active_draft"
                elif command in REJECT_COMMANDS:
                    draft = self._decide(conn, draft, {"expected_version": draft["version"], "decision": "reject", "received": False}, tenant)
                    thread.update(active_draft_id=None, active_version=None)
                    reply = f"Draf {draft['id'][:8]} dibatalkan. Pembelian dan stok tidak berubah."
                    result["action"] = "rejected"
                elif draft["status"] == "needs_clarification":
                    reply = draft_reply(draft, names)
                    result["action"] = "review_required"
                elif draft["version"] != thread["active_version"]:
                    # Dashboard edits invalidate the version previously summarized.
                    thread["active_version"] = draft["version"]
                    reply = "Draf berubah sejak pesan sebelumnya. Tinjau ulang rincian berikut dan balas YA lagi jika benar.\n" + draft_reply(draft, names)
                    result["action"] = "review_required"
                else:
                    draft = self._decide(conn, draft, {"expected_version": thread["active_version"], "decision": "approve", "received": True}, tenant)
                    thread.update(active_draft_id=None, active_version=None)
                    confirmation = f"Pembelian {draft['id'][:8]} tercatat. Stok diperbarui."
                    names_created = draft.get("new_ingredients") or []
                    if names_created:
                        confirmation += " " + ", ".join(f"'{name}'" for name in names_created) + " ditambahkan sebagai bahan baru."
                    thread["pending_followup"] = draft["purchase_id"]
                    reply = confirmation + "\n" + PAYMENT_FOLLOWUP_PROMPT
                    result.update(action="approved", purchase_id=draft["purchase_id"])
            elif command in SUMMARY_COMMANDS:
                # A read-only report: never creates or disturbs an active draft.
                summary = self._weekly_summary(conn, SUMMARY_WINDOW_DAYS, tenant)
                reply = weekly_summary_reply(summary, SUMMARY_WINDOW_DAYS)
                result["action"] = "weekly_summary"
                result["summary"] = summary
            elif draft and (remove_index := parse_remove_item(payload["text"])):
                # 'hapus 2' drops the 1-based flagged item from the active
                # draft — the owner's confirmation for the kitchen filter.
                flagged = [position for position, item in enumerate(draft["items"]) if item.get("non_kitchen")]
                if remove_index - 1 not in flagged:
                    hint = ", ".join(str(position + 1) for position in flagged) or "tidak ada"
                    reply = f"Item {remove_index} tidak bertanda bukan bahan masak. Hapus hanya baris bertanda: " + hint + "."
                    result["action"] = "remove_rejected"
                elif len(draft["items"]) <= 1:
                    reply = "Draf tinggal satu baris. Balas BATAL untuk membatalkan seluruh draf."
                    result["action"] = "remove_rejected"
                else:
                    removed = draft["items"].pop(remove_index - 1)
                    # Re-derive warnings for the remaining items: numbered
                    # warnings ('Item 2: ...') shift when a line disappears.
                    remaining, field_warnings = self._normalize(draft["items"], self._catalog_for(tenant))
                    flags = [item.get("non_kitchen") for item in draft["items"]]
                    for item, flagged in zip(remaining, flags):
                        if flagged:
                            item["non_kitchen"] = True
                    kept_warnings = [warning for warning in draft.get("warnings") or []
                                     if "bukan bahan masak" in warning
                                     and any(item.get("non_kitchen") and item.get("raw_name") and item["raw_name"] in warning for item in remaining)]
                    all_warnings = list(dict.fromkeys(kept_warnings + field_warnings))
                    draft.update({"items": remaining, "warnings": all_warnings,
                                  "status": "needs_clarification" if all_warnings else "pending_approval",
                                  "total_idr": draft_total(remaining, all_warnings),
                                  "version": draft["version"] + 1})
                    self._save_draft(conn, draft)
                    thread["active_version"] = draft["version"]
                    self._audit(conn, "draft_item_removed", f"Non-kitchen item '{removed.get('raw_name')}' removed from draft {draft['id'][:8]}.", tenant)
                    reply = f"Item '{removed.get('raw_name')}' dihapus dari draf.\n" + draft_reply(draft, names)
                    result["action"] = "item_removed"
            elif stock_answer:
                # A stock question answered from inventory facts before the
                # ingest; read-only like the summary, so an active draft
                # stays untouched and no purchase line is filed.
                reply = stock_answer["body"]
                result["action"] = "stock_answer"
            elif draft:
                reply = draft_reply(draft, names)
                result["action"] = "active_draft_exists"
            else:
                extracted = extracted_text if extracted_text is not None else extract_purchase(payload["text"])
                # Plain text with no purchase signal (a greeting, chat noise) is
                # an intent miss: every parsed line is a bare word with no
                # catalog match, quantity, or price. Answer with help instead of
                # filing an empty draft on the attention list.
                if all(item.get("ingredient_id") is None and item.get("quantity") is None and item.get("line_total_idr") is None for item in extracted["items"]):
                    reply = HELP_REPLY
                    result["action"] = "help_reply"
                else:
                    draft = self._new_draft(conn, payload["text"], None, extracted["items"], extracted["warnings"], restaurant_id=tenant)
                    thread.update(active_draft_id=draft["id"], active_version=draft["version"])
                    reply = draft_reply(draft, names)
                    result["action"] = "draft_created" if draft["status"] == "pending_approval" else "review_required"
            conn.execute("UPDATE whatsapp_threads SET active_draft_id=?, active_version=?, pending_followup=? WHERE id=?", (thread["active_draft_id"], thread["active_version"], thread.get("pending_followup"), thread["id"]))
            draft_id = draft["id"] if draft else None
            if extracted_photo:
                inbound_body = f"[Foto nota] {source_label}"
            elif transcript:
                inbound_body = f"[Catatan suara] {transcript}"
            elif payload["num_media"]:
                inbound_body = f"[Media omitted: {payload['num_media']} attachment(s)] " + payload["text"]
            else:
                inbound_body = payload["text"]
            reply_status = outbound_status or ("simulated" if transport == SIMULATOR else "prepared")
            for direction, body, status in (("inbound", inbound_body, "received"), ("outbound", reply, reply_status)):
                conn.execute("INSERT INTO whatsapp_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (uuid.uuid4().hex, thread["id"], direction, body, now(), transport, status, draft_id))
            if result["action"] == "unregistered_sender":
                self._audit(conn, "whatsapp_unregistered", f"Rejected a message from unregistered WhatsApp sender {payload['sender']}.")
            else:
                self._audit(conn, "whatsapp_message_processed", f"{transport} message processed: {result['action']}.", tenant)
            response = self._whatsapp_snapshot(conn, thread, draft=draft, result=result)
            conn.execute("INSERT INTO whatsapp_events VALUES (?, ?, ?, ?, ?)", (channel, event_id, thread["id"], fingerprint, encode(response)))
            return response

        response = self._idempotent(key, "whatsapp:" + channel, payload, action)
        self.maybe_cloud_mirror(response.get("draft"), response["thread"].get("restaurant_id"))
        return response

    def _weekly_summary(self, conn, days=SUMMARY_WINDOW_DAYS, restaurant_id=None):
        """Aggregate recorded purchases inside the window; zeroed when empty."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        catalog_by_id = self._catalog_for(restaurant_id)["by_id"]
        summary = {"purchase_count": 0, "total_spend_idr": 0, "top_item": None, "biggest_price_spike": None}
        quantities = {}
        for row in conn.execute("SELECT body FROM purchases WHERE restaurant_id=? AND created_at>=? ORDER BY created_at, id", (restaurant_id, cutoff)):
            purchase = json.loads(row["body"])
            summary["purchase_count"] += 1
            total = purchase.get("total_idr")
            if isinstance(total, int) and not isinstance(total, bool) and 0 < total <= 1_000_000_000_000:
                summary["total_spend_idr"] += total
            for item in purchase.get("items") or []:
                ingredient = catalog_by_id.get(item.get("ingredient_id"))
                if ingredient and item.get("canonical_quantity") and item.get("canonical_unit"):
                    key = (item["ingredient_id"], item["canonical_unit"])
                    quantities[key] = quantities.get(key, Decimal(0)) + Decimal(item["canonical_quantity"])
                comparison = item.get("comparison") or {}
                if comparison.get("status") == "price_increase" and comparison.get("change_percent") is not None:
                    try:
                        change = Decimal(str(comparison["change_percent"]))
                    except InvalidOperation:
                        continue
                    spike = summary["biggest_price_spike"]
                    if spike is None or change > Decimal(spike["change_percent"]):
                        summary["biggest_price_spike"] = {"name": ingredient["name"] if ingredient else str(item.get("raw_name") or ""), "change_percent": format(change, "f")}
        if quantities:
            (ingredient_id, unit), total = max(quantities.items(), key=lambda entry: (entry[1], entry[0]))
            summary["top_item"] = {"name": catalog_by_id[ingredient_id]["name"], "quantity": decimal_string(total), "unit": unit}
        return summary

    def weekly_summary(self, days=SUMMARY_WINDOW_DAYS, restaurant_id=None):
        with self.transaction() as conn:
            return self._weekly_summary(conn, days, restaurant_id)

    def export_purchases(self, restaurant_id=None):
        """Flat purchase rows for the CSV export; newest first."""
        with self.transaction() as conn:
            rows = []
            for row in conn.execute("SELECT id, supplier, created_at, body FROM purchases WHERE restaurant_id=? ORDER BY created_at DESC, id DESC", (restaurant_id,)):
                purchase = json.loads(row["body"])
                total = purchase.get("total_idr")
                rows.append({
                    "id": purchase.get("id") or row["id"],
                    "created_at": row["created_at"],
                    "supplier": row["supplier"] or "",
                    "item_count": len(purchase.get("items") or []),
                    "total_idr": total if isinstance(total, int) and not isinstance(total, bool) else "",
                    "status": purchase.get("payment_status") or "",
                })
            return rows

    def export_stock(self, restaurant_id=None):
        """Catalog stock rows with the most recent recorded unit price per ingredient."""
        catalog = self._catalog_for(restaurant_id)
        with self.transaction() as conn:
            last_prices = {}
            for row in conn.execute("SELECT body FROM purchases WHERE restaurant_id=? ORDER BY created_at DESC, id DESC", (restaurant_id,)):
                for item in json.loads(row["body"]).get("items") or []:
                    ingredient_id = item.get("ingredient_id")
                    price = item.get("unit_price_idr")
                    if ingredient_id and price and ingredient_id not in last_prices:
                        last_prices[ingredient_id] = price
            rows = []
            for ingredient in catalog["list"]:
                row = conn.execute("SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id=?", (restaurant_id, ingredient["id"])).fetchone()
                quantity = row[0] if row else "0"
                rows.append({"item": ingredient["name"], "unit": ingredient["unit"], "current_stock": quantity, "last_unit_price": last_prices.get(ingredient["id"])})
            return rows

    def _canonical_supplier(self, conn, supplier, exclude_id=None, restaurant_id=None):
        """Match a free-typed supplier name against suppliers already recorded.

        Keeps one supplier on one canonical ledger name: 'di aeon', 'AEON',
        and 'aeon mall' resolve to the stored form when a confident match
        exists. Deterministic and auditable — exact match ignoring case and
        spacing first, then containment of a 3+ character name. Returns
        (canonical_name, matched_existing).
        """
        def norm(value):
            return " ".join(str(value).casefold().split())

        candidate = norm(supplier)
        if not candidate:
            return supplier, True
        rows = conn.execute(
            "SELECT DISTINCT supplier FROM purchases WHERE restaurant_id=? AND supplier IS NOT NULL AND supplier != '' AND id != ?",
            (restaurant_id, exclude_id or ""),
        ).fetchall()
        stored = {row["supplier"]: norm(row["supplier"]) for row in rows}
        for name, normed in stored.items():
            if normed == candidate:
                return name, True
        for name, normed in sorted(stored.items(), key=lambda item: len(item[1])):
            if (len(candidate) >= 3 and candidate in normed) or (len(normed) >= 3 and normed in candidate):
                return name, True
        return supplier, False

    def _update_purchase_payment(self, conn, purchase_id, payment_status, supplier, restaurant_id=None):
        """Persist payment/supplier details on one recorded purchase.

        Returns (purchase, supplier_new) where supplier_new is True when the
        supplier text was stored as a brand-new ledger name instead of
        resolving to an existing one.
        """
        row = conn.execute("SELECT body, restaurant_id FROM purchases WHERE id=?", (purchase_id,)).fetchone()
        if not row or row["restaurant_id"] != restaurant_id:
            raise DomainError("not_found", "Purchase not found.", 404)
        purchase = json.loads(row["body"])
        if payment_status:
            purchase["payment_status"] = payment_status
        supplier_is_new = None
        if supplier:
            canonical, matched_existing = self._canonical_supplier(conn, supplier, exclude_id=purchase_id, restaurant_id=restaurant_id)
            purchase["supplier"] = canonical
            supplier_is_new = not matched_existing
        conn.execute("UPDATE purchases SET body=?, supplier=? WHERE id=?", (encode(purchase), purchase.get("supplier"), purchase_id))
        self._audit(conn, "purchase_updated", f"Purchase {purchase_id[:8]} payment details completed via WhatsApp follow-up.", restaurant_id)
        return purchase, supplier_is_new

    def update_purchase_payment(self, purchase_id, restaurant_id=None, payment_status=None, supplier=None):
        """Public update path so dashboard or tests can complete a recorded purchase."""
        with self.transaction() as conn:
            purchase, _ = self._update_purchase_payment(conn, purchase_id, payment_status, supplier, restaurant_id)
            return purchase

    def maybe_cloud_mirror(self, draft, restaurant_id=None) -> dict | None:
        """Mirror a freshly recorded purchase into Supabase (opt-in, fail-soft).

        Called after the SQLite transaction has committed; SQLite stays the
        authority. Any failure lands in the activity feed as a visible audit
        event instead of breaking the demo.
        """
        if not (draft and draft.get("purchase_id") and draft.get("status") == "recorded"):
            return None
        from .supabase_db import mirror_enabled, mirror_purchase  # lazy: keep demo startup free of cloud deps

        if not mirror_enabled():
            return None
        try:
            with self.transaction() as conn:
                row = conn.execute("SELECT body FROM purchases WHERE id=?", (draft["purchase_id"],)).fetchone()
                restaurant = conn.execute("SELECT id, name FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
            if not row:
                return None
            purchase = json.loads(row["body"])
            result = mirror_purchase(purchase, dict(restaurant) if restaurant else None)
            event = "cloud_mirror_ok" if result.get("status") in ("mirrored", "already_mirrored") else "cloud_mirror_failed"
            detail = result.get("error", "") if event == "cloud_mirror_failed" else f"invoice {result.get('invoice_id', '')[:8]}"
            with self.transaction() as conn:
                self._audit(conn, event, f"Cloud mirror {result.get('status')}: {detail}".rstrip(": "), restaurant_id)
        except Exception as error:  # noqa: BLE001 - mirror bookkeeping must never break the demo
            try:
                with self.transaction() as conn:
                    self._audit(conn, "cloud_mirror_failed", f"Cloud mirror error: {str(error)[:200]}", restaurant_id)
            except Exception:  # noqa: BLE001
                pass
        return None

    def overview(self, restaurant_id=None):
        catalog = self._catalog_for(restaurant_id)
        with self.transaction() as conn:
            restaurant = conn.execute("SELECT id, name, address FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
            if not restaurant:
                raise DomainError("not_found", "Restaurant not found.", 404)
            drafts = [self._present(conn, json.loads(row["body"]), restaurant_id) for row in conn.execute("SELECT body FROM drafts WHERE restaurant_id=? ORDER BY rowid DESC", (restaurant_id,))]
            purchases = [json.loads(row["body"]) for row in conn.execute("SELECT body FROM purchases WHERE restaurant_id=? ORDER BY created_at DESC, id DESC", (restaurant_id,))]
            inventory = []
            for ingredient in catalog["list"]:
                row = conn.execute("SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id=?", (restaurant_id, ingredient["id"])).fetchone()
                quantity = row[0] if row else "0"
                inventory.append({"ingredient_id": ingredient["id"], "name": ingredient["name"], "quantity": quantity,
                                  "unit": ingredient["unit"], "reorder_threshold": ingredient["reorder_threshold"],
                                  "low_stock": Decimal(quantity) < Decimal(ingredient["reorder_threshold"])})
            today = datetime.now(RESTAURANT_TIMEZONE).date()
            # Provenance is read from the message ledger, never guessed: the
            # earliest inbound WhatsApp message that produced each draft. Drafts
            # without one came from dashboard intake and stay unattributed.
            sources = {}
            for row in conn.execute("SELECT w.draft_id, w.transport, w.created_at FROM whatsapp_messages w JOIN whatsapp_threads t ON t.id=w.thread_id WHERE w.direction='inbound' AND w.draft_id IS NOT NULL AND t.restaurant_id=? ORDER BY w.rowid", (restaurant_id,)):
                sources.setdefault(row["draft_id"], {"kind": "whatsapp" if row["transport"] == LIVE else "simulator", "created_at": row["created_at"]})
            for draft in drafts:
                if draft["id"] in sources:
                    draft["source"] = sources[draft["id"]]
            purchase_days = [datetime.fromisoformat(row["created_at"]).astimezone(RESTAURANT_TIMEZONE).date() for row in purchases]
            return {
                "restaurant": dict(restaurant),
                "whatsapp_numbers": [dict(row) for row in conn.execute("SELECT phone, label, created_at FROM whatsapp_links WHERE restaurant_id=? ORDER BY rowid", (restaurant_id,))],
                "mode": {"transport": "manual", "extraction": "rules", "storage": "sqlite", "identity": "session"},
                "metrics": {"purchases_today_idr": sum(row["total_idr"] for row, day in zip(purchases, purchase_days) if day == today),
                            "purchases_yesterday_idr": sum(row["total_idr"] for row, day in zip(purchases, purchase_days) if day == today - timedelta(days=1)),
                            "pending_approvals": sum(row["status"] in ("pending_approval", "needs_clarification") for row in drafts),
                            "low_stock_count": sum(row["low_stock"] for row in inventory), "recorded_purchases": len(purchases)},
                "drafts": drafts, "purchases": purchases, "inventory": inventory,
                "activity": [dict(row) for row in conn.execute("SELECT * FROM activity WHERE restaurant_id=? ORDER BY created_at DESC, id DESC LIMIT 100", (restaurant_id,))],
                "ingredients": catalog["list"],
            }

    # ---- Authentication: users, password checks, sessions -----------------

    def verify_login(self, username, password):
        """Check credentials and return the public user record; 401 on any miss."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT u.id, u.username, u.display_name, u.role, u.restaurant_id, u.password_hash, r.name AS restaurant_name"
                " FROM users u LEFT JOIN restaurants r ON r.id=u.restaurant_id WHERE username=?",
                (str(username or "").strip(),),
            ).fetchone()
        if not row or not verify_password(str(password or ""), row["password_hash"]):
            raise DomainError("invalid_credentials", "Unknown username or wrong password.", 401)
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
                "role": row["role"], "restaurant_id": row["restaurant_id"], "restaurant_name": row["restaurant_name"]}

    def create_session(self, user_id):
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + SESSION_TTL).isoformat()
        with self.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
            conn.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)", (token, user_id, now(), expires))
        return token

    def session_user(self, token):
        """Resolve a session token to its user, or None when missing or expired."""
        if not token:
            return None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT s.expires_at, u.id, u.username, u.display_name, u.role, u.restaurant_id, r.name AS restaurant_name"
                " FROM sessions s JOIN users u ON u.id=s.user_id LEFT JOIN restaurants r ON r.id=u.restaurant_id WHERE s.token=?",
                (token,),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] <= now():
                conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                return None
            return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
                    "role": row["role"], "restaurant_id": row["restaurant_id"], "restaurant_name": row["restaurant_name"]}

    def delete_session(self, token):
        with self.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))

    # ---- WhatsApp allowlist: numbers to restaurants ------------------------

    def restaurant_for_sender(self, phone):
        """The {"id", "name"} of the restaurant a WhatsApp number is linked to, or None."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT l.restaurant_id, r.name FROM whatsapp_links l JOIN restaurants r ON r.id=l.restaurant_id WHERE l.phone=?",
                (str(phone or "").strip(),),
            ).fetchone()
        return {"id": row["restaurant_id"], "name": row["name"]} if row else None

    def linked_numbers(self, restaurant_id):
        with self.transaction() as conn:
            return [dict(row) for row in conn.execute("SELECT phone, label, created_at FROM whatsapp_links WHERE restaurant_id=? ORDER BY rowid", (restaurant_id,))]

    # ---- Admin console: restaurants and their WhatsApp numbers -------------

    def _restaurant_detail(self, conn, restaurant_id):
        row = conn.execute("SELECT id, name, address, created_at FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "Restaurant not found.", 404)
        detail = dict(row)
        detail["whatsapp_numbers"] = [dict(item) for item in conn.execute("SELECT phone, label, created_at FROM whatsapp_links WHERE restaurant_id=? ORDER BY rowid", (restaurant_id,))]
        detail["users"] = [dict(item) for item in conn.execute("SELECT username, display_name, role, created_at FROM users WHERE restaurant_id=? ORDER BY rowid", (restaurant_id,))]
        detail["pending_drafts"] = conn.execute("SELECT COUNT(*) FROM drafts WHERE restaurant_id=? AND status IN ('pending_approval','needs_clarification')", (restaurant_id,)).fetchone()[0]
        detail["recorded_purchases"] = conn.execute("SELECT COUNT(*) FROM purchases WHERE restaurant_id=?", (restaurant_id,)).fetchone()[0]
        return detail

    def create_restaurant(self, key, payload):
        """Create a restaurant with its owner login and (optionally) a first WhatsApp number."""
        name = str(payload.get("name") or "").strip()
        if not name:
            raise DomainError("invalid_request", "A restaurant name is required.", 422)
        name, address = name[:200], (str(payload.get("address") or "").strip() or None)
        username = validate_username(payload.get("owner_username"))
        password = validate_password(payload.get("owner_password"))
        phone = validate_phone_number(payload["whatsapp_number"]) if str(payload.get("whatsapp_number") or "").strip() else None
        label = (str(payload.get("number_label") or "").strip() or None)

        def action(conn):
            if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise DomainError("username_taken", "That owner username is already in use.", 409)
            if phone and conn.execute("SELECT 1 FROM whatsapp_links WHERE phone=?", (phone,)).fetchone():
                raise DomainError("phone_already_linked", "That WhatsApp number is already linked to a restaurant.", 409)
            restaurant_id = uuid.uuid4().hex
            conn.execute("INSERT INTO restaurants (id, name, address, created_at) VALUES (?, ?, ?, ?)", (restaurant_id, name, address, now()))
            conn.execute(
                "INSERT INTO users (id, username, password_hash, display_name, role, restaurant_id, created_at) VALUES (?, ?, ?, ?, 'owner', ?, ?)",
                (uuid.uuid4().hex, username, hash_password(password), f"Pemilik {name}"[:80], restaurant_id, now()),
            )
            if phone:
                conn.execute("INSERT INTO whatsapp_links (id, restaurant_id, phone, label, created_at) VALUES (?, ?, ?, ?, ?)", (uuid.uuid4().hex, restaurant_id, phone, label, now()))
            self._audit(conn, "restaurant_created", f"Restaurant '{name}' created with owner login '{username}'.", restaurant_id)
            return self._restaurant_detail(conn, restaurant_id)

        stored = {"name": name, "address": address, "owner_username": username, "whatsapp_number": phone, "number_label": label}
        return self._idempotent(key, "admin:create_restaurant", stored, action)

    def list_restaurants(self):
        with self.transaction() as conn:
            rows = conn.execute("SELECT id FROM restaurants ORDER BY created_at, id").fetchall()
            return [self._restaurant_detail(conn, row["id"]) for row in rows]

    def restaurant_detail(self, restaurant_id):
        with self.transaction() as conn:
            return self._restaurant_detail(conn, restaurant_id)

    def update_restaurant(self, restaurant_id, key, payload):
        name = str(payload.get("name") or "").strip()
        address = str(payload.get("address") or "").strip()

        def action(conn):
            current = conn.execute("SELECT name, address FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
            if not current:
                raise DomainError("not_found", "Restaurant not found.", 404)
            updated_name = name[:200] or current["name"]
            updated_address = address[:200] or current["address"]
            conn.execute("UPDATE restaurants SET name=?, address=? WHERE id=?", (updated_name, updated_address, restaurant_id))
            self._audit(conn, "restaurant_updated", f"Restaurant details updated: now '{updated_name}'.", restaurant_id)
            return self._restaurant_detail(conn, restaurant_id)

        return self._idempotent(key, f"admin:update_restaurant:{restaurant_id}", {"name": name, "address": address}, action)

    def add_whatsapp_number(self, restaurant_id, key, payload):
        phone = validate_phone_number(payload.get("whatsapp_number"))
        label = str(payload.get("label") or "").strip()[:100] or None

        def action(conn):
            if not conn.execute("SELECT 1 FROM restaurants WHERE id=?", (restaurant_id,)).fetchone():
                raise DomainError("not_found", "Restaurant not found.", 404)
            existing = conn.execute("SELECT restaurant_id FROM whatsapp_links WHERE phone=?", (phone,)).fetchone()
            if existing:
                if existing["restaurant_id"] == restaurant_id:
                    raise DomainError("phone_already_linked", "That WhatsApp number is already linked to this restaurant.", 409)
                raise DomainError("phone_already_linked", "That WhatsApp number is already linked to another restaurant.", 409)
            conn.execute("INSERT INTO whatsapp_links (id, restaurant_id, phone, label, created_at) VALUES (?, ?, ?, ?, ?)", (uuid.uuid4().hex, restaurant_id, phone, label, now()))
            self._audit(conn, "whatsapp_number_linked", f"WhatsApp number {phone} linked to this restaurant.", restaurant_id)
            return self._restaurant_detail(conn, restaurant_id)

        return self._idempotent(key, f"admin:add_number:{restaurant_id}", {"whatsapp_number": phone, "label": label}, action)

    def remove_whatsapp_number(self, restaurant_id, phone, key):
        phone = validate_phone_number(phone)

        def action(conn):
            changed = conn.execute("DELETE FROM whatsapp_links WHERE restaurant_id=? AND phone=?", (restaurant_id, phone)).rowcount
            if changed != 1:
                raise DomainError("not_found", "That WhatsApp number is not linked to this restaurant.", 404)
            # Threads keep their restaurant binding; the number simply stops
            # being accepted at the gate until it is linked again.
            self._audit(conn, "whatsapp_number_unlinked", f"WhatsApp number {phone} unlinked; its messages are no longer processed.", restaurant_id)
            return self._restaurant_detail(conn, restaurant_id)

        return self._idempotent(key, f"admin:remove_number:{restaurant_id}:{phone}", {}, action)

    def reset_owner_password(self, restaurant_id, key, payload):
        username = validate_username(payload.get("username"))
        password = validate_password(payload.get("new_password"))

        def action(conn):
            row = conn.execute("SELECT id FROM users WHERE username=? AND restaurant_id=? AND role='owner'", (username, restaurant_id)).fetchone()
            if not row:
                raise DomainError("not_found", "No owner with that username belongs to this restaurant.", 404)
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), row["id"]))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
            self._audit(conn, "owner_password_reset", f"Owner login '{username}' password reset; its sessions were signed out.", restaurant_id)
            return self._restaurant_detail(conn, restaurant_id)

        return self._idempotent(key, f"admin:reset_password:{restaurant_id}:{username}", {}, action)

    def recent_rejections(self, limit=20):
        """Unregistered-sender attempts, for the admin console."""
        with self.transaction() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT message, created_at FROM activity WHERE event='whatsapp_unregistered' AND restaurant_id IS NULL ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            )]
