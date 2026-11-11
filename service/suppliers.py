"""Supplier contacts, price quotes, and the price-comparison agent.

Suppliers are per-restaurant entities with an optional WhatsApp/Telegram
contact. Every recorded purchase is linked to a supplier entity (auto-
created from the canonical name), so "terakhir beli ayam di mana dan
berapa?" is answerable from the ledger. Price quotes extend price
intelligence beyond purchases: market prices recorded from supplier
replies or manual entries, without buying anything.

SQL runs through ``self._inventory_query`` so ``?`` placeholders work on
SQLite and Postgres alike. Contact phones are disjoint from owner-linked
numbers in both directions.
"""

import json
import re
import uuid
from decimal import Decimal, InvalidOperation

from .extraction import catalog_aliases

_MONEY = re.compile(r"(?i)(?:rp\.?\s*)?(\d{1,9}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s*(rb|ribu|jt|juta|k)?")
_PHONE = re.compile(r"[1-9]\d{6,14}")
_PLATFORMS = ("whatsapp", "telegram")


def _money_idr(value):
    """Parse '48rb', 'Rp50.000', '1,2jt' into whole rupiah, or None."""
    match = _MONEY.search(str(value or ""))
    if not match:
        return None
    digits, suffix = match.group(1), (match.group(2) or "").casefold()
    number = digits.replace(".", "").replace(",", ".")
    try:
        amount = Decimal(number)
    except InvalidOperation:
        return None
    if suffix in ("rb", "ribu", "k"):
        amount *= 1000
    elif suffix in ("jt", "juta"):
        amount *= 1000000
    return int(amount)


def parse_supplier_command(text):
    """Owner supplier commands: daftar / tambah / catat harga / tanya harga /
    harga per bahan. Returns None when the message is not a supplier command."""
    text = str(text or "").strip()
    if len(text) > 300 or not re.search(r"(?i)\b(supplier|pemasok|catat\s+harga)\b", text):
        return None
    lowered = text.casefold()
    if re.search(r"(?i)\b(daftar|list)\s+(supplier|pemasok)", lowered):
        return {"kind": "list"}
    if re.search(r"(?i)\btambah\s+(supplier|pemasok)\b", lowered):
        return {"kind": "add", "text": text}
    if re.search(r"(?i)\btanya\s+harga\b", lowered):
        return {"kind": "inquiry", "text": text}
    if re.search(r"(?i)\bcatat\s+harga\b", lowered):
        return {"kind": "quote", "text": text}
    if re.search(r"(?i)\bsupplier\b", lowered) and re.search(r"(?i)\b(harga|berapa|murah|termurah|banding)\b", lowered):
        return {"kind": "compare", "text": text}
    return None


def parse_supplier_reply(text, catalog):
    """Parse one supplier reply into priced ingredient quotes.

    Deterministic: money tokens ('48rb', 'Rp50.000') paired with a known
    ingredient name/alias on the same line. Returns
    {"quotes": [{ingredient_id, ingredient_name, unit_price_idr}], "unparsed": [...]}.
    Never invents a price.
    """
    quotes, unparsed = [], []
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for line in lines:
        price = _MONEY.search(line)
        if not price:
            unparsed.append(line)
            continue
        matched = None
        lowered = _normalized(line)
        for candidate_id, aliases in catalog.items():
            for alias in aliases:
                alias = _normalized(alias)
                if alias and (alias in lowered or (len(lowered) >= 4 and lowered in alias)):
                    matched = (candidate_id, aliases[0])
                    break
            if matched:
                break
        amount = _money_idr(price.group(0))
        if matched and amount:
            quotes.append({"ingredient_id": matched[0], "ingredient_name": matched[1], "unit_price_idr": amount, "line": line})
        else:
            unparsed.append(line)
    return {"quotes": quotes, "unparsed": unparsed}


def _normalized(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())




