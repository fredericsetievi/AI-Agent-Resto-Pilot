"""Purchase corrections from chat: 'ubah pembelian' and 'batal pembelian'.

A recorded purchase can be corrected or voided from the chat channel that
recorded it. The flow mirrors the stock-count proposals: the owner's
message parses into a proposal that is stored on the sending thread, the
assistant replies with the exact before/after, and only an explicit YA
applies it — BATAL discards it. Deltas are applied as compensating stock
movements and every correction is audited.

Targeting: an 8-character purchase id is the precise way
(`ubah pembelian a1b2c3d4 jadi cabai 3 kg 20rb`). Without an id the
remainder is resolved against the restaurant's recent purchases with
plain words — ingredient names, a quantity, `kemarin`/`hari ini` — and
an ambiguous match answers with the candidate list instead of guessing.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .extraction import extract_purchase

_EDIT_RE = re.compile(r"(?i)^\s*(ubah|edit|batal)\s+pembelian\b\s*[:,-]?\s*(.*)$")
_ID8 = re.compile(r"^([0-9a-f]{8})(?:\b\s*(.*)|(\s*))?$", re.IGNORECASE)
_QUANTITY = re.compile(r"(?i)(\d+(?:[.,]\d+)?)\s*(kg|gr|g|ons|l|ml|pcs|bh|buah|bungkus|pack|paket|ikat|kaleng|botol|tray)?")
_DATE_HINTS = {"kemarin lusa": 2, "kemarin": 1, "hari ini": 0}
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_EDIT_STOPWORDS = {"pembelian", "nota", "beli", "dari", "untuk", "yang", "itu", "ke", "kepada", "salah", "catat", "tercatat"}
_MAX_TARGET_TEXT = 8000

VOID_COMMANDS = {"batal"}


def parse_purchase_edit_command(text):
    """Return {kind, id8, remainder} for 'ubah/batal pembelian ...', else None."""
    if not isinstance(text, str) or len(text) > 4000:
        return None
    match = _EDIT_RE.match(text.strip())
    if not match:
        return None
    word, remainder = match.group(1).casefold(), (match.group(2) or "").strip()
    kind = "void" if word in VOID_COMMANDS else "edit"
    id8, rest = None, remainder
    id_match = _ID8.match(remainder)
    if id_match:
        id8 = id_match.group(1).lower()
        rest = (id_match.group(2) or "").strip()
    return {"kind": kind, "id8": id8, "remainder": rest[:_MAX_TARGET_TEXT]}


def _normalize_word(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def parse_target_hints(remainder):
    """Extract the plain-language hints: a date, quantities, and name words."""
    text = str(remainder or "").strip()
    days_back = None
    lowered = text.casefold()
    for hint, days in _DATE_HINTS.items():
        if hint in lowered:
            days_back = days
            lowered = lowered.replace(hint, " ")
            break
    iso = _ISO_DATE.search(text)
    if iso:
        days_back = iso.group(1)
    quantities = []
    def _capture(quantity_match):
        raw = quantity_match.group(1).replace(",", ".")
        try:
            quantities.append(Decimal(raw))
        except InvalidOperation:
            pass
        return " "
    lowered = _QUANTITY.sub(_capture, lowered)
    words = [_normalize_word(word) for word in lowered.split()]
    names = [word for word in words if word and word not in _EDIT_STOPWORDS]
    return {"days_back": days_back, "quantities": quantities, "names": names}


def summarize_items(items):
    lines = []
    for item in items:
        quantity = item.get("canonical_quantity") or item.get("quantity") or "?"
        unit = item.get("canonical_unit") or item.get("unit") or ""
        name = item.get("raw_name") or ""
        lines.append(f"• {name} — {quantity} {unit}".rstrip())
    return "\n".join(lines)


def _item_display_name(item, catalog):
    ingredient = catalog.get("by_id", {}).get(item.get("ingredient_id"))
    return (ingredient or {}).get("name") or item.get("raw_name") or "?"


def _deltas_between(old_items, new_items):
    """Per-ingredient quantity change: new − old (Decimal)."""
    deltas = {}
    for item in old_items:
        ingredient = item.get("ingredient_id")
        if ingredient:
            deltas[ingredient] = deltas.get(ingredient, Decimal(0)) - Decimal(item.get("canonical_quantity") or 0)
    for item in new_items:
        ingredient = item.get("ingredient_id")
        if ingredient:
            deltas[ingredient] = deltas.get(ingredient, Decimal(0)) + Decimal(item.get("canonical_quantity") or 0)
    return deltas


def _format_deltas(deltas, catalog):
    lines = []
    for ingredient, delta in deltas.items():
        if delta == 0:
            continue
        name = (catalog.get("by_id", {}).get(ingredient) or {}).get("name") or ingredient
        sign = "+" if delta > 0 else ""
        lines.append(f"{name} {sign}{delta}")
    return ", ".join(lines) or "tidak ada perubahan stok"


class PurchaseEditsMixin:
    """Chat-driven corrections of recorded purchases (both stores).

    SQL runs through ``self._inventory_query`` so ``?`` placeholders work
    on SQLite and Postgres alike, exactly like the inventory operations.
    """

    def _edit_purchase_rows(self, conn, tenant, limit=30):
        rows = self._inventory_query(
            conn,
            "SELECT id, supplier, created_at, body, voided_at FROM purchases "
            "WHERE restaurant_id=? AND voided_at IS NULL ORDER BY created_at DESC, id DESC LIMIT ?",
            (tenant, limit),
        ).fetchall()
        purchases = []
        for purchase_id, supplier, created_at, body, _voided in rows:
            try:
                purchase = json.loads(body) if isinstance(body, (str, bytes)) else dict(body)
            except ValueError:
                continue
            purchase.update({"id": purchase_id, "supplier": supplier, "created_at": created_at})
            purchases.append(purchase)
        return purchases

    def _resolve_edit_target(self, conn, tenant, remainder):
        """Return (purchase, None) | (None, options_reply_text) for plain words."""
        purchases = self._edit_purchase_rows(conn, tenant)
        hints = parse_target_hints(remainder)
        catalog = self._catalog_for(tenant)
        target_date = None
        if isinstance(hints["days_back"], str):
            target_date = hints["days_back"]
        elif hints["days_back"] is not None:
            target_date = (datetime.now(timezone.utc) - timedelta(days=hints["days_back"])).date().isoformat()

        scored = []
        for purchase in purchases:
            purchase_date = str(purchase.get("created_at") or "")[:10]
            date_hit = target_date is None or purchase_date == target_date
            if target_date is not None and not date_hit:
                continue
            names_hit = 0
            qty_hit = False
            for item in purchase.get("items") or []:
                display = _item_display_name(item, catalog).casefold()
                for name in hints["names"]:
                    if name and (name in _normalize_word(display) or _normalize_word(display) in name and len(_normalize_word(display)) > 2):
                        names_hit += 1
                        break
                try:
                    if any(Decimal(str(item.get("canonical_quantity") or item.get("quantity") or 0)) == q for q in hints["quantities"]):
                        qty_hit = True
                except InvalidOperation:
                    pass
            score = names_hit * 3 + (2 if qty_hit else 0) + (2 if date_hit and target_date is not None else 0)
            if score > 0:
                scored.append((score, purchase))
        if scored:
            best = max(score for score, _ in scored)
            winners = [purchase for score, purchase in scored if score == best]
            if len(winners) == 1:
                return winners[0], None
        recent = "\n".join(
            f"• {purchase['id'][:8]} · {str(purchase.get('created_at') or '')[:10]} · "
            + (purchase.get("supplier") or "tanpa pemasok") + " · "
            + ", ".join(f"{item.get('raw_name')} {item.get('canonical_quantity') or item.get('quantity')}{item.get('canonical_unit') or item.get('unit') or ''}" for item in (purchase.get("items") or [])[:3])
            for purchase in purchases[:5]
        ) or "(belum ada pembelian tercatat)"
        return None, ("Pembelian yang dimaksud tidak jelas. Pembelian terbaru:\n" + recent
                      + "\n\nUlangi dengan kode 8 digitnya, misalnya: ubah pembelian a1b2c3d4 jadi cabai 3 kg.")

    def _edit_load_purchase(self, conn, tenant, purchase_id):
        row = self._inventory_query(
            conn,
            "SELECT id, supplier, created_at, body, voided_at FROM purchases "
            "WHERE restaurant_id=? AND id LIKE ? AND voided_at IS NULL",
            (tenant, purchase_id + "%"),
        ).fetchone()
        if not row:
            return None
        purchase = json.loads(row[3]) if isinstance(row[3], (str, bytes)) else dict(row[3])
        purchase.update({"id": row[0], "supplier": row[1], "created_at": row[2]})
        return purchase

    def _purchase_edit_review_text(self, proposal, catalog):
        header = f"Usulan {'pembatalan' if proposal['kind'] == 'void' else 'perubahan'} pembelian {proposal['purchase_id'][:8]} · {str(proposal['purchase_created_at'])[:10]}"
        lines = [header]
        if proposal["kind"] == "void":
            lines.append(summarize_items(proposal["old_items"]))
            lines.append("Stok akan dikurangi kembali sesuai catatan awal.")
        else:
            old_map = {item.get("ingredient_id"): item for item in proposal["old_items"]}
            for item in proposal["new_items"]:
                name = _item_display_name(item, catalog)
                old = old_map.get(item.get("ingredient_id"))
                if old:
                    old_qty = old.get("canonical_quantity") or old.get("quantity")
                    lines.append(f"• {name}: {old_qty} {old.get('canonical_unit') or old.get('unit') or ''} → {item.get('canonical_quantity')} {item.get('canonical_unit') or ''}".rstrip())
                else:
                    lines.append(f"• + {name} — {item.get('canonical_quantity')} {item.get('canonical_unit') or ''}")
            removed = [old for ingredient, old in old_map.items() if ingredient not in {item.get("ingredient_id") for item in proposal["new_items"]}]
            for item in removed:
                lines.append(f"• − {_item_display_name(item, catalog)} (dihapus)")
            supplier_line = f"Supplier: {proposal.get('new_supplier') or proposal.get('old_supplier') or 'tanpa pemasok'}"
            lines.append(supplier_line)
            lines.append("Stok akan disesuaikan otomatis mengikuti perubahan.")
        lines.append("Balas YA untuk terapkan, atau BATAL untuk batal.")
        return "\n".join(lines)

    def propose_purchase_edit(self, conn, thread, tenant, command, text):
        """Resolve the target and store the pending proposal on the thread.

        Returns (reply, result). Never applies anything: only YA does.
        """
        from .db import encode, now
        purchase = None
        options = None
        if command.get("id8"):
            purchase = self._edit_load_purchase(conn, tenant, command["id8"])
            if purchase is None:
                return ("Pembelian dengan kode " + command["id8"] + " tidak ditemukan (sudah dibatalkan, atau milik resto lain). "
                        "Cek daftar pembelian di dashboard.", {"action": "edit_target_missing"})
        elif command.get("remainder"):
            purchase, options = self._resolve_edit_target(conn, tenant, command["remainder"])
            if purchase is None:
                return (options, {"action": "edit_target_ambiguous"})
        else:
            return ("Sebutkan pembelian yang diubah: kode 8 digitnya (contoh: ubah pembelian a1b2c3d4 jadi cabai 3 kg) "
                    "atau cukup katanya (contoh: ubah pembelian cabai 5kg kemarin menjadi 3 kg).",
                    {"action": "edit_target_missing"})

        if command["kind"] == "void":
            proposal = {"kind": "void", "purchase_id": purchase["id"], "purchase_created_at": purchase.get("created_at"),
                        "old_items": purchase.get("items") or [], "old_supplier": purchase.get("supplier"),
                        "old_total_idr": purchase.get("total_idr"), "version": 1}
        else:
            remainder = command.get("remainder") or ""
            change_text = remainder
            if command.get("id8"):
                # 'ubah pembelian a1b2c3d4 jadi cabai 3 kg' → strip the id prefix words.
                change_text = re.sub(r"(?i)^\s*(menjadi|jadi)\b\s*", "", remainder).strip()
            if not change_text.strip():
                return ("Sebutkan perubahannya, contoh: ubah pembelian " + purchase["id"][:8]
                        + " jadi cabai 3 kg 90rb (atau: batal pembelian " + purchase["id"][:8] + ").",
                        {"action": "edit_change_missing"})
            extracted = extract_purchase(change_text)
            items, _warnings = self._normalize(extracted.get("items") or [], self._catalog_for(tenant))
            old_map = {item.get("ingredient_id"): item for item in (purchase.get("items") or [])}
            for item in items:
                # A replacement line without a price keeps the recorded unit
                # price of the line it replaces ("menjadi 3 kg" ≠ a re-price).
                if not item.get("line_total_idr"):
                    old = old_map.get(item.get("ingredient_id"))
                    old_unit = (old or {}).get("unit_price_idr")
                    if old_unit:
                        try:
                            quantity = Decimal(item.get("canonical_quantity") or 0)
                            item["unit_price_idr"] = old_unit
                            item["line_total_idr"] = int((Decimal(old_unit) * quantity).to_integral_value())
                        except InvalidOperation:
                            pass
            if not items:
                return ("Perubahan tidak terbaca sebagai item pembelian. Contoh format: jadi cabai 3 kg 90rb.",
                        {"action": "edit_change_unreadable"})
            proposal = {"kind": "edit", "purchase_id": purchase["id"], "purchase_created_at": purchase.get("created_at"),
                        "old_items": purchase.get("items") or [], "old_supplier": purchase.get("supplier"),
                        "old_total_idr": purchase.get("total_idr"),
                        "new_items": items, "new_supplier": extracted.get("supplier"), "version": 1}
        thread["pending_edit"] = encode(proposal)
        catalog = self._catalog_for(tenant)
        return (self._purchase_edit_review_text(proposal, catalog),
                {"action": "purchase_edit_proposed", "purchase_edit": proposal})

    def apply_pending_purchase_edit(self, conn, thread, tenant, decision):
        """Apply (YA) or discard (BATAL) the pending proposal on this thread.

        Applying is transaction-safe by construction: the caller runs inside
        the ingest transaction, so a retry replays the stored response
        instead of double-applying.
        """
        from .db import DomainError, draft_total, now
        import uuid
        proposal = json.loads(thread["pending_edit"]) if thread.get("pending_edit") else None
        if not proposal:
            return ("Tidak ada usulan perubahan pembelian yang menunggu.", {"action": "no_pending_edit"})
        thread["pending_edit"] = None
        if decision == "reject":
            return ("Usulan perubahan pembelian dibatalkan. Pembelian dan stok tidak berubah.",
                    {"action": "purchase_edit_cancelled"})
        purchase = self._edit_load_purchase(conn, tenant, proposal["purchase_id"])
        if purchase is None:
            return ("Pembelian tidak lagi tersedia (mungkin sudah dibatalkan lewat jalur lain). Usulan dihentikan.",
                    {"action": "edit_target_missing"})
        catalog = self._catalog_for(tenant)
        if proposal["kind"] == "void":
            # Void: reverse exactly what the original approval added.
            new_items, deltas = [], _deltas_between(proposal["old_items"], [])
        else:
            new_items = [dict(item) for item in proposal["new_items"]]
            pseudo = {"items": [dict(item) for item in new_items]}
            self._resolve_new_ingredients(conn, pseudo, tenant)
            new_items = pseudo["items"]
            deltas = _deltas_between(proposal["old_items"], new_items)
        # Compensating stock movements: the difference between the recorded
        # items and the corrected ones, applied per ingredient.
        for ingredient, delta in deltas.items():
            row = self._inventory_query(conn, "SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id=?",
                                        (tenant, ingredient)).fetchone()
            stock = Decimal(row[0]) if row and row[0] is not None else Decimal(0)
            new_quantity = stock + delta
            if row:
                self._inventory_query(conn, "UPDATE inventory SET quantity=? WHERE restaurant_id=? AND ingredient_id=?",
                                      (str(new_quantity), tenant, ingredient))
            else:
                self._inventory_query(conn, "INSERT INTO inventory (restaurant_id, ingredient_id, quantity) VALUES (?, ?, ?)",
                                      (tenant, ingredient, str(new_quantity)))
            self._inventory_query(conn, "INSERT INTO movements (id, purchase_id, ingredient_id, quantity, created_at, restaurant_id) VALUES (?, NULL, ?, ?, ?, ?)",
                                  (uuid.uuid4().hex, ingredient, str(delta), now(), tenant))
        if proposal["kind"] == "void":
            body = dict(purchase)
            body["voided"] = True
            self._inventory_query(conn, "UPDATE purchases SET body=?, voided_at=? WHERE id=? AND restaurant_id=?",
                                  (json.dumps(body, ensure_ascii=False), now(), purchase["id"], tenant))
            self._audit(conn, "purchase_voided",
                        f"Purchase {purchase['id'][:8]} voided from chat; stock reversed ("
                        + _format_deltas(deltas, catalog) + ").", tenant)
            return ("Pembelian " + purchase["id"][:8] + " dibatalkan. Stok dikurangi kembali ("
                    + _format_deltas(deltas, catalog) + ").",
                    {"action": "purchase_voided", "purchase_id": purchase["id"]})
        body = dict(purchase)
        body.update({"items": new_items})
        body["total_idr"] = draft_total(new_items, [])
        if proposal.get("new_supplier"):
            body["supplier"] = proposal["new_supplier"]
        self._inventory_query(conn, "UPDATE purchases SET body=?, supplier=? WHERE id=? AND restaurant_id=?",
                              (json.dumps(body, ensure_ascii=False), body.get("supplier"), purchase["id"], tenant))
        self._audit(conn, "purchase_edited",
                    f"Purchase {purchase['id'][:8]} edited from chat ("
                    + _format_deltas(deltas, catalog) + "); stock adjusted.", tenant)
        return ("Pembelian " + purchase["id"][:8] + " diperbarui. Stok disesuaikan ("
                + _format_deltas(deltas, catalog) + ").",
                {"action": "purchase_edited", "purchase_id": purchase["id"]})
