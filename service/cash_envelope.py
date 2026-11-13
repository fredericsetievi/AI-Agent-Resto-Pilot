"""Daily cash envelope: budget brought to the market, change returned, gap.

WBC buys cake-market stock with physical cash: the owner takes a daily
envelope, preorders come in via the (Telegram-pilot) chat, and most days a
little cash comes back unused. The dashboard ledger records purchases, but
nothing answers "how much money is still unaccounted for?". This mixin keeps
one row per restaurant per WIB day:

- ``envelope_idr``  — the cash taken along ("budget hari ini 2 juta").
- ``leftover_idr``  — the change that came back ("uang kembali 500 ribu").
- unrecorded spend  — envelope − recorded purchases today − leftover;
  reported only, never posted as a stock change.

Commands are deterministic regex parses handled in the ingest chain right
after the reminder commands (a budget sentence can never be a purchase line,
and it must run even while a purchase draft waits). Money parsing reuses the
suppliers' Indonesian money parser ("2 juta", "500rb", "Rp50.000").

SQL runs through ``self._inventory_query`` so ``?`` placeholders work on
SQLite and Postgres alike; the tenant column is ``restaurant_id`` in SQLite
and ``slug`` in the hosted Postgres schema, resolved via ``_tenant_col()``
(same pattern as ReminderMixin).
"""

import json
import re
from datetime import datetime

from .suppliers import _money_idr

SQLITE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cash_envelopes (restaurant_id TEXT NOT NULL, day TEXT NOT NULL, "
    "envelope_idr INTEGER, leftover_idr INTEGER, recorded_at TEXT NOT NULL, "
    "PRIMARY KEY (restaurant_id, day))",
)

MAX_IDR = 1_000_000_000_000  # same bound the dashboards accept for purchase totals

_BUDGET_RE = re.compile(r"(?i)^\s*(budget|anggaran)\b(.*)$")
_LEFTOVER_RE = re.compile(r"(?i)^\s*(?:uang\s+)?kembali(?:an)?\b(.*)$")


def parse_envelope_command(text):
    """Detect a cash-envelope command in one message. Returns a dict or None.

    Kinds: set ("budget hari ini 2 juta"), leftover ("uang kembali 500 ribu"),
    show ("budget", "budget?"), invalid (unusable amount). The leftover
    command requires an amount — a bare "kembali" sentence is never hijacked.
    """
    text = str(text or "").strip()
    if len(text) > 200:
        return None
    leftover = _LEFTOVER_RE.match(text)
    if leftover:
        amount = _money_idr(leftover.group(1))
        if amount is None or amount <= 0 or amount > MAX_IDR:
            return None  # "kembali" alone stays a normal message
        return {"kind": "leftover", "amount": amount}
    budget = _BUDGET_RE.match(text)
    if not budget:
        return None
    amount = _money_idr(budget.group(2))
    if amount is None or amount <= 0:
        # "budget", "budget?", "anggaran hari ini" → report the day's status.
        return {"kind": "show"}
    if amount > MAX_IDR:
        return {"kind": "invalid"}
    return {"kind": "set", "amount": amount}


def _rp(amount):
    """Indonesian money format: dot thousand separators (matches suppliers)."""
    return f"Rp{amount:,}".replace(",", ".")


def cash_envelope_status_text(status):
    """The chat answer to "budget": today's envelope, recorded spend, gap."""
    if not status["envelope_idr"]:
        return ("Belum ada budget kas hari ini. Kirim 'budget hari ini 2 juta' untuk mulai, "
                "lalu habis belanja kirim 'uang kembali <jumlah>'.")
    lines = [f"💰 Kas hari ini:",
             f"- Budget: {_rp(status['envelope_idr'])}",
             f"- Tercatat: {_rp(status['recorded_idr'])} ({status['purchase_count']} pembelian)"]
    if status["leftover_idr"] is None:
        lines.append("- Kembali: belum ada")
    else:
        lines.append(f"- Kembali: {_rp(status['leftover_idr'])}")
    if status["unrecorded_idr"] > 0:
        lines.append(f"- Belum tercatat: {_rp(status['unrecorded_idr'])}")
    elif status["unrecorded_idr"] < 0:
        lines.append(f"- Melebihi budget: {_rp(-status['unrecorded_idr'])}")
    else:
        lines.append("- Semua pembelian sudah tercatat 👍")
    return "\n".join(lines)


