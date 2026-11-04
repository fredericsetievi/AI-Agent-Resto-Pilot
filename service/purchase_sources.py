"""Tenant-scoped supplier-photo identity, committed alongside its draft."""

import re


SQLITE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS whatsapp_purchase_sources (restaurant_id TEXT NOT NULL, digest TEXT NOT NULL, draft_id TEXT NOT NULL, PRIMARY KEY(restaurant_id,digest))",
)


class PurchaseSourcesMixin:
    def _purchase_source_guard(self, conn, restaurant_id, source):
        """Return (digest, reply-or-None), without exposing another draft."""
        source = source if isinstance(source, dict) else {}
        digest = source.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            return None, {
                "reply": "Foto nota belum memiliki identitas yang dapat diperiksa. Kirim ulang foto atau rincian pembelian sebagai teks. Stok tidak berubah.",
                "result": {"action": "purchase_source_required"},
            }
        if self.inventory_postgres:
            available = self._inventory_query(conn, "SELECT to_regclass('public.whatsapp_purchase_sources')").fetchone()[0]
        else:
            available = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='whatsapp_purchase_sources'").fetchone()
        if not available:
            return None, {
                "reply": "Pemeriksaan nota ganda belum diaktifkan (migrasi 0005 diperlukan). Stok tidak berubah; gunakan rincian pembelian sebagai teks.",
                "result": {"action": "purchase_source_migration_required"},
            }
        if self.inventory_postgres:
            # Same ordering/key as operation approval: thread -> tenant -> stock.
            self._inventory_query(conn, "SELECT pg_advisory_xact_lock(hashtextextended(?,0))", ("whatsapp-operations:" + restaurant_id,))
        digest = digest.lower()
        prior = self._inventory_query(
            conn, "SELECT draft_id FROM whatsapp_purchase_sources WHERE restaurant_id=? AND digest=?",
            (restaurant_id, digest),
        ).fetchone()
        if prior:
            # Only inspect status; never return/attach this draft to the sender.
            draft = self._load_draft(conn, prior[0], restaurant_id)
            status = draft["status"]
            if status == "recorded":
                detail = "sudah tercatat"
            elif status == "rejected":
                detail = "sudah dibatalkan"
            else:
                detail = "masih menunggu pemeriksaan dan konfirmasi"
            return digest, {
                "reply": "Foto nota yang sama " + detail + ". Tidak dibuat pembelian baru dan stok tidak ditambah lagi. Jika perlu koreksi, kirim rincian koreksi sebagai teks atau nota baru yang berbeda.",
                "result": {"action": "purchase_source_duplicate", "source_status": status},
            }
        return digest, None

    def _purchase_source_register(self, conn, restaurant_id, digest, draft_id):
        self._inventory_query(
            conn, "INSERT INTO whatsapp_purchase_sources(restaurant_id,digest,draft_id) VALUES (?,?,?)",
            (restaurant_id, digest, draft_id),
        )
