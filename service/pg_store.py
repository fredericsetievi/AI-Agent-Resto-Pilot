"""PostgreSQL (Supabase) authority — the Store interface over psycopg2.

Drop-in replacement for service.db.Store with the same public methods and
DomainError semantics. Design notes:

* One connection per operation, explicit BEGIN/commit inside
  pg_transaction() — the same discipline as the SQLite store.
* Every operational row carries the tenant slug (application restaurant id);
  queries always filter by it.
* Draft and purchase bodies stay JSON documents (jsonb), so the application
  logic that projects and validates drafts is unchanged from SQLite.
* Timestamps are timestamptz in Postgres; the JSON bodies keep the original
  ISO strings so the dashboard payload shape is identical.
* Idempotency, event dedupe, and approval posting remain transactional:
  a crash rolls back the ledger, stock, audit and the stored response together.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from .inventory import InventoryMixin
from .operations import OperationsMixin
from .purchase_sources import PurchaseSourcesMixin
from .db import (
    APPROVE_COMMANDS, CATALOG, CATALOG_BY_ID, DEMO_NUMBERS, DEMO_USERS,
    DomainError, RESTAURANT, RESTAURANT_TIMEZONE, REJECT_COMMANDS,
    SCHEMA_VERSION, SESSION_TTL, SUMMARY_COMMANDS, SUMMARY_WINDOW_DAYS,
    MAX_TRANSCRIPT_CHARS,
    catalog_aliases, decimal_string, draft_total, encode, hash_password,
    normalize_items_for_catalog, now, validate_password, validate_phone_number,
    validate_username, verify_password,
)
from .extraction import extract_purchase
from .pg import pg_connection, pg_transaction
from .onboarding import restaurant_selection
from .whatsapp import (
    FOLLOWUP_ACK, HELP_REPLY, LIVE, PAYMENT_FOLLOWUP_PROMPT, SIMULATOR,
    UNREGISTERED_REPLY, draft_reply, is_stock_question, parse_payment_followup,
    parse_remove_request, stock_reply, weekly_summary_reply,
)


class PostgresStore(PurchaseSourcesMixin, OperationsMixin, InventoryMixin):
    """The RestoPilot authority in Supabase Postgres. Same contract as Store."""

    inventory_postgres = True

    # Tables/columns the application reads and writes (supabase/migrations/0003).
    REQUIRED_SCHEMA = {
        "restaurants": {"id", "name", "slug", "address"},
        "tenants": {"slug", "restaurant_id", "created_at"},
        "app_users": {"id", "username", "password_hash", "display_name", "role", "slug", "created_at"},
        "app_sessions": {"token", "user_id", "created_at", "expires_at"},
        "whatsapp_links": {"id", "slug", "phone", "label", "created_at"},
        "custom_ingredients": {"id", "slug", "name", "unit", "reorder_threshold", "created_at"},
        "drafts": {"id", "slug", "version", "status", "body", "created_at"},
        "purchases": {"id", "slug", "draft_id", "supplier", "created_at", "body"},
        "stock": {"slug", "ingredient_id", "quantity"},
        "movements": {"id", "slug", "purchase_id", "ingredient_id", "quantity", "created_at"},
        "activity": {"id", "slug", "event", "message", "created_at"},
        "idempotency": {"key", "slug", "fingerprint", "response", "created_at"},
        "whatsapp_threads": {"id", "slug", "channel", "sender", "transport", "active_draft_id",
                             "active_version", "pending_followup"},
        "whatsapp_messages": {"id", "thread_id", "direction", "body", "created_at", "transport",
                              "status", "draft_id"},
        "whatsapp_events": {"channel", "event_id", "thread_id", "fingerprint", "response"},
        "app_metadata": {"key", "value"},
    }

    def __init__(self, path=None, seed=True, read_only_startup=False):
        # `path` is accepted and ignored so call sites stay identical.
        self._catalogs = {}
        # Short-TTL overview cache: each Supabase round trip costs 0.1-0.9s,
        # and the dashboard polls every 5s — serve repeats from memory.
        self._overview_cache = {}
        self._overview_cache_ttl = 3.0
        if read_only_startup:
            # Hosted deployments attach to an existing ledger: validate only,
            # never insert demo users, allowlist rows, metadata or stock.
            self.validate_schema()
        else:
            self.initialize(seed)
        self._load_custom_ingredients()

    def validate_schema(self):
        """SELECT-only check that every required table and column exists."""
        with pg_connection() as conn:
            self._execute(conn, "SET TRANSACTION READ ONLY")
            cur = self._execute(
                conn,
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name = ANY(%s)",
                (list(self.REQUIRED_SCHEMA),),
            )
            found = {}
            for table, column in cur.fetchall():
                found.setdefault(table, set()).add(column)
        problems = []
        for table, columns in self.REQUIRED_SCHEMA.items():
            if table not in found:
                problems.append(f"missing table {table}")
                continue
            missing = sorted(columns - found[table])
            if missing:
                problems.append(f"{table} missing columns {', '.join(missing)}")
        if problems:
            raise RuntimeError("Supabase schema does not match RestoPilot: " + "; ".join(problems))

    # ---- connection helpers -------------------------------------------------

    @staticmethod
    def transaction():
        """Public multi-statement transaction, same contract as Store.transaction."""
        return pg_transaction()

    @staticmethod
    def _rows(cur):
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    @staticmethod
    def _execute(conn, sql, params=()):
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur

    @contextmanager
    def _query(self, sql, params=()):
        """Read-only one-shot: connection returned with an open cursor."""
        with pg_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            yield cur

    # ---- catalogs -----------------------------------------------------------

    def _catalog_for(self, restaurant_id):
        entry = self._catalogs.get(restaurant_id)
        if entry is None:
            entry = {"list": [dict(item) for item in CATALOG], "by_id": dict(CATALOG_BY_ID)}
            self._catalogs[restaurant_id] = entry
        return entry

    def catalog_names(self, restaurant_id):
        return {item["id"]: item["name"] for item in self._catalog_for(restaurant_id)["list"]}

    def catalog_list(self, restaurant_id):
        """The restaurant's full catalog (base + custom), for filter checks."""
        return self._catalog_for(restaurant_id)["list"]

    @staticmethod
    def slugify_name(raw_name):
        return PostgresStore._slugify(raw_name)

    # ---- bootstrap ----------------------------------------------------------

    def initialize(self, seed):
        """Ensure the demo tenant exists, then seed once when asked.

        Tables themselves come from supabase/migrations (applied out of band);
        this method is safe to call on every boot.
        """
        with pg_transaction() as conn:
            cur = self._execute(conn, "SELECT slug FROM tenants WHERE slug=%s", (RESTAURANT["id"],))
            if cur.fetchone() is None:
                restaurant = self._execute(
                    conn,
                    "INSERT INTO restaurants (id, name, phone_number, created_at) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING RETURNING id",
                    (str(uuid.uuid5(uuid.NAMESPACE_URL, f"restopilot:{RESTAURANT['id']}")), RESTAURANT["name"],
                     f"demo+{RESTAURANT['id']}", now()),
                ).fetchone()
                if restaurant is None:
                    restaurant = self._execute(conn, "SELECT id FROM restaurants WHERE name=%s LIMIT 1", (RESTAURANT["name"],)).fetchone()
                self._execute(
                    conn,
                    "INSERT INTO tenants (slug, restaurant_id) VALUES (%s, %s) ON CONFLICT (slug) DO NOTHING",
                    (RESTAURANT["id"], restaurant[0]),
                )
            for user in DEMO_USERS:
                self._execute(
                    conn,
                    "INSERT INTO app_users (id, username, password_hash, display_name, role, slug) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (username) DO NOTHING",
                    (uuid.uuid4().hex, user["username"], hash_password(user["password"]),
                     user["display_name"], user["role"], user["restaurant_id"]),
                )
            for number in DEMO_NUMBERS:
                self._execute(
                    conn,
                    "INSERT INTO whatsapp_links (id, slug, phone, label) VALUES (%s, %s, %s, %s) ON CONFLICT (phone) DO NOTHING",
                    (uuid.uuid4().hex, RESTAURANT["id"], number["phone"], number["label"]),
                )
            self._execute(
                conn,
                "INSERT INTO app_metadata (key, value) VALUES ('schema_version', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )
            cur = self._execute(conn, "SELECT 1 FROM app_metadata WHERE key='initialized'")
            fresh = cur.fetchone() is None
            if fresh:
                for ingredient in CATALOG:
                    self._execute(
                        conn,
                        "INSERT INTO stock (slug, ingredient_id, quantity) VALUES (%s, %s, '0') ON CONFLICT DO NOTHING",
                        (RESTAURANT["id"], ingredient["id"]),
                    )
                if seed:
                    self._seed(conn)
                self._execute(conn, "INSERT INTO app_metadata (key, value) VALUES ('initialized', %s)", (now(),))

    def _load_custom_ingredients(self):
        with pg_connection() as conn:
            cur = self._execute(
                conn,
                "SELECT slug, id, name, unit, reorder_threshold FROM custom_ingredients ORDER BY created_at, id",
            )
            for row in self._rows(cur):
                entry = {"id": row["id"], "name": row["name"], "unit": row["unit"], "reorder_threshold": row["reorder_threshold"]}
                catalog = self._catalog_for(row["slug"] or RESTAURANT["id"])
                catalog["list"].append(entry)
                catalog["by_id"][entry["id"]] = entry

    def _seed(self, conn):
        for ingredient, qty, total, age in [("cabai", "3", 129000, 3), ("telur", "10", 430000, 2)]:
            created_at = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
            draft = self._new_draft(conn, f"Synthetic history: {ingredient}", "Pasar Pagi", [
                {"ingredient_id": ingredient, "raw_name": CATALOG_BY_ID[ingredient]["name"], "quantity": qty, "unit": "kg", "line_total_idr": total}
            ], [], created_at=created_at, restaurant_id=RESTAURANT["id"])
            self._post(conn, draft, created_at=created_at, restaurant_id=RESTAURANT["id"])
        for ingredient, qty in [("beras", "12"), ("minyak", "3"), ("bawang", "1.5")]:
            self._execute(
                conn,
                "INSERT INTO stock (slug, ingredient_id, quantity) VALUES (%s, %s, %s) "
                "ON CONFLICT (slug, ingredient_id) DO UPDATE SET quantity=excluded.quantity",
                (RESTAURANT["id"], ingredient, qty),
            )
            self._execute(
                conn,
                "INSERT INTO movements (id, slug, purchase_id, ingredient_id, quantity, created_at) VALUES (%s, %s, NULL, %s, %s, %s)",
                (uuid.uuid4().hex, RESTAURANT["id"], ingredient, qty, now()),
            )
        self._new_draft(conn, "Telur 3 kg 150rb", "Pasar Pagi", [
            {"ingredient_id": "telur", "raw_name": "Telur", "quantity": "3", "unit": "kg", "line_total_idr": 150000}
        ], [], restaurant_id=RESTAURANT["id"])
        self._audit(conn, "demo_seeded", "Synthetic sample history and estimated opening stock loaded.", RESTAURANT["id"])

    def _audit(self, conn, event, message, restaurant_id=None):
        self._execute(
            conn,
            "INSERT INTO activity (id, slug, event, message, created_at) VALUES (%s, %s, %s, %s, %s)",
            (uuid.uuid4().hex, restaurant_id, event, message, now()),
        )

    # ---- draft plumbing -----------------------------------------------------

    def _normalize(self, items, catalog):
        return normalize_items_for_catalog(items, catalog)

    def _comparison(self, conn, item, supplier, restaurant_id, preloaded=None):
        comparison = {"status": "insufficient_history", "change_percent": None, "baseline_unit_price_idr": None, "baseline_purchase_id": None,
                      "baseline_supplier": None, "baseline_created_at": None}
        if not item.get("unit_price_idr") or item.get("warnings") or not supplier:
            return comparison
        if preloaded is not None:
            # Overview already fetched every purchase: compare in memory.
            rows = [row for row in preloaded if row.get("supplier") == supplier
                    and str(row.get("created_at")) >= (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()]
            rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        else:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            cur = self._execute(
                conn,
                "SELECT id, supplier, created_at, body FROM purchases WHERE slug=%s AND supplier=%s AND created_at>=%s ORDER BY created_at DESC, id DESC",
                (restaurant_id, supplier, cutoff),
            )
            rows = self._rows(cur)
        for row in rows:
            purchase = row["body"]
            previous = [part for part in purchase["items"] if part["ingredient_id"] == item["ingredient_id"] and part["canonical_unit"] == item["canonical_unit"]]
            if not previous:
                continue
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

    def _present(self, conn, draft, restaurant_id, preloaded_purchases=None):
        if draft["status"] == "recorded":
            return draft
        draft["total_idr"] = draft_total(draft["items"], draft["warnings"])
        for item in draft["items"]:
            item["comparison"] = self._comparison(conn, item, draft["supplier"], restaurant_id, preloaded_purchases)
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
            self._execute(
                conn,
                "INSERT INTO drafts (id, slug, version, status, body, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (draft["id"], restaurant_id, draft["version"], draft["status"], encode(draft), draft["created_at"]),
            )
        else:
            self._execute(
                conn,
                "UPDATE drafts SET version=%s, status=%s, body=%s WHERE id=%s",
                (draft["version"], draft["status"], encode(draft), draft["id"]),
            )

    def _load_draft(self, conn, draft_id, restaurant_id):
        cur = self._execute(conn, "SELECT body, slug FROM drafts WHERE id=%s", (draft_id,))
        row = cur.fetchone()
        # A wrong-tenant id is indistinguishable from a missing one.
        if not row or row[1] != restaurant_id:
            raise DomainError("not_found", "Draft not found.", 404)
        return row[0]

    def get_draft(self, draft_id, restaurant_id):
        with pg_transaction() as conn:
            return self._present(conn, self._load_draft(conn, draft_id, restaurant_id), restaurant_id)

    # ---- idempotency --------------------------------------------------------

    def _idempotent(self, key, operation, payload, action):
        fingerprint = hashlib.sha256(encode({"operation": operation, "payload": payload}).encode()).hexdigest()
        with pg_transaction() as conn:
            # Serialize the same request key before its first mutation.
            self._execute(conn, "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
            cur = self._execute(conn, "SELECT fingerprint, response FROM idempotency WHERE key=%s", (key,))
            existing = cur.fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise DomainError("idempotency_conflict", "This idempotency key was already used for a different request.")
                return existing[1]
            result = action(conn)
            self._execute(
                conn,
                "INSERT INTO idempotency (key, fingerprint, response) VALUES (%s, %s, %s)",
                (key, fingerprint, encode(result)),
            )
            self._overview_cache.clear()
            return result

    def intake(self, key, payload, extracted=None, restaurant_id=None):
        if extracted is None or not isinstance(extracted.get("items"), list):
            extracted = extract_purchase(payload["text"])
        def action(conn):
            return self._present(conn, self._new_draft(conn, payload["text"], payload.get("supplier") or extracted.get("supplier"), extracted["items"], extracted["warnings"], restaurant_id=restaurant_id), restaurant_id)
        return self._idempotent(key, "intake", payload, action)

    def intake_image(self, key, image_sha256, source_label, supplier, items, warnings, restaurant_id=None):
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

    # ---- approval / ledger --------------------------------------------------

    def _post(self, conn, draft, created_at=None, restaurant_id=None):
        self._present(conn, draft, restaurant_id)
        purchase_id = uuid.uuid4().hex
        created_at = created_at or now()
        purchase = {"id": purchase_id, "draft_id": draft["id"], "supplier": draft["supplier"],
                    "total_idr": draft["total_idr"], "items": draft["items"], "created_at": created_at,
                    "payment_status": "unknown"}
        self._execute(
            conn,
            "INSERT INTO purchases (id, slug, draft_id, supplier, created_at, body) VALUES (%s, %s, %s, %s, %s, %s)",
            (purchase_id, restaurant_id, draft["id"], draft["supplier"], created_at, encode(purchase)),
        )
        deltas = {}
        for item in draft["items"]:
            ingredient = item["ingredient_id"]
            deltas[ingredient] = deltas.get(ingredient, Decimal(0)) + Decimal(item["canonical_quantity"])
        for ingredient, quantity in sorted(deltas.items()):
            self._execute(conn, "INSERT INTO stock (slug,ingredient_id,quantity) VALUES (%s,%s,'0') ON CONFLICT DO NOTHING", (restaurant_id, ingredient))
            cur = self._execute(conn, "SELECT quantity FROM stock WHERE slug=%s AND ingredient_id=%s FOR UPDATE", (restaurant_id, ingredient))
            row = cur.fetchone()
            stock = Decimal(row[0]) if row else Decimal(0)
            if row:
                self._execute(
                    conn,
                    "UPDATE stock SET quantity=%s WHERE slug=%s AND ingredient_id=%s",
                    (decimal_string(stock + quantity), restaurant_id, ingredient),
                )
            else:
                self._execute(
                    conn,
                    "INSERT INTO stock (slug, ingredient_id, quantity) VALUES (%s, %s, %s)",
                    (restaurant_id, ingredient, decimal_string(quantity)),
                )
            self._execute(
                conn,
                "INSERT INTO movements (id, slug, purchase_id, ingredient_id, quantity, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (uuid.uuid4().hex, restaurant_id, purchase_id, ingredient, decimal_string(quantity), created_at),
            )
        draft.update({"status": "recorded", "version": draft["version"] + 1, "purchase_id": purchase_id})
        self._save_draft(conn, draft)
        self._audit(conn, "purchase_recorded", f"Purchase recorded for Rp {draft['total_idr']:,}; received stock added. Payment status unknown.", restaurant_id)
        return draft

    def decision(self, draft_id, key, payload, restaurant_id=None):
        def action(conn):
            draft = self._load_draft(conn, draft_id, restaurant_id)
            return self._decide(conn, draft, payload, restaurant_id)
        draft = self._idempotent(key, "decision:" + draft_id, payload, action)
        return draft

    @staticmethod
    def _slugify(raw_name):
        slug = re.sub(r"[^a-z0-9]+", "_", str(raw_name).casefold()).strip("_")
        return re.sub(r"_+", "_", slug)[:100]

    def _resolve_new_ingredients(self, conn, draft, restaurant_id):
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
                self._execute(
                    conn,
                    "INSERT INTO custom_ingredients (id, slug, name, unit, reorder_threshold) VALUES (%s, %s, %s, %s, '0')",
                    (slug, restaurant_id, name, unit),
                )
                self._execute(
                    conn,
                    "INSERT INTO stock (slug, ingredient_id, quantity) VALUES (%s, %s, '0') ON CONFLICT DO NOTHING",
                    (restaurant_id, slug),
                )
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

    # ---- WhatsApp -----------------------------------------------------------

    def _whatsapp_thread(self, conn, sender, channel, transport, restaurant_id=None, create=False):
        # A whole conversation is serialized, including distinct Meta event IDs.
        # Creating the deterministic row first also covers simultaneous first
        # messages without relying on a request-key lock shared by those IDs.
        if create:
            self._execute(
                conn,
                "INSERT INTO whatsapp_threads (id, slug, channel, sender, transport) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (channel, sender) DO NOTHING",
                (hashlib.sha256((channel + "\0" + sender).encode()).hexdigest()[:32], restaurant_id, channel, sender, transport),
            )
        cur = self._execute(
            conn, "SELECT * FROM whatsapp_threads WHERE channel=%s AND sender=%s" + (" FOR UPDATE" if create else ""),
            (channel, sender),
        )
        columns = [desc[0] for desc in cur.description]
        row = cur.fetchone()
        if row:
            thread = dict(zip(columns, row))
            if restaurant_id and thread.get("slug") is None:
                thread["slug"] = restaurant_id
                self._execute(conn, "UPDATE whatsapp_threads SET slug=%s WHERE id=%s", (restaurant_id, thread["id"]))
            return thread
        return {"id": hashlib.sha256((channel + "\0" + sender).encode()).hexdigest()[:32], "channel": channel,
                "sender": sender, "transport": transport, "active_draft_id": None, "active_version": None,
                "pending_followup": None, "slug": restaurant_id}

    def _whatsapp_snapshot(self, conn, thread, draft=None, result=None):
        tenant = thread.get("slug")
        active = self._load_draft(conn, thread["active_draft_id"], tenant) if thread["active_draft_id"] and tenant else None
        active_id = active["id"] if active and active["status"] in ("pending_approval", "needs_clarification") else None
        cur = self._execute(
            conn,
            "SELECT id, direction, body, created_at, transport, status, draft_id FROM whatsapp_messages "
            "WHERE thread_id=%s ORDER BY created_at DESC, id DESC LIMIT 100",
            (thread["id"],),
        )
        messages = self._rows(cur)
        messages.reverse()
        for message in messages:
            message["created_at"] = message["created_at"].isoformat() if isinstance(message["created_at"], datetime) else message["created_at"]
        response = {"thread": {"id": thread["id"], "sender": thread["sender"], "transport": thread["transport"], "active_draft_id": active_id},
                    "messages": messages}
        visible_draft = draft or (active if active_id else None)
        if visible_draft:
            response["draft"] = self._present(conn, visible_draft, tenant)
        if result:
            response["result"] = result
        return response

    def whatsapp_messages(self, sender="demo", restaurant_id=None):
        with pg_transaction() as conn:
            thread = self._whatsapp_thread(conn, sender, SIMULATOR, SIMULATOR)
            if thread.get("slug") != restaurant_id:
                raise DomainError("not_found", "No conversation for this sender.", 404)
            return self._whatsapp_snapshot(conn, thread)

    def claim_whatsapp_message(self, message_id, expected_status, claimed_status):
        with pg_transaction() as conn:
            cur = self._execute(
                conn,
                "UPDATE whatsapp_messages SET status=%s WHERE id=%s AND direction='outbound' AND status=%s",
                (claimed_status, message_id, expected_status),
            )
            return cur.rowcount == 1

    def set_whatsapp_message_status(self, message_id, status):
        with pg_transaction() as conn:
            cur = self._execute(
                conn,
                "UPDATE whatsapp_messages SET status=%s WHERE id=%s AND direction='outbound'",
                (status, message_id),
            )
            if cur.rowcount != 1:
                raise DomainError("whatsapp_message_not_found", "Outbound WhatsApp message was not found.", 404)

    def _stock_ingredient_ids(self, text, restaurant_id):
        """Catalog ids whose names appear in the message, longest name first.

        Same alias mapping as purchase extraction, extended with this
        restaurant's runtime ingredients, so a custom bahan is askable - and
        only by the restaurant that created it.
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

    def stock_facts(self, text, restaurant_id=None):
        """Read-only inventory lookup behind stock questions like 'berapa stok cabai?'.

        Facts come only from the asking restaurant's stock table and catalog -
        that scoping is the tenant guard for the whole answer, including the
        optional model rephrase. Returns {"question", "facts"} when the
        message is a stock question, else None. Read-only: no draft, no ledger.
        """
        if not is_stock_question(text) or not restaurant_id:
            return None
        catalog = self._catalog_for(restaurant_id)
        with pg_transaction() as conn:
            named = self._stock_ingredient_ids(text, restaurant_id)
            ingredients = [catalog["by_id"][i] for i in named if i in catalog["by_id"]] if named else catalog["list"]
            facts = []
            for ingredient in ingredients:
                cur = self._execute(conn, "SELECT quantity FROM stock WHERE slug=%s AND ingredient_id=%s", (restaurant_id, ingredient["id"]))
                row = cur.fetchone()
                quantity = row[0] if row else "0"
                facts.append({"ingredient_id": ingredient["id"], "name": ingredient["name"], "quantity": quantity,
                              "unit": ingredient["unit"], "reorder_threshold": ingredient["reorder_threshold"],
                              "low_stock": Decimal(quantity) < Decimal(ingredient["reorder_threshold"])})
        return {"question": str(text).strip(), "facts": facts, "restaurant_name": self.restaurant_name(restaurant_id)}

    def restaurant_name(self, restaurant_id):
        with pg_connection() as conn:
            cur = self._execute(
                conn,
                "SELECT r.name FROM restaurants r JOIN tenants t ON t.restaurant_id=r.id WHERE t.slug=%s",
                (restaurant_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def whatsapp_ingest(self, key, payload, *, transport=SIMULATOR, channel=None, outbound_status=None, restaurant_id=None, open_onboarding=False):
        channel = channel or transport
        operation_candidate = payload.get("operation_candidate")
        operation_source = payload.get("operation_source")
        extracted_photo = payload.get("extracted")
        source_label = payload.get("source_label") or "Foto nota WhatsApp"
        transcript = payload.get("transcript")
        transcript = transcript.strip()[:MAX_TRANSCRIPT_CHARS] if isinstance(transcript, str) else ""
        extracted_text = payload.get("extracted_text")
        if not isinstance(extracted_text, dict) or not isinstance(extracted_text.get("items"), list) or not isinstance(extracted_text.get("warnings"), list):
            extracted_text = None
        # A read-only stock answer the API layer built from this restaurant's
        # inventory facts; like extracted_text it stays out of the dedupe
        # fingerprint (the deterministic reply is identical, a rephrase is not).
        stock_answer = payload.get("stock_answer")
        if not isinstance(stock_answer, dict) or not isinstance(stock_answer.get("body"), str) or not stock_answer["body"].strip():
            stock_answer = None
        payload = {"sender": payload["sender"], "text": payload["text"], "event_id": payload.get("event_id"), "num_media": payload.get("num_media", 0)}
        # Provenance bytes/media identity are stable across provider retries;
        # model-derived candidates and transcripts deliberately are not.
        fingerprint_source = {}
        if isinstance(operation_source, dict):
            identity_fields = ("media_id", "media_kind", "caption") if operation_source.get("media_id") else ("digest", "media_kind", "caption")
            identity = {name: operation_source[name] for name in identity_fields if operation_source.get(name) is not None}
            if identity:
                fingerprint_source = identity
        event_id = payload["event_id"] or "sim:" + key
        request_identity = dict(payload)
        if fingerprint_source.get("media_kind") == "image" and fingerprint_source.get("media_id"):
            # Older normalizers omitted image captions. Keep their request-key
            # identity while retaining the caption in the stronger event hash.
            request_identity["text"] = ""
            fingerprint_source.setdefault("caption", payload["text"])
        fingerprint_body = {"sender": request_identity["sender"], "text": request_identity["text"], "num_media": request_identity["num_media"]}
        # Preserve request-key compatibility with already stored WhatsApp
        # callbacks; media identity tightens the provider-event comparison.
        legacy_fingerprint = hashlib.sha256(encode(fingerprint_body).encode()).hexdigest()
        if fingerprint_source:
            fingerprint_body["operation_source"] = fingerprint_source
        fingerprint = hashlib.sha256(encode(fingerprint_body).encode()).hexdigest()

        def action(conn):
            cur = self._execute(conn, "SELECT fingerprint, response FROM whatsapp_events WHERE channel=%s AND event_id=%s", (channel, event_id))
            prior = cur.fetchone()
            if prior:
                if prior[0] not in (fingerprint, legacy_fingerprint):
                    raise DomainError("event_conflict", "This message event was already used for different content or sender.")
                return prior[1]
            cur = self._execute(conn, "SELECT slug FROM whatsapp_links WHERE phone=%s", (payload["sender"],))
            link = cur.fetchone()
            linked = link[0] if link else None
            onboarding_reply = None
            if linked is None and open_onboarding:
                rows = self._rows(self._execute(conn,
                    "SELECT t.slug AS id, r.name FROM tenants t JOIN restaurants r ON r.id=t.restaurant_id ORDER BY r.name, t.slug"))
                choices = [row for row in rows if restaurant_id is None or row["id"] == restaurant_id]
                selected, onboarding_reply = restaurant_selection(choices, payload["text"])
                if selected:
                    phone = validate_phone_number(payload["sender"])
                    self._execute(conn,
                        "INSERT INTO whatsapp_links (id, slug, phone, label) VALUES (%s, %s, %s, %s) ON CONFLICT (phone) DO NOTHING",
                        (uuid.uuid4().hex, selected["id"], phone, "WhatsApp self-registration"))
                    linked = self._execute(conn, "SELECT slug FROM whatsapp_links WHERE phone=%s", (phone,)).fetchone()[0]
                    if linked != selected["id"]:
                        raise DomainError("sender_link_changed", "This number was linked elsewhere. Please send your message again.", 409)
            if restaurant_id is not None and linked != restaurant_id and not (linked is None and open_onboarding):
                raise DomainError("sender_not_registered", "This WhatsApp number is not linked to your restaurant.", 403)
            tenant = linked
            thread = self._whatsapp_thread(conn, payload["sender"], channel, transport, restaurant_id=tenant, create=True)
            # Another key may have replayed this same provider event while we
            # waited on the thread lock. Re-read before touching any draft.
            prior = self._execute(conn, "SELECT fingerprint, response FROM whatsapp_events WHERE channel=%s AND event_id=%s", (channel, event_id)).fetchone()
            if prior:
                if prior[0] not in (fingerprint, legacy_fingerprint):
                    raise DomainError("event_conflict", "This message event was already used for different content or sender.")
                return prior[1]
            names = self.catalog_names(tenant) if tenant else None
            draft = self._load_draft(conn, thread["active_draft_id"], tenant) if thread["active_draft_id"] and tenant else None
            if draft and draft["status"] not in ("pending_approval", "needs_clarification"):
                draft = None
                thread.update(active_draft_id=None, active_version=None)
            pending_purchase = thread.get("pending_followup")
            operation_result = None
            if tenant and onboarding_reply is None:
                operation_result = self._operation_handle(
                    conn, thread, tenant, transcript or payload["text"],
                    candidate=operation_candidate, source=operation_source,
                    purchase_draft=draft,
                )
            # Inventory conversations must not consume purchase payment state.
            if operation_result is None:
                thread["pending_followup"] = None
            command = payload["text"].strip().casefold()
            result = {}
            if onboarding_reply is not None:
                reply = onboarding_reply
                result["action"] = "restaurant_selected" if tenant else "restaurant_selection_required"
            elif tenant is None:
                reply = UNREGISTERED_REPLY
                result["action"] = "unregistered_sender"
            elif operation_result is not None:
                reply = operation_result["reply"]
                result = operation_result["result"]
            elif extracted_photo:
                source_digest, source_result = self._purchase_source_guard(conn, tenant, operation_source)
                if source_result is not None:
                    reply, result = source_result["reply"], source_result["result"]
                    thread["pending_followup"] = pending_purchase
                elif draft:
                    reply = "Foto diterima. Masih ada draf yang menunggu konfirmasimu dulu, jadi foto ini belum dicatat:\n" + draft_reply(draft, names)
                    result["action"] = "active_draft_exists"
                else:
                    draft = self._new_draft(conn, source_label, extracted_photo.get("supplier"), extracted_photo.get("items") or [], extracted_photo.get("warnings") or [], restaurant_id=tenant)
                    self._purchase_source_register(conn, tenant, source_digest, draft["id"])
                    thread.update(active_draft_id=draft["id"], active_version=draft["version"])
                    reply = "Nota diterima. Cek draf pembelian ini:\n" + draft_reply(draft, names)
                    result["action"] = "draft_created" if draft["status"] == "pending_approval" else "review_required"
            elif transcript:
                if draft:
                    reply = "Pesan suara diterima. Masih ada draf yang menunggu konfirmasimu dulu, jadi pesan ini belum dicatat:\n" + draft_reply(draft, names)
                    result["action"] = "active_draft_exists"
                else:
                    extracted = extracted_text if extracted_text is not None else extract_purchase(transcript)
                    draft = self._new_draft(conn, transcript, extracted.get("supplier"), extracted["items"], extracted["warnings"], restaurant_id=tenant)
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
                summary = self._weekly_summary(conn, SUMMARY_WINDOW_DAYS, tenant)
                reply = weekly_summary_reply(summary, SUMMARY_WINDOW_DAYS)
                result["action"] = "weekly_summary"
                result["summary"] = summary
            elif draft and (request := parse_remove_request(payload["text"])):
                # 'hapus 4', 'hapus 4, 5' and 'hapus semua' drop flagged
                # (non-kitchen) lines from the active draft - the owner's
                # confirmation for the kitchen filter.
                items = draft["items"]
                flagged = [position for position, item in enumerate(items) if item.get("non_kitchen")]
                hint = ", ".join(str(position + 1) for position in flagged) or "tidak ada"
                if request["mode"] == "all":
                    targets = flagged
                else:
                    targets = [index - 1 for index in dict.fromkeys(request["indices"])]
                unknown = [position for position in targets if not 0 <= position < len(items)]
                unflagged = [position for position in targets if position not in flagged]
                if unknown:
                    reply = (f"Item {', '.join(str(position + 1) for position in unknown)} tidak ada di draf "
                             f"({len(items)} baris). Hapus hanya baris bertanda: {hint}.")
                    result["action"] = "remove_rejected"
                elif unflagged:
                    reply = (f"Item {', '.join(str(position + 1) for position in unflagged)} tidak bertanda "
                             f"bukan bahan masak. Hapus hanya baris bertanda: {hint}.")
                    result["action"] = "remove_rejected"
                elif not targets:
                    reply = "Tidak ada baris yang bertanda bukan bahan masak."
                    result["action"] = "remove_rejected"
                elif len(targets) >= len(items):
                    reply = "Kalau semua baris dibuang, draf jadi kosong. Balas BATAL untuk membatalkan seluruh draf."
                    result["action"] = "remove_rejected"
                else:
                    removed_names = [str(items[position].get("raw_name")) for position in targets]
                    for position in sorted(targets, reverse=True):
                        items.pop(position)
                    # Re-derive warnings for the remaining items: numbered
                    # warnings ('Item 2: ...') shift when lines disappear.
                    remaining, field_warnings = self._normalize(items, self._catalog_for(tenant))
                    flags = [item.get("non_kitchen") for item in items]
                    for item, still_flagged in zip(remaining, flags):
                        if still_flagged:
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
                    quoted = [f"'{name}'" for name in removed_names]
                    names_text = " dan ".join([", ".join(quoted[:-1]), quoted[-1]]) if len(quoted) > 1 else quoted[0]
                    self._audit(conn, "draft_item_removed", f"Non-kitchen item(s) {names_text} removed from draft {draft['id'][:8]}.", tenant)
                    reply = f"Item {names_text} dihapus dari draf.\n\n" + draft_reply(draft, names)
                    result["action"] = "item_removed"
            elif stock_answer:
                # A stock question answered from inventory facts before the
                # ingest; read-only like the summary, so an active draft stays
                # untouched and no purchase line is filed.
                reply = stock_answer["body"]
                result["action"] = "stock_answer"
            elif draft:
                reply = draft_reply(draft, names)
                result["action"] = "active_draft_exists"
            else:
                extracted = extracted_text if extracted_text is not None else extract_purchase(payload["text"])
                if all(item.get("ingredient_id") is None and item.get("quantity") is None and item.get("line_total_idr") is None for item in extracted["items"]):
                    reply = HELP_REPLY
                    result["action"] = "help_reply"
                else:
                    draft = self._new_draft(conn, payload["text"], extracted.get("supplier"), extracted["items"], extracted["warnings"], restaurant_id=tenant)
                    thread.update(active_draft_id=draft["id"], active_version=draft["version"])
                    reply = draft_reply(draft, names)
                    result["action"] = "draft_created" if draft["status"] == "pending_approval" else "review_required"
            self._execute(
                conn,
                "UPDATE whatsapp_threads SET active_draft_id=%s, active_version=%s, pending_followup=%s WHERE id=%s",
                (thread["active_draft_id"], thread["active_version"], thread.get("pending_followup"), thread["id"]),
            )
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
                self._execute(
                    conn,
                    "INSERT INTO whatsapp_messages (id, thread_id, direction, body, created_at, transport, status, draft_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (uuid.uuid4().hex, thread["id"], direction, body, now(), transport, status, draft_id),
                )
            if result["action"] == "unregistered_sender":
                self._audit(conn, "whatsapp_unregistered", f"Rejected a message from unregistered WhatsApp sender {payload['sender']}.")
            else:
                self._audit(conn, "whatsapp_message_processed", f"{transport} message processed: {result['action']}.", tenant)
            response = self._whatsapp_snapshot(conn, thread, draft=draft, result=result)
            self._execute(
                conn,
                "INSERT INTO whatsapp_events (channel, event_id, thread_id, fingerprint, response) VALUES (%s, %s, %s, %s, %s)",
                (channel, event_id, thread["id"], fingerprint, encode(response)),
            )
            return response

        return self._idempotent(key, "whatsapp:" + channel, request_identity, action)

    # ---- reports and exports ------------------------------------------------

    def _weekly_summary(self, conn, days=SUMMARY_WINDOW_DAYS, restaurant_id=None):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        catalog_by_id = self._catalog_for(restaurant_id)["by_id"]
        summary = {"purchase_count": 0, "total_spend_idr": 0, "top_item": None, "biggest_price_spike": None}
        quantities = {}
        cur = self._execute(
            conn,
            "SELECT body FROM purchases WHERE slug=%s AND created_at>=%s ORDER BY created_at, id",
            (restaurant_id, cutoff),
        )
        for (body,) in cur.fetchall():
            purchase = body
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
        with pg_transaction() as conn:
            return self._weekly_summary(conn, days, restaurant_id)

    def export_purchases(self, restaurant_id=None):
        with pg_connection() as conn:
            cur = self._execute(
                conn,
                "SELECT id, supplier, created_at, body FROM purchases WHERE slug=%s ORDER BY created_at DESC, id DESC",
                (restaurant_id,),
            )
            rows = []
            for row in self._rows(cur):
                purchase = row["body"]
                total = purchase.get("total_idr")
                rows.append({
                    "id": purchase.get("id") or row["id"],
                    "created_at": row["created_at"].isoformat() if isinstance(row["created_at"], datetime) else row["created_at"],
                    "supplier": row["supplier"] or "",
                    "item_count": len(purchase.get("items") or []),
                    "total_idr": total if isinstance(total, int) and not isinstance(total, bool) else "",
                    "status": purchase.get("payment_status") or "",
                })
            return rows

    def export_stock(self, restaurant_id=None):
        catalog = self._catalog_for(restaurant_id)
        with pg_connection() as conn:
            last_prices = {}
            cur = self._execute(
                conn,
                "SELECT body FROM purchases WHERE slug=%s ORDER BY created_at DESC, id DESC",
                (restaurant_id,),
            )
            for (body,) in cur.fetchall():
                for item in body.get("items") or []:
                    ingredient_id = item.get("ingredient_id")
                    price = item.get("unit_price_idr")
                    if ingredient_id and price and ingredient_id not in last_prices:
                        last_prices[ingredient_id] = price
            rows = []
            for ingredient in catalog["list"]:
                cur = self._execute(conn, "SELECT quantity FROM stock WHERE slug=%s AND ingredient_id=%s", (restaurant_id, ingredient["id"]))
                row = cur.fetchone()
                quantity = row[0] if row else "0"
                rows.append({"item": ingredient["name"], "unit": ingredient["unit"], "current_stock": quantity, "last_unit_price": last_prices.get(ingredient["id"])})
            return rows

    def _canonical_supplier(self, conn, supplier, exclude_id=None, restaurant_id=None):
        def norm(value):
            return " ".join(str(value).casefold().split())

        candidate = norm(supplier)
        if not candidate:
            return supplier, True
        cur = self._execute(
            conn,
            "SELECT DISTINCT supplier FROM purchases WHERE slug=%s AND supplier IS NOT NULL AND supplier != '' AND id != %s",
            (restaurant_id, exclude_id or ""),
        )
        stored = {row[0]: norm(row[0]) for row in cur.fetchall()}
        for name, normed in stored.items():
            if normed == candidate:
                return name, True
        for name, normed in sorted(stored.items(), key=lambda item: len(item[1])):
            if (len(candidate) >= 3 and candidate in normed) or (len(normed) >= 3 and normed in candidate):
                return name, True
        return supplier, False

    def _update_purchase_payment(self, conn, purchase_id, payment_status, supplier, restaurant_id=None):
        cur = self._execute(conn, "SELECT body, slug FROM purchases WHERE id=%s", (purchase_id,))
        row = cur.fetchone()
        if not row or row[1] != restaurant_id:
            raise DomainError("not_found", "Purchase not found.", 404)
        purchase = row[0]
        if payment_status:
            purchase["payment_status"] = payment_status
        supplier_is_new = None
        if supplier:
            canonical, matched_existing = self._canonical_supplier(conn, supplier, exclude_id=purchase_id, restaurant_id=restaurant_id)
            purchase["supplier"] = canonical
            supplier_is_new = not matched_existing
        self._execute(
            conn,
            "UPDATE purchases SET body=%s, supplier=%s WHERE id=%s",
            (encode(purchase), purchase.get("supplier"), purchase_id),
        )
        self._audit(conn, "purchase_updated", f"Purchase {purchase_id[:8]} payment details completed via WhatsApp follow-up.", restaurant_id)
        return purchase, supplier_is_new

    def update_purchase_payment(self, purchase_id, restaurant_id=None, payment_status=None, supplier=None):
        with pg_transaction() as conn:
            purchase, _ = self._update_purchase_payment(conn, purchase_id, payment_status, supplier, restaurant_id)
            return purchase

    @staticmethod
    def _as_datetime(value):
        """timestamptz arrives as datetime from jsonb_agg; tolerate ISO strings."""
        if isinstance(value, datetime):
            return value
        text_value = str(value)
        # Python 3.10 fromisoformat rejects 7+ fractional digits: keep the
        # fraction to 6 digits, preserving the timezone offset after it.
        if "." in text_value:
            head, rest = text_value.split(".", 1)
            digits = ""
            for index, char in enumerate(rest):
                if char.isdigit():
                    digits += char
                else:
                    break
            # 3.10 accepts exactly 3 or 6 fractional digits; pad/truncate to 6.
            text_value = f"{head}.{digits.ljust(6, '0')[:6]}{rest[index:]}"
        return datetime.fromisoformat(text_value)

    def overview(self, restaurant_id=None):
        """The full dashboard payload in ONE round trip, with a 3s cache.

        Every sub-read (restaurant, drafts, purchases, stock, WhatsApp
        sources, numbers, activity) is pulled as JSON in a single query; the
        purchase bodies also seed the price comparisons, so projecting drafts
        needs no further queries. The dashboard polls this every 5 seconds.
        """
        now_ts = time.time()
        cached = self._overview_cache.get(restaurant_id)
        if cached and now_ts - cached[0] < self._overview_cache_ttl:
            return cached[1]

        catalog = self._catalog_for(restaurant_id)
        with pg_transaction() as conn:
            cur = self._execute(conn, (
                "SELECT "
                "(SELECT jsonb_build_object('id', r.id, 'name', r.name, 'address', r.address) "
                " FROM restaurants r JOIN tenants t ON t.restaurant_id=r.id WHERE t.slug=%s), "
                "(SELECT COALESCE(jsonb_agg(body ORDER BY created_at DESC, id), '[]'::jsonb) "
                " FROM drafts WHERE slug=%s), "
                "(SELECT COALESCE(jsonb_agg(jsonb_build_object('id', p.id, 'supplier', p.supplier, 'created_at', p.created_at, 'body', p.body) "
                " ORDER BY p.created_at DESC, p.id), '[]'::jsonb) "
                " FROM purchases p WHERE p.slug=%s), "
                "(SELECT COALESCE(jsonb_object_agg(ingredient_id, quantity), '{}'::jsonb) "
                " FROM stock WHERE slug=%s), "
                "(SELECT COALESCE(jsonb_agg(jsonb_build_object('phone', phone, 'label', label, 'created_at', created_at) "
                " ORDER BY created_at, id), '[]'::jsonb) "
                " FROM whatsapp_links WHERE slug=%s), "
                "(SELECT COALESCE(jsonb_agg(jsonb_build_object('draft_id', w.draft_id, 'transport', w.transport, 'created_at', w.created_at) "
                " ORDER BY w.created_at, w.id), '[]'::jsonb) "
                " FROM whatsapp_messages w JOIN whatsapp_threads t ON t.id=w.thread_id "
                " WHERE w.direction='inbound' AND w.draft_id IS NOT NULL AND t.slug=%s), "
                "(SELECT COALESCE(jsonb_agg(jsonb_build_object('id', a.id, 'restaurant_id', a.slug, 'event', a.event, "
                "'message', a.message, 'created_at', a.created_at) ORDER BY a.created_at DESC, a.id), '[]'::jsonb) "
                " FROM (SELECT * FROM activity WHERE slug=%s ORDER BY created_at DESC, id DESC LIMIT 100) a)"
            ), (restaurant_id,) * 7)
            row = cur.fetchone()

        restaurant, drafts_bodies, purchase_rows, stock_map, numbers, sources, activity = row
        if restaurant is None:
            raise DomainError("not_found", "Restaurant not found.", 404)

        # Price comparisons run in memory against the already-fetched purchases.
        preloaded = [{**purchase, "created_at": purchase["created_at"].isoformat() if isinstance(purchase["created_at"], datetime) else purchase["created_at"]}
                     for purchase in purchase_rows]
        purchases = [purchase["body"] for purchase in purchase_rows]
        drafts = [self._present(conn, body, restaurant_id, preloaded) for body in drafts_bodies]

        inventory = []
        for ingredient in catalog["list"]:
            quantity = stock_map.get(ingredient["id"], "0")
            inventory.append({"ingredient_id": ingredient["id"], "name": ingredient["name"], "quantity": quantity,
                              "unit": ingredient["unit"], "reorder_threshold": ingredient["reorder_threshold"],
                              "low_stock": Decimal(quantity) < Decimal(ingredient["reorder_threshold"])})
        today = datetime.now(RESTAURANT_TIMEZONE).date()
        sources_by_draft = {}
        for source in sources:
            created = source["created_at"].isoformat() if isinstance(source["created_at"], datetime) else source["created_at"]
            sources_by_draft.setdefault(source["draft_id"], {"kind": "whatsapp" if source["transport"] == LIVE else "simulator", "created_at": created})
        for draft in drafts:
            if draft["id"] in sources_by_draft:
                draft["source"] = sources_by_draft[draft["id"]]
        purchase_days = [self._as_datetime(purchase["created_at"]).astimezone(RESTAURANT_TIMEZONE).date() for purchase in purchase_rows]

        activity = list(activity)
        for entry in activity:
            if isinstance(entry["created_at"], datetime):
                entry["created_at"] = entry["created_at"].isoformat()
        numbers = list(numbers)
        for number in numbers:
            if isinstance(number["created_at"], datetime):
                number["created_at"] = number["created_at"].isoformat()

        payload = {
            "restaurant": dict(restaurant),
            "whatsapp_numbers": numbers,
            "mode": {"transport": "manual", "extraction": "rules", "storage": "supabase", "identity": "session"},
            "metrics": {"purchases_today_idr": sum(purchase["body"].get("total_idr") or 0 for purchase in purchase_rows if isinstance(purchase["body"].get("total_idr"), int) and self._as_datetime(purchase["created_at"]).astimezone(RESTAURANT_TIMEZONE).date() == today),
                        "purchases_yesterday_idr": sum(purchase["body"].get("total_idr") or 0 for purchase in purchase_rows if isinstance(purchase["body"].get("total_idr"), int) and self._as_datetime(purchase["created_at"]).astimezone(RESTAURANT_TIMEZONE).date() == today - timedelta(days=1)),
                        "pending_approvals": sum(entry["status"] in ("pending_approval", "needs_clarification") for entry in drafts),
                        "low_stock_count": sum(entry["low_stock"] for entry in inventory), "recorded_purchases": len(purchases)},
            "drafts": drafts, "purchases": purchases, "inventory": inventory,
            "activity": activity,
            "ingredients": catalog["list"],
        }
        self._overview_cache[restaurant_id] = (now_ts, payload)
        return payload

    def verify_login(self, username, password):
        with pg_connection() as conn:
            cur = self._execute(
                conn,
                "SELECT u.id, u.username, u.display_name, u.role, u.slug, u.password_hash, r.name "
                "FROM app_users u LEFT JOIN tenants t ON t.slug=u.slug LEFT JOIN restaurants r ON r.id=t.restaurant_id "
                "WHERE username=%s",
                (str(username or "").strip(),),
            )
            row = cur.fetchone()
        if not row or not verify_password(str(password or ""), row[5]):
            raise DomainError("invalid_credentials", "Unknown username or wrong password.", 401)
        return {"id": row[0], "username": row[1], "display_name": row[2],
                "role": row[3], "restaurant_id": row[4], "restaurant_name": row[6]}

    def create_session(self, user_id):
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + SESSION_TTL
        with pg_transaction() as conn:
            self._execute(conn, "DELETE FROM app_sessions WHERE expires_at <= now()", ())
            self._execute(
                conn,
                "INSERT INTO app_sessions (token, user_id, created_at, expires_at) VALUES (%s, %s, now(), %s)",
                (token, user_id, expires),
            )
        return token

    def session_user(self, token):
        if not token:
            return None
        with pg_transaction() as conn:
            cur = self._execute(
                conn,
                "SELECT s.expires_at, u.id, u.username, u.display_name, u.role, u.slug, r.name "
                "FROM app_sessions s JOIN app_users u ON u.id=s.user_id "
                "LEFT JOIN tenants t ON t.slug=u.slug LEFT JOIN restaurants r ON r.id=t.restaurant_id "
                "WHERE s.token=%s",
                (token,),
            )
            row = cur.fetchone()
            if not row:
                return None
            expires_at = row[0]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at <= datetime.now(timezone.utc):
                self._execute(conn, "DELETE FROM app_sessions WHERE token=%s", (token,))
                return None
            return {"id": row[1], "username": row[2], "display_name": row[3],
                    "role": row[4], "restaurant_id": row[5], "restaurant_name": row[6]}

    def delete_session(self, token):
        with pg_transaction() as conn:
            self._execute(conn, "DELETE FROM app_sessions WHERE token=%s", (token,))

    # ---- WhatsApp allowlist -------------------------------------------------

    def restaurant_for_sender(self, phone):
        with pg_connection() as conn:
            cur = self._execute(
                conn,
                "SELECT l.slug, r.name FROM whatsapp_links l JOIN tenants t ON t.slug=l.slug "
                "JOIN restaurants r ON r.id=t.restaurant_id WHERE l.phone=%s",
                (str(phone or "").strip(),),
            )
            row = cur.fetchone()
        return {"id": row[0], "name": row[1]} if row else None

    @staticmethod
    def _linked_numbers_conn(conn, restaurant_id):
        """linked_numbers on an existing connection (for inside transactions)."""
        rows = PostgresStore._rows(PostgresStore._execute(
            conn,
            "SELECT phone, label, created_at FROM whatsapp_links WHERE slug=%s ORDER BY created_at, id",
            (restaurant_id,),
        ))
        for row in rows:
            if isinstance(row["created_at"], datetime):
                row["created_at"] = row["created_at"].isoformat()
        return rows

    def linked_numbers(self, restaurant_id):
        with pg_connection() as conn:
            cur = self._execute(
                conn,
                "SELECT phone, label, created_at FROM whatsapp_links WHERE slug=%s ORDER BY created_at, id",
                (restaurant_id,),
            )
            rows = self._rows(cur)
        for row in rows:
            if isinstance(row["created_at"], datetime):
                row["created_at"] = row["created_at"].isoformat()
        return rows

    # ---- admin console ------------------------------------------------------

    def _restaurant_detail(self, conn, restaurant_id):
        cur = self._execute(
            conn,
            "SELECT t.slug AS id, r.name, r.address, t.created_at FROM tenants t "
            "JOIN restaurants r ON r.id=t.restaurant_id WHERE t.slug=%s",
            (restaurant_id,),
        )
        columns = [desc[0] for desc in cur.description]
        row = cur.fetchone()
        if not row:
            raise DomainError("not_found", "Restaurant not found.", 404)
        detail = dict(zip(columns, row))
        if isinstance(detail["created_at"], datetime):
            detail["created_at"] = detail["created_at"].isoformat()
        cur = self._execute(
            conn,
            "SELECT phone, label, created_at FROM whatsapp_links WHERE slug=%s ORDER BY created_at, id",
            (restaurant_id,),
        )
        numbers = self._rows(cur)
        for number in numbers:
            if isinstance(number["created_at"], datetime):
                number["created_at"] = number["created_at"].isoformat()
        detail["whatsapp_numbers"] = numbers
        cur = self._execute(
            conn,
            "SELECT username, display_name, role, created_at FROM app_users WHERE slug=%s ORDER BY created_at, id",
            (restaurant_id,),
        )
        users = self._rows(cur)
        for user in users:
            if isinstance(user["created_at"], datetime):
                user["created_at"] = user["created_at"].isoformat()
        detail["users"] = users
        cur = self._execute(
            conn,
            "SELECT COUNT(*) FROM drafts WHERE slug=%s AND status IN ('pending_approval','needs_clarification')",
            (restaurant_id,),
        )
        detail["pending_drafts"] = cur.fetchone()[0]
        cur = self._execute(conn, "SELECT COUNT(*) FROM purchases WHERE slug=%s", (restaurant_id,))
        detail["recorded_purchases"] = cur.fetchone()[0]
        return detail

    def create_restaurant(self, key, payload):
        name = str(payload.get("name") or "").strip()
        if not name:
            raise DomainError("invalid_request", "A restaurant name is required.", 422)
        name, address = name[:200], (str(payload.get("address") or "").strip() or None)
        username = validate_username(payload.get("owner_username"))
        password = validate_password(payload.get("owner_password"))
        phone = validate_phone_number(payload["whatsapp_number"]) if str(payload.get("whatsapp_number") or "").strip() else None
        label = (str(payload.get("number_label") or "").strip() or None)

        def action(conn):
            cur = self._execute(conn, "SELECT 1 FROM app_users WHERE username=%s", (username,))
            if cur.fetchone():
                raise DomainError("username_taken", "That owner username is already in use.", 409)
            if phone:
                cur = self._execute(conn, "SELECT 1 FROM whatsapp_links WHERE phone=%s", (phone,))
                if cur.fetchone():
                    raise DomainError("phone_already_linked", "That WhatsApp number is already linked to a restaurant.", 409)
            slug = uuid.uuid4().hex[:12]
            cloud_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"restopilot:{slug}"))
            self._execute(
                conn,
                "INSERT INTO restaurants (id, name, phone_number, address, created_at) VALUES (%s, %s, %s, %s, now())",
                (cloud_id, name, f"demo+{slug}", address),
            )
            self._execute(conn, "INSERT INTO tenants (slug, restaurant_id) VALUES (%s, %s)", (slug, cloud_id))
            self._execute(
                conn,
                "INSERT INTO app_users (id, username, password_hash, display_name, role, slug) VALUES (%s, %s, %s, %s, 'owner', %s)",
                (uuid.uuid4().hex, username, hash_password(password), f"Pemilik {name}"[:80], slug),
            )
            if phone:
                self._execute(
                    conn,
                    "INSERT INTO whatsapp_links (id, slug, phone, label) VALUES (%s, %s, %s, %s)",
                    (uuid.uuid4().hex, slug, phone, label),
                )
            self._audit(conn, "restaurant_created", f"Restaurant '{name}' created with owner login '{username}'.", slug)
            return self._restaurant_detail(conn, slug)

        stored = {"name": name, "address": address, "owner_username": username, "whatsapp_number": phone, "number_label": label}
        return self._idempotent(key, "admin:create_restaurant", stored, action)

    def list_restaurants(self):
        with pg_transaction() as conn:
            cur = self._execute(conn, "SELECT slug FROM tenants ORDER BY created_at, slug")
            return [self._restaurant_detail(conn, row[0]) for row in cur.fetchall()]

    def restaurant_detail(self, restaurant_id):
        with pg_transaction() as conn:
            return self._restaurant_detail(conn, restaurant_id)

    def update_restaurant(self, restaurant_id, key, payload):
        name = str(payload.get("name") or "").strip()
        address = str(payload.get("address") or "").strip()

        def action(conn):
            cur = self._execute(
                conn,
                "SELECT r.name, r.address FROM tenants t JOIN restaurants r ON r.id=t.restaurant_id WHERE t.slug=%s",
                (restaurant_id,),
            )
            current = cur.fetchone()
            if not current:
                raise DomainError("not_found", "Restaurant not found.", 404)
            updated_name = name[:200] or current[0]
            updated_address = address[:200] or current[1]
            self._execute(
                conn,
                "UPDATE restaurants SET name=%s, address=%s WHERE id=(SELECT restaurant_id FROM tenants WHERE slug=%s)",
                (updated_name, updated_address, restaurant_id),
            )
            self._audit(conn, "restaurant_updated", f"Restaurant details updated: now '{updated_name}'.", restaurant_id)
            return self._restaurant_detail(conn, restaurant_id)

        return self._idempotent(key, f"admin:update_restaurant:{restaurant_id}", {"name": name, "address": address}, action)

    def add_whatsapp_number(self, restaurant_id, key, payload):
        phone = validate_phone_number(payload.get("whatsapp_number"))
        label = str(payload.get("label") or "").strip()[:100] or None

        def action(conn):
            cur = self._execute(conn, "SELECT 1 FROM tenants WHERE slug=%s", (restaurant_id,))
            if not cur.fetchone():
                raise DomainError("not_found", "Restaurant not found.", 404)
            cur = self._execute(conn, "SELECT slug FROM whatsapp_links WHERE phone=%s", (phone,))
            existing = cur.fetchone()
            if existing:
                if existing[0] == restaurant_id:
                    raise DomainError("phone_already_linked", "That WhatsApp number is already linked to this restaurant.", 409)
                raise DomainError("phone_already_linked", "That WhatsApp number is already linked to another restaurant.", 409)
            self._execute(
                conn,
                "INSERT INTO whatsapp_links (id, slug, phone, label) VALUES (%s, %s, %s, %s)",
                (uuid.uuid4().hex, restaurant_id, phone, label),
            )
            self._audit(conn, "whatsapp_number_linked", f"WhatsApp number {phone} linked to this restaurant.", restaurant_id)
            return self._restaurant_detail(conn, restaurant_id)

        return self._idempotent(key, f"admin:add_number:{restaurant_id}", {"whatsapp_number": phone, "label": label}, action)

    def remove_whatsapp_number(self, restaurant_id, phone, key):
        phone = validate_phone_number(phone)

        def action(conn):
            cur = self._execute(conn, "DELETE FROM whatsapp_links WHERE slug=%s AND phone=%s", (restaurant_id, phone))
            if cur.rowcount != 1:
                raise DomainError("not_found", "That WhatsApp number is not linked to this restaurant.", 404)
            self._audit(conn, "whatsapp_number_unlinked", f"WhatsApp number {phone} unlinked; its messages are no longer processed.", restaurant_id)
            return self._restaurant_detail(conn, restaurant_id)

        return self._idempotent(key, f"admin:remove_number:{restaurant_id}:{phone}", {}, action)

    def reset_owner_password(self, restaurant_id, key, payload):
        username = validate_username(payload.get("username"))
        password = validate_password(payload.get("new_password"))

        def action(conn):
            cur = self._execute(
                conn,
                "SELECT id FROM app_users WHERE username=%s AND slug=%s AND role='owner'",
                (username, restaurant_id),
            )
            row = cur.fetchone()
            if not row:
                raise DomainError("not_found", "No owner with that username belongs to this restaurant.", 404)
            self._execute(conn, "UPDATE app_users SET password_hash=%s WHERE id=%s", (hash_password(password), row[0]))
            self._execute(conn, "DELETE FROM app_sessions WHERE user_id=%s", (row[0],))
            self._audit(conn, "owner_password_reset", f"Owner login '{username}' password reset; its sessions were signed out.", restaurant_id)
            return self._restaurant_detail(conn, restaurant_id)

        return self._idempotent(key, f"admin:reset_password:{restaurant_id}:{username}", {}, action)

    def recent_rejections(self, limit=20):
        with pg_connection() as conn:
            cur = self._execute(
                conn,
                "SELECT message, created_at FROM activity WHERE event='whatsapp_unregistered' AND slug IS NULL "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (limit,),
            )
            rows = self._rows(cur)
        for row in rows:
            if isinstance(row["created_at"], datetime):
                row["created_at"] = row["created_at"].isoformat()
        return rows
