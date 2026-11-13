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
    """Owner supplier commands: daftar / tambah / hapus / ubah / catat harga /
    tanya harga / riwayat / harga per bahan. Returns None when the message is
    not a supplier command."""
    text = str(text or "").strip()
    if len(text) > 300 or not re.search(r"(?i)\b(supplier|pemasok|catat\s+harga)\b", text):
        # A bare price quote ('harga telur 45rb dari pak budi') names no
        # supplier keyword; keep it out of the purchase draft parser, where it
        # used to be filed as a purchase instead of a price quote.
        if re.match(r"(?i)^harga\s+\S+", text) and _MONEY.search(text) \
                and not re.search(r"(?i)\b(beli|ambil|pesan|jual)\b", text) and "?" not in text:
            return {"kind": "quote", "text": text}
        return None
    lowered = text.casefold()
    if re.search(r"(?i)\b(daftar|list)\s+(supplier|pemasok)", lowered):
        return {"kind": "list"}
    if re.search(r"(?i)\btambah\s+(supplier|pemasok)\b", lowered):
        return {"kind": "add", "text": text}
    if re.search(r"(?i)\bhapus\s+(supplier|pemasok)\b", lowered):
        return {"kind": "remove", "text": text}
    if re.search(r"(?i)\b(ubah|ganti)\s+(supplier|pemasok|nomor|kontak)\b", lowered):
        return {"kind": "update", "text": text}
    if re.search(r"(?i)\briwayat\s+harga\b", lowered):
        return {"kind": "history", "text": text}
    if re.search(r"(?i)\b(tanya|minta)\s+harga\b", lowered):
        return {"kind": "inquiry", "text": text}
    if re.search(r"(?i)\bcatat\s+harga\b", lowered):
        return {"kind": "quote", "text": text}
    # Recording with the supplier named explicitly and no question words:
    # 'supplier budi cabai 30rb' is a quote, not a comparison question.
    if _MONEY.search(text) and re.search(r"(?i)\bsupplier\b", lowered) \
            and not re.search(r"(?i)\b(berapa|banding|dibanding|murah|termurah)\b", lowered) and "?" not in text:
        return {"kind": "quote", "text": text}
    if re.search(r"(?i)\bsupplier\b", lowered) and re.search(r"(?i)\b(harga|berapa|murah|termurah|banding)\b", lowered):
        return {"kind": "compare", "text": text}
    return None


_UNIT_PHRASE_RE = re.compile(
    r"(?i)\bper\s+((?:setengah|1\s*/\s*2|½)?\s*(?:\d+[.,]?\d*\s*)?"
    r"(?:kg|kilo(?:gram)?|gram|gr|g|ons|ikat|tray|peti|dus|bungkus|bks|pak|porsi|liter|lt))\b")
_UNIT_WORD_RE = re.compile(
    r"(?i)^(setengah|1\s*/\s*2|½|\d+[.,]?\d*)?\s*(kg|kilo|kilogram|gram|gr|g|ons|ikat|tray|peti|dus|bungkus|bks|pak|porsi|liter|lt)$")


def _unit_multiplier(phrase, catalog_unit):
    """Per-catalog-unit multiplier for a 'per <unit>' phrase, or None when it
    is unknown (the quote keeps its raw amount and carries the phrase as a
    note, so comparisons never silently mix units)."""
    if not phrase or not catalog_unit:
        return None
    catalog_unit = str(catalog_unit).strip().casefold()
    if catalog_unit not in ("kg", "kilo", "kilogram"):
        return None
    phrase = re.sub(r"\s+", " ", phrase.strip().casefold())
    if re.search(r"(?i)setengah|1\s*/\s*2|½|0[,×.]5", phrase) and re.search(r"(?i)kg|kilo", phrase):
        return Decimal(2)
    mass_grams = None
    if re.search(r"(?i)kg|kilo", phrase):
        mass_grams = Decimal(1000)
        grams = re.search(r"(\d+[.,]?\d*)", phrase)
        if grams and not re.search(r"(?i)\bkg\b|kilo", grams.group(0)):
            mass_grams = Decimal(grams.group(1).replace(",", "."))
        elif grams:
            pass
    number = re.search(r"(\d+[.,]?\d*)", phrase)
    if number and re.search(r"(?i)\bgram\b|\bgr\b|\bg\b", phrase):
        mass_grams = Decimal(number.group(1).replace(",", "."))
    if number and re.search(r"(?i)\bons\b", phrase):
        mass_grams = Decimal(number.group(1).replace(",", ".")) * Decimal(100)
    if mass_grams is None or mass_grams <= 0:
        return None
    return (Decimal(1000) / mass_grams).quantize(Decimal("0.0001"))


