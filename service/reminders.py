"""Daily reminder engine: morning/evening nudges, adjustable from chat.

Owners told us the two-app dashboard/Telegram split is friction and that the
core job is remembering to record purchases (and bring the right cash). The
reminder engine sends one morning and one evening nudge per restaurant per
day to every linked sender:

- Defaults 09:00 (record purchases) and 21:00 (evening recap) in the
  restaurant timezone (Asia/Jakarta).
- Quiet hours 22:00–07:00: hours outside 7–21 are rejected when set, and the
  engine itself never fires inside the band.
- Silence-aware: when the owner interacted recently (last inbound message
  within REMINDER_SILENCE_HOURS) the nudge is skipped — a present owner does
  not need reminding, and once they go quiet again the same-hour tick sends.

The settings live per restaurant in ``reminder_settings`` (migration 0009;
SQLite creates it at initialize). Commands are deterministic regex parses —
"pengingat pagi jadi jam 8", "pengingat malam jadi jam 9", "pengingat mati",
"pengingat nyala", "pengingat" for the current schedule. No model calls.

SQL runs through ``self._inventory_query`` so ``?`` placeholders work on
SQLite and Postgres alike; the tenant column is ``restaurant_id`` in SQLite
and ``slug`` in the hosted Postgres schema, resolved per query via
``_tenant_col()`` (same pattern as SupplierMixin).
"""

import re

SQLITE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS reminder_settings (restaurant_id TEXT PRIMARY KEY, "
    "morning_hour INTEGER NOT NULL DEFAULT 9, evening_hour INTEGER NOT NULL DEFAULT 21, "
    "enabled INTEGER NOT NULL DEFAULT 1, last_morning_date TEXT, last_evening_date TEXT, "
    "updated_at TEXT)",
)

DEFAULT_MORNING_HOUR = 9
DEFAULT_EVENING_HOUR = 21
# Quiet hours: the engine never sends between 22:00 and 07:00, and the chat
# command refuses to schedule an hour inside the band.
QUIET_START_HOUR = 22
QUIET_END_HOUR = 7
MIN_HOUR = QUIET_END_HOUR
MAX_HOUR = 21
# Aliases for the engine: the loop refuses to fire inside the quiet band even
# if a schedule row was edited outside the chat command's validation.
REMINDER_MIN_HOUR = MIN_HOUR
REMINDER_MAX_HOUR = MAX_HOUR
# Silence-aware skip: an inbound message this recent means the owner is at
# the chat right now and does not need the nudge.
REMINDER_SILENCE_HOURS = 6
# How often the reminder loop ticks (seconds). A one-hour slot match with a
# 60s tick sends within the first minute of the configured hour.
REMINDER_LOOP_SECONDS = 60

_REMINDER_RE = re.compile(r"(?i)^\s*pengingat\b(.*)$")
_SET_RE = re.compile(r"(?i)\b(?:jadi\s*)?jam\s*(\d{1,2})\b")
_SLOT_RE = re.compile(r"(?i)\b(pagi|siang|sore|malam)\b")
_OFF_RE = re.compile(r"(?i)\b(mati|berhenti|stop|non\s?aktif|off)\b")
_ON_RE = re.compile(r"(?i)\b(nyala|aktif|hidup|on)\b")

_SLOT_NAMES = {"morning": "pagi", "evening": "malam"}