class SupplierMixin:
    """Supplier entities, price quotes, and the comparison agent (both stores).

    SQL runs through ``self._inventory_query`` with ``?`` placeholders; the
    tenant column is ``restaurant_id`` in SQLite and ``slug`` in the hosted
    Postgres schema, resolved per query via ``_tenant_col()``. Tables come
    from migration 0008.
    """

    def _tenant_col(self):
        return "slug" if self.inventory_postgres else "restaurant_id"

    def _suppliers_ready(self, conn):
        return True  # SQLite creates the tables at initialize; hosted validated by REQUIRED_SCHEMA

    # ---- entity lifecycle --------------------------------------------------

    def _supplier_ensure(self, conn, restaurant_id, name):
        """The supplier entity for a canonical purchase name; creates it when new."""
        from .db import now
        tc = self._tenant_col()
        if not name or not str(name).strip():
            return None
        name = str(name).strip()
        row = self._inventory_query(
            conn, f"SELECT id FROM suppliers WHERE {tc}=? AND lower(trim(name))=lower(trim(?))",
            (restaurant_id, name)).fetchone()
        if row:
            return row[0]
        supplier_id = uuid.uuid4().hex
        self._inventory_query(conn, f"INSERT INTO suppliers (id, {tc}, name, created_at) VALUES (?, ?, ?, ?)",
                              (supplier_id, restaurant_id, name, now()))
        return supplier_id

    def _supplier_phone_taken(self, conn, phone):
        """True when the phone is an owner-linked number (either platform)."""
        row = self._inventory_query(conn, "SELECT 1 FROM whatsapp_links WHERE phone=? LIMIT 1", (str(phone),)).fetchone()
        return row is not None

    def _phone_is_supplier_contact(self, conn, phone):
        """True when the phone is a registered supplier contact."""
        tc = self._tenant_col()
        row = self._inventory_query(conn, f"SELECT 1 FROM suppliers WHERE {tc}=? AND contact_phone=? LIMIT 1",
                                    (restaurant_id_dummy, phone)).fetchone() if False else \
            self._inventory_query(conn, "SELECT 1 FROM suppliers WHERE contact_phone=? LIMIT 1", (str(phone),)).fetchone()
        return row is not None

    def supplier_add(self, key, payload, restaurant_id, conn=None):
        from .db import DomainError, now
        payload = dict(payload or {})
        name = str(payload.get("name") or "").strip()[:100]
        phone = str(payload.get("contact_phone") or "").strip()
        platform = str(payload.get("contact_platform") or "").strip().casefold()
        notes = str(payload.get("notes") or "").strip()[:300]
        if not name:
            raise DomainError("invalid_request", "Nama supplier wajib diisi.", 422)
        if phone:
            if not _PHONE.fullmatch(phone):
                raise DomainError("invalid_request", "Nomor kontak tidak valid.", 422)
            platform = platform if platform in _PLATFORMS else "whatsapp"
        else:
            phone, platform = None, None
        stored = {"name": name, "contact_phone": phone, "contact_platform": platform, "notes": notes}
        tc = self._tenant_col()

        def action(conn):
            if phone and self._supplier_phone_taken(conn, phone):
                raise DomainError("phone_is_owner_number", "Nomor itu terdaftar sebagai nomor pemilik, bukan supplier.", 409)
            existing = self._inventory_query(
                conn, f"SELECT id FROM suppliers WHERE {tc}=? AND lower(trim(name))=lower(trim(?))",
                (restaurant_id, name)).fetchone()
            if existing:
                raise DomainError("supplier_exists", f"Supplier '{name}' sudah terdaftar.", 409)
            supplier_id = uuid.uuid4().hex
            self._inventory_query(conn, f"INSERT INTO suppliers (id, {tc}, name, contact_phone, contact_platform, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (supplier_id, restaurant_id, name, phone, platform, notes or None, now()))
            self._audit(conn, "supplier_added", f"Supplier '{name}' ditambahkan" + (f" (kontak {platform})." if phone else "."), restaurant_id)
            return self.supplier_detail(restaurant_id, supplier_id, conn=conn)

        if conn is not None:
            return action(conn)
        return self._idempotent(key, "supplier_add:" + restaurant_id + ":" + _normalized(name), stored, action)

    def supplier_update(self, restaurant_id, supplier_id, key, payload, conn=None):
        from .db import DomainError
        payload = dict(payload or {})
        tc = self._tenant_col()

        def action(conn):
            row = self._inventory_query(conn, f"SELECT id FROM suppliers WHERE {tc}=? AND id=?",
                                        (restaurant_id, supplier_id)).fetchone()
            if not row:
                raise DomainError("not_found", "Supplier tidak ditemukan.", 404)
            name = str(payload.get("name") or "").strip()[:100]
            if name:
                self._inventory_query(conn, "UPDATE suppliers SET name=? WHERE id=?", (name, supplier_id))
            if "contact_phone" in payload:
                phone = str(payload.get("contact_phone") or "").strip()
                if phone:
                    if not _PHONE.fullmatch(phone):
                        raise DomainError("invalid_request", "Nomor kontak tidak valid.", 422)
                    if self._supplier_phone_taken(conn, phone):
                        raise DomainError("phone_is_owner_number", "Nomor itu terdaftar sebagai nomor pemilik, bukan supplier.", 409)
                    self._inventory_query(conn, "UPDATE suppliers SET contact_phone=?, contact_platform=? WHERE id=?",
                                          (phone, str(payload.get("contact_platform") or "whatsapp"), supplier_id))
                else:
                    self._inventory_query(conn, "UPDATE suppliers SET contact_phone=NULL, contact_platform=NULL WHERE id=?", (supplier_id,))
            if "notes" in payload:
                self._inventory_query(conn, "UPDATE suppliers SET notes=? WHERE id=?",
                                      (str(payload.get("notes") or "").strip()[:300] or None, supplier_id))
            self._audit(conn, "supplier_updated", f"Supplier {supplier_id[:8]} diperbarui.", restaurant_id)
            return self.supplier_detail(restaurant_id, supplier_id, conn=conn)

        if conn is not None:
            return action(conn)
        return self._idempotent(key, "supplier_update:" + supplier_id, payload, action)

    def supplier_remove(self, restaurant_id, supplier_id, key, conn=None):
        from .db import DomainError
        tc = self._tenant_col()

        def action(conn):
            row = self._inventory_query(conn, f"SELECT name FROM suppliers WHERE {tc}=? AND id=?",
                                        (restaurant_id, supplier_id)).fetchone()
            if not row:
                raise DomainError("not_found", "Supplier tidak ditemukan.", 404)
            used = self._inventory_query(conn, f"SELECT 1 FROM purchases WHERE {tc}=? AND supplier_id=? LIMIT 1",
                                         (restaurant_id, supplier_id)).fetchone()
            if used:
                raise DomainError("supplier_in_use", "Supplier masih punya riwayat pembelian. Hapus lewat riwayat pembelian, atau ubah kontak saja.", 409)
            self._inventory_query(conn, "DELETE FROM supplier_price_quotes WHERE supplier_id=?", (supplier_id,))
            self._inventory_query(conn, "DELETE FROM suppliers WHERE id=?", (supplier_id,))
            self._audit(conn, "supplier_removed", f"Supplier '{row[0]}' dihapus.", restaurant_id)
            return {"ok": True}

        if conn is not None:
            return action(conn)
        return self._idempotent(key, "supplier_remove:" + supplier_id, {"supplier_id": supplier_id}, action)

    # ---- reads --------------------------------------------------------------

    def supplier_detail(self, restaurant_id, supplier_id, conn=None):
        tc = self._tenant_col()
        if conn is None:
            with self.transaction() as conn:
                return self._supplier_detail_conn(conn, tc, restaurant_id, supplier_id)
        return self._supplier_detail_conn(conn, tc, restaurant_id, supplier_id)

    def _supplier_detail_conn(self, conn, tc, restaurant_id, supplier_id):
        row = self._inventory_query(conn, f"SELECT id, name, contact_phone, contact_platform, notes, created_at FROM suppliers WHERE {tc}=? AND id=?",
                                    (restaurant_id, supplier_id)).fetchone()
        if not row:
            raise DomainError("not_found", "Supplier tidak ditemukan.", 404)
        return {"id": row[0], "name": row[1], "contact_phone": row[2], "contact_platform": row[3], "notes": row[4], "created_at": row[5]}

    def suppliers_list(self, restaurant_id, conn=None):
        tc = self._tenant_col()
        if conn is None:
            with self.transaction() as conn:
                return self._suppliers_list_conn(conn, tc, restaurant_id)
        return self._suppliers_list_conn(conn, tc, restaurant_id)

    def _suppliers_list_conn(self, conn, tc, restaurant_id):
        unpaid_expr = ("(SELECT COUNT(*) FROM purchases p WHERE p.supplier_id=s.id "
                       "AND lower(COALESCE(p.body->>'payment_status','unknown'))='unpaid')" if self.inventory_postgres else
                       "(SELECT COUNT(*) FROM purchases p WHERE p.supplier_id=s.id "
                       "AND lower(COALESCE(json_extract(p.body,'$.payment_status'),'unknown')='unpaid'))")
        rows = self._inventory_query(
            conn,
            "SELECT s.id, s.name, s.contact_phone, s.contact_platform, s.notes, s.created_at, "
            "(SELECT COUNT(*) FROM purchases p WHERE p.supplier_id=s.id), "
            + unpaid_expr + ", "
            "(SELECT MAX(p.created_at) FROM purchases p WHERE p.supplier_id=s.id) "
            f"FROM suppliers s WHERE s.{tc}=? ORDER BY s.name",
            (restaurant_id,)).fetchall()
        return [{"id": r[0], "name": r[1], "contact_phone": r[2], "contact_platform": r[3], "notes": r[4],
                 "created_at": r[5], "purchase_count": r[6], "unpaid_count": r[7], "last_purchase_at": r[8]}
                for r in rows]

    def supplier_for_phone(self, phone):
        """The supplier contact for this number, or None (owner numbers resolve elsewhere)."""
        tc = self._tenant_col()
        with self.transaction() as conn:
            row = self._inventory_query(conn, f"SELECT id, name, {tc} FROM suppliers WHERE contact_phone=?",
                                        (str(phone),)).fetchone()
        return {"id": row[0], "name": row[1], "restaurant_id": row[2]} if row else None

    # ---- quotes -------------------------------------------------------------

    def supplier_quote_record(self, key, payload, restaurant_id, conn=None, source="manual", inquiry_id=None):
        from .db import DomainError, now
        payload = dict(payload or {})
        supplier_id = payload.get("supplier_id")
        ingredient_id = payload.get("ingredient_id")
        ingredient_name = payload.get("ingredient_name")
        unit_price = payload.get("unit_price_idr")
        if not supplier_id or unit_price in (None, ""):
            raise DomainError("invalid_request", "Supplier dan harga wajib diisi.", 422)
        tc = self._tenant_col()

        def action(conn):
            row = self._inventory_query(conn, f"SELECT 1 FROM suppliers WHERE {tc}=? AND id=?",
                                        (restaurant_id, supplier_id)).fetchone()
            if not row:
                raise DomainError("not_found", "Supplier tidak ditemukan.", 404)
            quote_id = uuid.uuid4().hex
            from .db import now
            self._inventory_query(conn, f"INSERT INTO supplier_price_quotes (id, {tc}, supplier_id, ingredient_id, ingredient_name, unit_price_idr, note, raw_message, source, inquiry_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                  (quote_id, restaurant_id, supplier_id, ingredient_id, ingredient_name,
                                   int(unit_price), payload.get("note"), payload.get("raw_message") or "",
                                   source, inquiry_id, now()))
            self._audit(conn, "supplier_quote_recorded", f"Harga {ingredient_name or ingredient_id}: Rp{int(unit_price):,} dicatat.", restaurant_id)
            return {"ok": True, "id": quote_id}

        if conn is not None:
            return action(conn)
        return self._idempotent(key, "supplier_quote:" + restaurant_id + ":" + str(supplier_id) + ":" + str(ingredient_id) + ":" + str(unit_price), payload, action)

    def supplier_quotes_recent(self, restaurant_id, ingredient_id=None, limit=10, conn=None):
        tc = self._tenant_col()
        if conn is None:
            with self.transaction() as conn:
                return self._supplier_quotes_recent_conn(conn, tc, restaurant_id, ingredient_id, limit)
        return self._supplier_quotes_recent_conn(conn, tc, restaurant_id, ingredient_id, limit)

    def _supplier_quotes_recent_conn(self, conn, tc, restaurant_id, ingredient_id, limit):
        ingredient_filter = "AND q.ingredient_id=?" if ingredient_id else ""
        params = [restaurant_id] + ([ingredient_id] if ingredient_id else []) + [limit]
        rows = self._inventory_query(
            conn,
            "SELECT q.supplier_id, s.name, q.ingredient_id, q.ingredient_name, q.unit_price_idr, q.note, q.created_at "
            f"FROM supplier_price_quotes q JOIN suppliers s ON s.id=q.supplier_id WHERE q.{tc}=? "
            + ingredient_filter + " ORDER BY q.created_at DESC LIMIT ?",
            params).fetchall()
        return [{"supplier_id": r[0], "supplier_name": r[1], "ingredient_id": r[2], "ingredient_name": r[3],
                 "unit_price_idr": r[4], "note": r[5], "created_at": r[6]} for r in rows]

    def supplier_purchase_price(self, conn, restaurant_id, ingredient_id):
        """The latest recorded purchase unit price for an ingredient (evidence baseline)."""
        rows = self._inventory_query(conn, "SELECT body, created_at FROM purchases WHERE restaurant_id=? ORDER BY created_at DESC, id DESC LIMIT 50",
                                     (restaurant_id,)).fetchall()
        import sys as _sys
        print("DBG-PP-ROWS:", [(json.loads(r[0]).get("supplier"), json.loads(r[0]).get("items", [{}])[0].get("unit_price_idr"), r[1][:26]) for r in rows])
        for row in rows:
            try:
                purchase = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else dict(row[0])
            except ValueError:
                continue
            for item in purchase.get("items") or []:
                if item.get("ingredient_id") == ingredient_id and item.get("unit_price_idr"):
                    return {"unit_price_idr": int(item["unit_price_idr"]), "created_at": row[1],
                            "supplier": purchase.get("supplier")}
        return None

    # ---- comparison ---------------------------------------------------------

    def supplier_price_comparison(self, restaurant_id, ingredient_id, ingredient_name=None, conn=None):
        """Compare suppliers for one ingredient: freshest quotes + purchase baseline."""
        tc = self._tenant_col()
        if conn is None:
            with self.transaction() as conn:
                return self._supplier_price_comparison_conn(conn, tc, restaurant_id, ingredient_id)
        return self._supplier_price_comparison_conn(conn, tc, restaurant_id, ingredient_id)

    def _supplier_price_comparison_conn(self, conn, tc, restaurant_id, ingredient_id):
        quotes = self.supplier_quotes_recent(restaurant_id, ingredient_id, limit=20, conn=conn)
        baseline = self.supplier_purchase_price(conn, restaurant_id, ingredient_id)
        latest_per_supplier = {}
        for quote in quotes:
            latest_per_supplier.setdefault(quote["supplier_name"], quote)
        comparison = sorted(latest_per_supplier.values(),
                            key=lambda q: q["unit_price_idr"] if q["unit_price_idr"] is not None else 10**12)
        return {"ingredient_id": ingredient_id, "quotes": comparison,
                "purchase_baseline": baseline, "quote_count": len(quotes)}

    # ---- inquiry storage (2b) ----------------------------------------------

    def supplier_inquiry_create(self, conn, restaurant_id, request, detail=None):
        from .db import now
        tc = self._tenant_col()
        inquiry_id = uuid.uuid4().hex
        stored_detail = json.dumps(detail or {}, ensure_ascii=False)

        def action(conn):
            contacts = self._inventory_query(conn, f"SELECT COUNT(*) FROM suppliers WHERE {tc}=? AND contact_phone IS NOT NULL",
                                             (restaurant_id,)).fetchone()[0]
            self._inventory_query(conn, f"INSERT INTO supplier_inquiries (id, {tc}, request, status, detail, created_at) VALUES (?, ?, ?, 'open', ?, ?)",
                                  (inquiry_id, restaurant_id, request, stored_detail, now()))
            return inquiry_id, contacts

        if conn is not None:
            return action(conn)
        with self.transaction() as conn:
            return action(conn)

    def supplier_inquiry_open(self, restaurant_id):
        with self.transaction() as conn:
            tc = self._tenant_col()
            rows = self._inventory_query(conn, f"SELECT id, request, created_at FROM supplier_inquiries WHERE {tc}=? AND status='open' ORDER BY created_at DESC",
                                         (restaurant_id,)).fetchall()
        return [{"id": r[0], "request": r[1], "created_at": r[2]} for r in rows]


    # ---- chat command dispatch (store-level; network sends live in api) ----

    def _ingredient_from_text(self, conn, restaurant_id, text):
        """Match one ingredient mentioned in the text using the same alias
        mapping as stock questions (catalog aliases + runtime ingredients)."""
        from .db import DomainError as _DE  # noqa: keep parity with other mixins
        from .extraction import catalog_aliases
        lowered = " " + re.sub(r"\s+", " ", str(text or "").strip().casefold()) + " "
        aliases = list(catalog_aliases())
        covered = {ingredient_id for _alias, ingredient_id in aliases}
        for ingredient in self._catalog_for(restaurant_id)["list"]:
            if ingredient["id"] in covered:
                continue
            for surface in (str(ingredient["id"]).replace("_", " "), str(ingredient["name"]).casefold()):
                surface = surface.strip()
                if surface:
                    aliases.append((surface, ingredient["id"]))
        for alias, ingredient_id in sorted(aliases, key=lambda pair: len(pair[0]), reverse=True):
            if re.search(r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])", lowered):
                ingredient = next((entry for entry in self._catalog_for(restaurant_id)["list"] if entry["id"] == ingredient_id), None)
                return ingredient_id, (ingredient or {}).get("name") or alias
        return None, None

    def supplier_command(self, conn, tenant, command, text, *, key):
        """Handle daftar/tambah/compare/catat-harga inside the ingest transaction.

        'inquiry' (tanya harga) only records the request here; the actual
        sending happens in the API layer, which passes the reply back via
        payload['supplier_answer'].
        """
        kind = command.get("kind")
        if kind == "list":
            suppliers = self.suppliers_list(tenant, conn=conn)
            if not suppliers:
                return ("Belum ada supplier terdaftar. Pembelian tercatat otomatis membuat supplier, "
                        "atau tambahkan dengan 'tambah supplier <nama>'.", {"action": "supplier_list_empty"})
            lines = ["Daftar supplier:"]
            for index, supplier in enumerate(suppliers, start=1):
                contact = f" · {supplier['contact_platform'] or 'wa'} {supplier['contact_phone']}" if supplier["contact_phone"] else ""
                unpaid = f" · utang {supplier['unpaid_count']} nota" if supplier["unpaid_count"] else ""
                lines.append(f"{index}. {supplier['name']} ({supplier['purchase_count']}× beli{contact}){unpaid}")
            return ("\n".join(lines), {"action": "supplier_list", "suppliers": suppliers})
        if kind == "add":
            name, phone, platform = self._parse_add_parts(command.get("text") or text)
            try:
                result = self.supplier_add(key, {"name": name, "contact_phone": phone,
                                                 "contact_platform": platform}, tenant, conn=conn)
            except DomainError as exc:
                return (str(exc), {"action": "supplier_add_rejected"})
            contact = f" · kontak {result.get('contact_platform') or 'wa'} {result.get('contact_phone')}" if result.get("contact_phone") else ""
            return (f"Supplier '{result.get('name')}' ditambahkan{contact}.",
                    {"action": "supplier_added", "supplier_id": result.get("id")})
        if kind == "quote":
            return self._supplier_quote_from_text(conn, tenant, command.get("text") or text, key)
        if kind == "inquiry":
            request = self._inquiry_request_text(text)
            inquiry_id, contact_count = self.supplier_inquiry_create(conn, tenant, request)
            if not contact_count:
                return ("Belum ada supplier dengan kontak. Tambahkan dulu: 'tambah supplier <nama> wa <nomor>'.",
                        {"action": "supplier_inquiry_no_contacts"})
            return (f"Permintaan harga dikirim ke {contact_count} supplier: '{request}'. "
                    "Balasan mereka akan dicatat sebagai perbandingan harga.",
                    {"action": "supplier_inquiry_created", "inquiry_id": inquiry_id, "contact_count": contact_count})
        return self._supplier_compare_reply(conn, tenant, command.get("text") or text)

    def _supplier_quote_from_text(self, conn, tenant, command_text, key):
        """'catat harga ayam pak budi 48rb' → manual price quote."""
        price = _money_idr(command_text)
        if price is None:
            return ("Sebutkan harganya, contoh: catat harga ayam pak budi 48rb.",
                    {"action": "supplier_quote_rejected"})
        suppliers = {s["name"]: s for s in self.suppliers_list(tenant, conn=conn)}
        supplier_hit = None
        lowered = command_text.casefold()
        for name, supplier in suppliers.items():
            if _normalized(name) and _normalized(name) in _normalized(lowered):
                supplier_hit = supplier
                break
        if supplier_hit is None:
            return ("Supplier tidak dikenali. Supplier terdaftar: "
                    + (", ".join(suppliers.keys()) or "belum ada") + ".",
                    {"action": "supplier_quote_rejected"})
        ingredient_id, ingredient_name = self._ingredient_from_text(conn, tenant, command_text)
        if not ingredient_id:
            return ("Bahan tidak dikenali. Sebutkan bahan yang ada di katalog, contoh: catat harga cabai pak budi 48rb.",
                    {"action": "supplier_quote_rejected"})
        result = self.supplier_quote_record(key, {"supplier_id": supplier_hit["id"], "ingredient_id": ingredient_id,
                                                  "ingredient_name": ingredient_name, "unit_price_idr": price,
                                                  "raw_message": command_text}, tenant, conn=conn)
        return (f"Harga {ingredient_name} dari {supplier_hit['name']}: Rp{price:,}/satuan. Tercatat.",
                {"action": "supplier_quote_recorded", "result": result})

    def _supplier_compare_reply(self, conn, tenant, text):
        """Cheapest supplier for one ingredient: freshest quotes, purchase baseline."""
        keywords = re.sub(r"(?i)\b(supplier|pemasok|harga|berapa|termurah|murah|banding|dibanding|dari|untuk|yang|mana)\b", " ", str(text))
        ingredient_id, ingredient_name = self._ingredient_from_text(conn, tenant, keywords)
        if not ingredient_id:
            return ("Sebutkan bahannya, contoh: supplier cabai termurah?",
                    {"action": "supplier_compare_missing"})
        comparison = self.supplier_price_comparison(tenant, ingredient_id, conn=conn)
        lines = [f"Harga {ingredient_name} per supplier:"]
        cheapest = None
        for entry in comparison["quotes"]:
            if entry["unit_price_idr"] is None:
                continue
            price = f"Rp{entry['unit_price_idr']:,}".replace(",", ".")
            note = f" ({entry['note']})" if entry.get("note") else ""
            lines.append(f"• {entry['supplier_name']}: {price}{note}")
            cheapest = cheapest or entry["supplier_name"]
        baseline = comparison.get("purchase_baseline")
        if baseline:
            price = f"Rp{baseline['unit_price_idr']:,}".replace(",", ".")
            lines.append(f"• Harga beli terakhirmu: {price} ({baseline.get('supplier') or 'tanpa pemasok'}, {str(baseline.get('created_at') or '')[:10]})")
        if cheapest:
            lines.append(f"Termurah saat ini: {cheapest}.")
        else:
            lines.append("Belum ada kutipan harga — 'tanya harga " + ingredient_name + " ke supplier' untuk menanyakan langsung.")
        return ("\n".join(lines), {"action": "supplier_compare",
                                   "comparison": comparison, "purchase_baseline": baseline})

    def _parse_add_parts(self, command_text, original=None):
        original = str(original if original is not None else command_text or "")
        lowered = original.casefold()
        phone = None
        platform = "whatsapp"
        phone_match = re.search(r"(?i)\b(wa|whatsapp|telegram|tg)\s*[:=]?\s*(628\d{6,}|[1-9]\d{6,14})", lowered)
        if phone_match:
            platform = "telegram" if phone_match.group(1).casefold() in ("telegram", "tg") else "whatsapp"
            phone = phone_match.group(2)
            original_name = original[:phone_match.start()] + " " + original[phone_match.end():]
        else:
            original_name = original
        name_match = re.search(r"(?i)tambah\s+(supplier|pemasok)\s*", original_name)
        name = original_name[name_match.end():].strip() if name_match else original_name.strip()
        name = re.sub(r"[\s\?\.$]+$", "", name)[:100].strip()
        return name, phone, platform

    def _inquiry_request_text(self, text):
        request = re.sub(r"(?i)^\s*tanya\s+harga\s*", "", str(text or "").strip())
        request = re.sub(r"(?i)\b(ke|untuk)\s+(semua\s+)?(supplier|pemasok)\b\s*\??\s*$", "", request).strip()
        request = re.sub(r"[\s\?]+$", "", request).strip()
        return request or "harga bahan"

    # ---- inquiry send + supplier reply intake (2b) --------------------------

    def supplier_inquiry_targets(self, restaurant_id, conn=None):
        """Suppliers with any contact channel, each tagged with its platform."""
        tc = self._tenant_col()
        if conn is None:
            with self.transaction() as conn:
                return self._supplier_inquiry_targets_conn(conn, tc, restaurant_id)
        return self._supplier_inquiry_targets_conn(conn, tc, restaurant_id)

    def _supplier_inquiry_targets_conn(self, conn, tc, restaurant_id):
        rows = self._inventory_query(conn, "SELECT id, name, contact_phone, contact_platform FROM suppliers WHERE {tc}=? AND contact_phone IS NOT NULL".format(tc=tc),
                                     (restaurant_id,)).fetchall()
        return [{"id": r[0], "name": r[1], "contact_phone": r[2], "contact_platform": r[3] or "whatsapp"} for r in rows]

    def supplier_reply_intake(self, restaurant_id, supplier_id, text, *, inquiry_id=None, key=None):
        """Parse one supplier reply and store price quotes (never purchases).

        Deterministic: money tokens paired with ingredient names via the
        same alias mapping as stock questions. Unparseable lines are stored
        as notes on the quote-less record and reported to the owner.
        """
        from .db import now
        parsed_quotes = []
        unparsed = []
        # Ingredient matching opens its own short transactions: do it BEFORE
        # the write transaction so SQLite never nests BEGIN IMMEDIATE.
        for line in [line.strip() for line in str(text or "").splitlines() if line.strip()]:
            price = _MONEY_IDR(line)
            ingredient_id, ingredient_name = self._ingredient_from_text(None, restaurant_id, line)
            if price is not None and ingredient_id:
                parsed_quotes.append({"ingredient_id": ingredient_id, "ingredient_name": ingredient_name, "price": price})
            else:
                unparsed.append(line)
        with self.transaction() as conn:
            stored = []
            for quote in parsed_quotes:
                quote_id = uuid.uuid4().hex
                tc = self._tenant_col()
                self._inventory_query(conn, f"INSERT INTO supplier_price_quotes (id, {tc}, supplier_id, ingredient_id, ingredient_name, unit_price_idr, note, raw_message, source, inquiry_id, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'reply', ?, ?)",
                                      (quote_id, restaurant_id, supplier_id, quote["ingredient_id"], quote["ingredient_name"], quote["price"], line.strip()[:300], inquiry_id, now()))
                stored.append(quote)
        return {"stored": stored, "unparsed": unparsed}