def _quote_unit_note(line):
    """A 'per <unit>' phrase on the reply line, or None."""
    match = _UNIT_PHRASE_RE.search(str(line or ""))
    return match.group(0).strip() if match else None


def _pair_line_quotes(line, resolve, catalog_unit=None):
    """Pair every money token on a reply line with its nearest ingredient.

    Deterministic segment walk: 'telur 45rb cabai 30rb' yields two quotes,
    '45rb untuk telur' pairs backwards. A 'per <unit>' phrase is removed
    before price scanning (so 'per 500 gram' is never read as a price) and
    becomes either a per-catalog-unit normalization or an honest note.
    Returns (quotes, leftover_texts); never invents a price.
    """
    cleaned = str(line or "")
    unit_note = None
    unit_match = _UNIT_PHRASE_RE.search(cleaned)
    if unit_match:
        unit_note = unit_match.group(0).strip()
        cleaned = cleaned[:unit_match.start()] + " " + cleaned[unit_match.end():]

    matches = list(_MONEY.finditer(cleaned))
    if not matches:
        return [], [cleaned.strip()] if cleaned.strip() else []
    segments, cursor = [], 0
    for match in matches:
        segments.append((cleaned[cursor:match.start()], _money_idr(match.group(0)), match.group(0)))
        cursor = match.end()
    segments.append((cleaned[cursor:], None, None))

    quotes, leftover = [], []
    pending = None  # (amount, raw) waiting for a later ingredient mention
    for seg_text, price_after, raw in segments:
        found = resolve(seg_text)
        if found:
            amount = pending[0] if pending else price_after
            pending = None
            if amount:
                note = unit_note
                multiplier = _unit_multiplier(unit_note, catalog_unit) if unit_note else None
                final = int((Decimal(amount) * multiplier).to_integral_value()) if multiplier else amount
                if multiplier and multiplier != 1:
                    note = f"asli Rp{amount:,} {unit_note}; dinormalisasi per {catalog_unit}"
                quotes.append({"ingredient_id": found[0], "ingredient_name": found[1],
                               "unit_price_idr": final, "note": note, "line": line})
            else:
                leftover.append(seg_text.strip() or f"(bahan tanpa harga: {found[1]})")
        elif price_after is not None:
            if pending is not None:
                leftover.append(f"(harga tanpa nama bahan: {pending[1]})")
            pending = (price_after, raw)
    if pending is not None:
        leftover.append(f"(harga tanpa nama bahan: {pending[1]})")
    return quotes, leftover