def parse_reminder_command(text):
    """Detect a reminder command in one message. Returns a dict or None.

    Kinds: set (slot + hour), off, on, show, clarify (bare "pengingat jadi
    jam 8" — which slot?), quiet (hour inside quiet hours), invalid (hour
    outside 0–23). Deterministic rules only; never a purchase sentence.
    """
    text = str(text or "").strip()
    if len(text) > 200:
        return None
    match = _REMINDER_RE.match(text)
    if not match:
        return None
    remainder = match.group(1).strip()
    if not remainder or not _SET_RE.search(remainder):
        # "pengingat", "pengingat pagi", "pengingat jam berapa" → current schedule.
        if _OFF_RE.search(remainder):
            return {"kind": "off"}
        if _ON_RE.search(remainder):
            return {"kind": "on"}
        return {"kind": "show"}
    if _OFF_RE.search(remainder):
        return {"kind": "off"}
    if _ON_RE.search(remainder):
        return {"kind": "on"}
    slot_match = _SLOT_RE.search(remainder)
    hour_match = _SET_RE.search(remainder)
    hour = int(hour_match.group(1))
    if hour > 23:
        return {"kind": "invalid"}
    if slot_match is None:
        # "pengingat jadi jam 8" — morning or evening? Ask, never guess.
        return {"kind": "clarify"}
    slot = "evening" if slot_match.group(1).casefold() in ("sore", "malam") else "morning"
    if hour < 12 and slot_match.group(1).casefold() in ("sore", "malam"):
        hour += 12
    if not MIN_HOUR <= hour <= MAX_HOUR:
        return {"kind": "quiet"}
    return {"kind": "set", "slot": slot, "hour": hour}


def reminder_settings_text(settings):
    """The chat answer to "pengingat": the current schedule, in Indonesian."""
    if not settings["enabled"]:
        return ("Pengingat sedang mati. Kirim 'pengingat nyala' untuk mengaktifkan lagi "
                f"(pagi jam {settings['morning_hour']}, malam jam {settings['evening_hour']}).")
    return ("Pengingat aktif ⏰\n"
            f"- Pagi jam {settings['morning_hour']}: pengingat catat pembelian\n"
            f"- Malam jam {settings['evening_hour']}: rekap pembelian hari ini\n"
            "Ubah jadwal: 'pengingat pagi jadi jam 8' atau 'pengingat malam jadi jam 9'. "
            "Mati: 'pengingat mati'.")


def reminder_command_reply(cmd, settings):
    """The chat reply for a parsed reminder command.

    ``settings`` is the schedule AFTER the command was applied (for show it is
    the unchanged current one). Returns Indonesian text for every kind.
    """
    kind = cmd["kind"]
    if kind == "set":
        return f"Siap! Pengingat {_SLOT_NAMES[cmd['slot']]} sekarang jam {cmd['hour']}." \
            + " " + reminder_schedule_tail(settings)
    if kind == "off":
        return "Pengingat dimatikan. Kirim 'pengingat nyala' kalau mau diaktifkan lagi."
    if kind == "on":
        return "Pengingat dinyalakan lagi. " + reminder_schedule_tail(settings)
    if kind == "quiet":
        return ("Jam tidur 😴 Pengingat hanya bisa diatur antara jam 7 sampai jam 21. "
                "Contoh: 'pengingat pagi jadi jam 7' atau 'pengingat malam jadi jam 21'.")
    if kind == "invalid":
        return "Jamnya belum sesuai. Pakai format jam 0–23, contoh: 'pengingat pagi jadi jam 8'."
    if kind == "clarify":
        return ("Jam berapa? Sebutkan pagi atau malam ya — contoh: "
                "'pengingat pagi jadi jam 8' atau 'pengingat malam jadi jam 9'.")
    return reminder_settings_text(settings)


def reminder_schedule_tail(settings):
    if not settings["enabled"]:
        return "Pengingat sedang mati — kirim 'pengingat nyala' untuk mengaktifkan lagi."
    return f"Jadwal: pagi jam {settings['morning_hour']}, malam jam {settings['evening_hour']}."


def build_morning_reply():
    """Deterministic morning nudge — no model call, no invented figures."""
    return ("🌅 Selamat pagi! Jangan lupa catat pembelian hari ini di chat ini ya — "
            "kirim daftar belanjaannya, totalnya jadi patokan uang yang dibawa.")


def build_evening_reply():
    """Deterministic evening nudge; the cash envelope folds in later (V33.2)."""
    return ("🌙 Malam! Ada pembelian hari ini yang belum tercatat? Kirim sekarang "
            "sebelum tutup, atau balas 'ringkas' untuk rekap mingguan.")