def _MONEY_IDR(line):
    match = _MONEY.search(str(line or ""))
    if not match:
        return None
    return _money_idr(match.group(0))


def line_for_quote(text, quote):
    for line in str(text or "").splitlines():
        if quote.get("ingredient_name") and quote["ingredient_name"].casefold() in line.casefold():
            return line.strip()[:300]
    return (text or "")[:300]


async def supplier_inquiry_send(store, tg_sender_fn, wa_sender_fn, restaurant_id, command):
    """Send a price inquiry to every supplier with a contact channel.

    ``tg_sender_fn(name, phone, text)`` sends one Telegram message;
    ``wa_sender_fn(name, phone, text)`` sends one WhatsApp template message
    (an approved template is required - suppliers rarely write first, so
    the 24-hour window is closed; without one configured, WhatsApp contacts
    are skipped and reported honestly). Returns the owner-facing reply body.
    """
    request = _inquiry_request_text_module(command.get("text") or "")
    inquiry_id, _contacts = store.supplier_inquiry_create(None, restaurant_id, request)
    targets = store.supplier_inquiry_targets(restaurant_id)
    sent, skipped = [], []
    for target in targets:
        sender_fn = tg_sender_fn if target["contact_platform"] == "telegram" else wa_sender_fn
        try:
            await sender_fn(target["name"], target["contact_phone"],
                            f"Halo {target['name']}, mau tanya harga {request} sekarang berapa ya? Terima kasih.")
            sent.append(f"{target['name']} ({target['contact_platform']})")
        except Exception:
            skipped.append(f"{target['name']} ({target['contact_platform']})")
    parts = [f"Permintaan harga '{request}' dikirim ke {len(sent)} supplier." if sent else
             "Tidak ada supplier yang bisa dikirim."]
    if skipped:
        parts.append("Dilewati: " + ", ".join(skipped) + ".")
    parts.append("Balasan mereka akan dicatat dan bisa dicek dengan 'harga <bahan> dari supplier?'.")
    return "\n".join(parts)