def parse_supplier_reply(text, catalog, units=None):
    """Parse one supplier reply into priced ingredient quotes.

    Deterministic: money tokens ('48rb', 'Rp50.000') paired with the nearest
    known ingredient name/alias on the same line; every pair on the line is
    extracted ('telur 45rb cabai 30rb' → two quotes). Returns
    {"quotes": [{ingredient_id, ingredient_name, unit_price_idr, note}],
    "unparsed": [...]}. Never invents a price.
    """
    quotes, unparsed = [], []
    for line in [line.strip() for line in str(text or "").splitlines() if line.strip()]:
        def resolve(segment):
            seg = _normalized(segment)
            if not seg:
                return None
            for candidate_id, aliases in catalog.items():
                for alias in aliases:
                    alias = _normalized(alias)
                    # Segment-only matching: a whole-line fallback would
                    # attribute every price on the line to the first catalog
                    # entry ('telur 45rb cabai 30rb' → two Telur quotes).
                    if alias and alias in seg:
                        return (candidate_id, aliases[0])
            return None

        probe_pairs, probe_leftover = _pair_line_quotes(line, resolve)
        if not probe_pairs and not probe_leftover:
            continue
        catalog_unit = (units or {}).get(probe_pairs[0]["ingredient_id"]) if probe_pairs else None
        line_pairs, line_leftover = _pair_line_quotes(line, resolve, catalog_unit)
        quotes.extend(line_pairs)
        unparsed.extend(text for text in line_leftover if text)
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
        rows = self._inventory_query(conn, f"SELECT body, created_at FROM purchases WHERE {self._tenant_col()}=? ORDER BY created_at DESC, id DESC LIMIT 50",
                                     (restaurant_id,)).fetchall()
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
        if kind == "remove":
            return self._supplier_remove_via_chat(conn, tenant, command.get("text") or text, key)
        if kind == "update":
            return self._supplier_update_via_chat(conn, tenant, command.get("text") or text, key)
        if kind == "history":
            return self._supplier_history_reply(conn, tenant, command.get("text") or text)
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

    def _resolve_supplier_from_text(self, conn, tenant, text):
        """Match 'hapus supplier pak budi' style text to exactly one supplier.

        Accepts the supplier name (case-insensitive containment), the first 8
        hex characters of its id, or its contact phone. Returns
        (supplier, None) | (None, reply_text) — multiple matches ask the
        owner to be more specific rather than guessing.
        """
        suppliers = self.suppliers_list(tenant, conn=conn)
        lowered = str(text or "").casefold()
        phone = None
        phone_match = re.search(r"(?i)(628\d{6,}|[1-9]\d{6,14})", lowered)
        if phone_match:
            phone = phone_match.group(1)
        id8 = None
        id_match = re.search(r"\b([0-9a-f]{8})\b", lowered)
        if id_match:
            id8 = id_match.group(1)
        matches = []
        stop = {"supplier", "pemasok", "hapus", "ubah", "ganti", "nomor", "kontak", "whatsapp",
                "telegram", "riwayat", "harga", "konfirmasi", "catatan", "kirim", "pagi"}
        for supplier in suppliers:
            name = str(supplier["name"] or "").casefold()
            if phone and supplier["contact_phone"] and phone in str(supplier["contact_phone"]):
                matches.append(supplier)
                continue
            if id8 and str(supplier["id"]).startswith(id8):
                matches.append(supplier)
                continue
            if name and name in lowered:
                matches.append(supplier)
                continue
            # Shorthand ('budi' for 'Pak Budi'): a distinctive name token the
            # owner typed still identifies the supplier.
            name_tokens = {token for token in re.split(r"[^a-z0-9]+", name) if len(token) >= 4 and token not in stop}
            text_tokens = set(re.findall(r"[a-z0-9]{4,}", lowered))
            if name_tokens and name_tokens & text_tokens:
                matches.append(supplier)
        if not matches:
            return None, ("Supplier tidak dikenali. Supplier terdaftar: "
                          + (", ".join(str(s["name"]) for s in suppliers) or "belum ada") + ".")
        if len(matches) > 1:
            return None, ("Beberapa supplier cocok — sebutkan lebih spesifik: "
                          + ", ".join(str(s["name"]) for s in matches) + ".")
        return matches[0], None

    def _supplier_remove_via_chat(self, conn, tenant, text, key):
        """'hapus supplier pak budi' — destructive, so it executes only when
        the same message carries the word KONFIRMASI; otherwise the owner
        gets the exact confirmation wording back (no thread state needed)."""
        target, error = self._resolve_supplier_from_text(conn, tenant, text)
        if error:
            return (error, {"action": "supplier_remove_rejected"})
        if not re.search(r"(?i)\bkonfirmasi\b", str(text or "")):
            return (f"Menghapus supplier menghapus juga kutipan harganya. Ketik ulang dengan kata KONFIRMASI: "
                    f"'hapus supplier {target['name']} KONFIRMASI'.",
                    {"action": "supplier_remove_confirm", "supplier_id": target["id"]})
        removed = self.supplier_remove(tenant, target["id"], key, conn=conn)
        return (f"Supplier '{target['name']}' dihapus.", {"action": "supplier_removed", "result": removed})

    def _supplier_update_via_chat(self, conn, tenant, text, key):
        """'ubah supplier budi wa 6281234567' / 'ubah supplier budi catatan murah'."""
        from .db import DomainError
        target, error = self._resolve_supplier_from_text(conn, tenant, re.sub(r"(?i)\b(ubah|ganti)\s+(supplier|pemasok|nomor|kontak)\b", " ", str(text or "")))
        if error:
            return (error, {"action": "supplier_update_rejected"})
        lowered = str(text or "").casefold()
        payload = {}
        phone_match = re.search(r"(?i)\b(wa|whatsapp|telegram|tg)\s*[:=]?\s*(628\d{6,}|[1-9]\d{6,14})", lowered)
        if phone_match:
            payload["contact_phone"] = phone_match.group(2)
            payload["contact_platform"] = "telegram" if phone_match.group(1) in ("telegram", "tg") else "whatsapp"
        notes_match = re.search(r"(?i)\bcatatan\s*[:=]?\s*(.+)", text)
        if notes_match:
            payload["notes"] = notes_match.group(1)[:300]
        if not payload:
            return ("Sebutkan yang diubah, contoh: 'ubah supplier budi wa 628123456789' "
                    "atau 'ubah supplier budi catatan: kirim pagi'.",
                    {"action": "supplier_update_rejected"})
        try:
            updated = self.supplier_update(tenant, target["id"], key, payload, conn=conn)
        except DomainError as exc:
            return (str(exc), {"action": "supplier_update_rejected"})
        contact = f"{updated.get('contact_platform') or 'wa'} {updated.get('contact_phone')}" if updated.get("contact_phone") else "tanpa kontak"
        return (f"Supplier '{updated.get('name')}' diperbarui · kontak: {contact}.",
                {"action": "supplier_updated", "supplier_id": updated.get("id")})

    def _supplier_history_reply(self, conn, tenant, text):
        """'riwayat harga supplier budi' — every recent quote from one supplier."""
        target, error = self._resolve_supplier_from_text(conn, tenant, re.sub(r"(?i)\briwayat\s+harga\b", " ", str(text or "")))
        if error:
            return (error, {"action": "supplier_history_missing"})
        rows = self._inventory_query(
            conn,
            "SELECT q.ingredient_name, q.unit_price_idr, q.note, q.created_at "
            f"FROM supplier_price_quotes q WHERE q.{self._tenant_col()}=? AND q.supplier_id=? "
            "ORDER BY q.created_at DESC LIMIT 12", (tenant, target["id"])).fetchall()
        if not rows:
            return (f"Belum ada kutipan harga dari {target['name']}. "
                    f"'tanya harga <bahan> ke supplier' untuk menanyakan langsung.",
                    {"action": "supplier_history_empty"})
        lines = [f"Kutipan harga dari {target['name']} (terbaru dulu):"]
        for ingredient_name, price, note, created_at in rows:
            price_text = f"Rp{price:,}".replace(",", ".") if price is not None else "?"
            note_text = f" ({note})" if note else ""
            lines.append(f"• {ingredient_name}: {price_text}{note_text} · {str(created_at or '')[:10]}")
        return ("\n".join(lines), {"action": "supplier_history", "supplier_id": target["id"]})

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
        same alias mapping as stock questions; every ingredient/price pair on
        a line is extracted ('telur 45rb cabai 30rb' → two quotes). A
        'per <unit>' phrase is normalized to the catalog unit when known,
        otherwise stored as the quote's note. Unparseable lines are reported
        to the owner, never guessed.
        """
        from .db import now
        units = {item["id"]: item.get("unit") for item in self._catalog_for(restaurant_id)["list"]}
        parsed_quotes = []
        unparsed = []
        # Ingredient matching opens its own short transactions: do it BEFORE
        # the write transaction so SQLite never nests BEGIN IMMEDIATE.
        def resolve(segment):
            if not str(segment or "").strip():
                return None
            return self._ingredient_from_text(None, restaurant_id, segment)

        for line in [line.strip() for line in str(text or "").splitlines() if line.strip()]:
            line_pairs, line_leftover = _pair_line_quotes(line, resolve)
            catalog_unit = units.get(line_pairs[0]["ingredient_id"]) if line_pairs else None
            if catalog_unit:
                line_pairs, line_leftover = _pair_line_quotes(line, resolve, catalog_unit)
            for pair in line_pairs:
                parsed_quotes.append({"ingredient_id": pair["ingredient_id"], "ingredient_name": pair["ingredient_name"],
                                      "price": pair["unit_price_idr"], "note": pair.get("note"), "line": line})
            unparsed.extend(text for text in line_leftover if text)
        with self.transaction() as conn:
            stored = []
            for quote in parsed_quotes:
                quote_id = uuid.uuid4().hex
                tc = self._tenant_col()
                self._inventory_query(conn, f"INSERT INTO supplier_price_quotes (id, {tc}, supplier_id, ingredient_id, ingredient_name, unit_price_idr, note, raw_message, source, inquiry_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reply', ?, ?)",
                                      (quote_id, restaurant_id, supplier_id, quote["ingredient_id"], quote["ingredient_name"], quote["price"], quote.get("note"), quote["line"][:300], inquiry_id, now()))
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