class ReminderMixin:
    """Per-restaurant reminder schedule + sent-marker (both stores)."""

    def _tenant_col(self):
        return "slug" if self.inventory_postgres else "restaurant_id"

    _REMINDER_COLUMNS = "morning_hour, evening_hour, enabled, last_morning_date, last_evening_date"

    def _reminder_defaults(self):
        return {
            "morning_hour": DEFAULT_MORNING_HOUR,
            "evening_hour": DEFAULT_EVENING_HOUR,
            "enabled": True,
            "last_morning_date": None,
            "last_evening_date": None,
        }

    def reminder_settings(self, restaurant_id, conn=None):
        """The schedule for one restaurant, with defaults filled in. Read-only."""
        def read(connection):
            tc = self._tenant_col()
            row = self._inventory_query(
                connection, f"SELECT {self._REMINDER_COLUMNS} FROM reminder_settings WHERE {tc}=?",
                (restaurant_id,)).fetchone()
            settings = self._reminder_defaults()
            if row:
                settings.update({
                    "morning_hour": int(row[0]),
                    "evening_hour": int(row[1]),
                    "enabled": bool(row[2]),
                    "last_morning_date": row[3],
                    "last_evening_date": row[4],
                })
            return settings
        if conn is not None:
            return read(conn)
        with self.transaction() as connection:
            return read(connection)

    def set_reminder_settings(self, restaurant_id, *, morning_hour=None, evening_hour=None,
                              enabled=None, conn=None):
        """Update the schedule (None keeps the current value); upserts the row."""
        from .db import now

        def write(connection):
            settings = self.reminder_settings(restaurant_id, conn=connection)
            if morning_hour is not None:
                settings["morning_hour"] = int(morning_hour)
            if evening_hour is not None:
                settings["evening_hour"] = int(evening_hour)
            if enabled is not None:
                settings["enabled"] = bool(enabled)
            tc = self._tenant_col()
            self._inventory_query(
                connection,
                f"INSERT INTO reminder_settings ({tc}, {self._REMINDER_COLUMNS}, updated_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT ({tc}) DO UPDATE SET morning_hour=excluded.morning_hour, "
                "evening_hour=excluded.evening_hour, enabled=excluded.enabled, "
                "last_morning_date=excluded.last_morning_date, "
                "last_evening_date=excluded.last_evening_date, updated_at=excluded.updated_at",
                (restaurant_id, settings["morning_hour"], settings["evening_hour"],
                 bool(settings["enabled"]), settings["last_morning_date"],
                 settings["last_evening_date"], now()))
            return settings
        if conn is not None:
            return write(conn)
        with self.transaction() as connection:
            return write(connection)

    def mark_reminder_sent(self, restaurant_id, slot, day, conn=None):
        """Stamp today on the sent slot so the nudge fires once per day."""
        column = "last_morning_date" if slot == "morning" else "last_evening_date"

        def write(connection):
            settings = self.reminder_settings(restaurant_id, conn=connection)
            settings[column] = day
            tc = self._tenant_col()
            self._inventory_query(
                connection,
                f"INSERT INTO reminder_settings ({tc}, {self._REMINDER_COLUMNS}, updated_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT ({tc}) DO UPDATE SET {column}=excluded.{column}, updated_at=excluded.updated_at",
                (restaurant_id, settings["morning_hour"], settings["evening_hour"],
                 bool(settings["enabled"]), settings["last_morning_date"],
                 settings["last_evening_date"], day))
            return {"ok": True}
        if conn is not None:
            return write(conn)
        with self.transaction() as connection:
            return write(connection)

    def apply_reminder_command(self, conn, restaurant_id, cmd):
        """Chat command → schedule change + Indonesian reply. Deterministic."""
        kind = cmd["kind"]
        if kind in ("set", "off", "on"):
            settings = self.set_reminder_settings(
                restaurant_id, conn=conn,
                morning_hour=cmd.get("hour") if kind == "set" and cmd["slot"] == "morning" else None,
                evening_hour=cmd.get("hour") if kind == "set" and cmd["slot"] == "evening" else None,
                enabled=False if kind == "off" else (True if kind == "on" else None))
            return reminder_command_reply(cmd, settings), {"action": "reminder_updated"}
        # show / clarify / quiet / invalid leave the schedule untouched.
        settings = self.reminder_settings(restaurant_id, conn=conn)
        return reminder_command_reply(cmd, settings), {"action": "reminder_info"}