def _inquiry_request_text_module(text):
    request = re.sub(r"(?i)^\s*tanya\s+harga\s*", "", str(text or "").strip())
    request = re.sub(r"(?i)\b(ke|untuk)\s+(semua\s+)?(supplier|pemasok)\b\s*\??\s*$", "", request).strip()
    request = re.sub(r"[\s\?]+$", "", request).strip()
    return request or "harga bahan"



def supplier_alert_hint(store, restaurant_id, candidates):
    """One line naming the cheapest known source per low-stock ingredient.

    Considers recorded quotes first, then purchase prices; returns None
    when nothing useful is known (no inventing).
    """
    hints = []
    with store.transaction() as conn:
        for candidate in candidates:
            ingredient_id = candidate.get("ingredient_id")
            if not ingredient_id:
                continue
            quote = store.supplier_quotes_recent(restaurant_id, ingredient_id, limit=1, conn=conn)
            purchase = store.supplier_purchase_price(conn, restaurant_id, ingredient_id)
            name = candidate.get("name") or ingredient_id
            if quote and quote[0].get("unit_price_idr") is not None:
                price = f"Rp{quote[0]['unit_price_idr']:,}".replace(",", ".")
                hints.append(f"{name}: termurah tercatat {price} ({quote[0]['supplier_name']})")
            elif purchase:
                price = f"Rp{purchase['unit_price_idr']:,}".replace(",", ".")
                hints.append(f"{name}: harga beli terakhir {price} ({purchase.get('supplier') or 'tanpa pemasok'})")
    return ("Harga termurah yang tercatat: " + "; ".join(hints) + ".") if hints else None