class CashEnvelopeMixin:
    """Per-restaurant, per-day cash envelope (both stores)."""

    _ENVELOPE_COLUMNS = "envelope_idr, leftover_idr, recorded_at"

    def _envelope_row(self, conn, restaurant_id, day):
        tc = self._tenant_col()
        return self._inventory_query(
            conn, f"SELECT {self._ENVELOPE_COLUMNS} FROM cash_envelopes WHERE {tc}=? AND day=?",
            (restaurant_id, day)).fetchone()

    def cash_envelope(self, restaurant_id, day, conn=None):
        """The envelope row for one WIB day; None amounts mean not recorded yet."""
        def read(connection):
            row = self._envelope_row(connection, restaurant_id, day)
            return {"day": day, "envelope_idr": row[0] if row else None,
                    "leftover_idr": row[1] if row else None,
                    "recorded_at": row[2] if row else None}
        if conn is not None:
            return read(conn)
        with self.transaction() as connection:
            return read(connection)

    def set_cash_envelope(self, restaurant_id, day, *, envelope_idr=None, leftover_idr=None,
                          conn=None):
        """Set the envelope or the leftover (None keeps the current value); upsert."""
        from .db import now

        def write(connection):
            current = self.cash_envelope(restaurant_id, day, conn=connection)
            if envelope_idr is not None:
                current["envelope_idr"] = int(envelope_idr)
            if leftover_idr is not None:
                current["leftover_idr"] = int(leftover_idr)
            current["recorded_at"] = now()
            tc = self._tenant_col()
            self._inventory_query(
                conn=connection,
                sql=(f"INSERT INTO cash_envelopes ({tc}, day, {self._ENVELOPE_COLUMNS}) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT (" + tc + ", day) DO UPDATE SET "
                     "envelope_idr=excluded.envelope_idr, leftover_idr=excluded.leftover_idr, "
                     "recorded_at=excluded.recorded_at"),
                params=(restaurant_id, day, current["envelope_idr"],
                        current["leftover_idr"], current["recorded_at"]))
            return current
        if conn is not None:
            return write(conn)
        with self.transaction() as connection:
            return write(connection)

    def recorded_purchase_total(self, restaurant_id, day=None, conn=None):
        """Non-void purchase total (and count) recorded on one WIB day.

        Reads the same ``body.total_idr`` figure the dashboards chart; purchases
        whose created-at WIB date matches ``day`` count. Report only — this
        never touches the inventory ledger.
        """
        from .db import RESTAURANT_TIMEZONE

        if day is None:
            day = datetime.now(RESTAURANT_TIMEZONE).date().isoformat()

        def read(connection):
            tc = self._tenant_col()
            rows = self._inventory_query(
                connection,
                f"SELECT created_at, body FROM purchases WHERE {tc}=? AND voided_at IS NULL",
                (restaurant_id,)).fetchall()
            total, count = 0, 0
            for row in rows:
                created = row[0]
                if not isinstance(created, datetime):
                    created = datetime.fromisoformat(str(created))
                if created.astimezone(RESTAURANT_TIMEZONE).date().isoformat() != day:
                    continue
                body = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
                amount = body.get("total_idr")
                if isinstance(amount, int) and not isinstance(amount, bool) and 0 < amount <= MAX_IDR:
                    total += amount
                    count += 1
            return {"total_idr": total, "purchase_count": count}
        if conn is not None:
            return read(conn)
        with self.transaction() as connection:
            return read(connection)

    def cash_envelope_status(self, restaurant_id, day=None, conn=None):
        """Envelope + recorded spend + the unrecorded gap, for chat and dashboard.

        ``day`` defaults to today in the restaurant timezone (Asia/Jakarta).
        """
        from .db import RESTAURANT_TIMEZONE

        if day is None:
            day = datetime.now(RESTAURANT_TIMEZONE).date().isoformat()

        def read(connection):
            envelope = self.cash_envelope(restaurant_id, day, conn=connection)
            recorded = self.recorded_purchase_total(restaurant_id, day, conn=connection)
            unrecorded = None
            if envelope["envelope_idr"] is not None:
                unrecorded = envelope["envelope_idr"] - recorded["total_idr"]
                if envelope["leftover_idr"] is not None:
                    unrecorded -= envelope["leftover_idr"]
            return {"day": day, "envelope_idr": envelope["envelope_idr"],
                    "leftover_idr": envelope["leftover_idr"], "recorded_at": envelope["recorded_at"],
                    "recorded_idr": recorded["total_idr"], "purchase_count": recorded["purchase_count"],
                    "unrecorded_idr": unrecorded}
        if conn is not None:
            return read(conn)
        with self.transaction() as connection:
            return read(connection)

    def apply_envelope_command(self, conn, restaurant_id, cmd):
        """Chat command → envelope change + Indonesian reply. Deterministic."""
        from .db import RESTAURANT_TIMEZONE

        kind = cmd["kind"]
        day = datetime.now(RESTAURANT_TIMEZONE).date().isoformat()
        if kind == "invalid":
            return ("Nominalnya belum sesuai. Contoh: 'budget hari ini 2 juta' atau "
                    "'uang kembali 500 ribu'.", {"action": "envelope_rejected"})
        if kind == "set":
            self.set_cash_envelope(restaurant_id, day, envelope_idr=cmd["amount"], conn=conn)
            return (f"Siap! Budget kas hari ini {_rp(cmd['amount'])}. Habis belanja, "
                    "kirim 'uang kembali <jumlah>' ya.", {"action": "envelope_set"})
        if kind == "leftover":
            self.set_cash_envelope(restaurant_id, day, leftover_idr=cmd["amount"], conn=conn)
            status = self.cash_envelope_status(restaurant_id, day, conn=conn)
            reply = f"Dicatat: uang kembali {_rp(cmd['amount'])}."
            if status["envelope_idr"] is not None and status["unrecorded_idr"] > 0:
                reply += f" Belum tercatat hari ini: {_rp(status['unrecorded_idr'])}."
            return reply, {"action": "leftover_set"}
        return cash_envelope_status_text(self.cash_envelope_status(restaurant_id, day, conn=conn)), \
            {"action": "envelope_info"}
