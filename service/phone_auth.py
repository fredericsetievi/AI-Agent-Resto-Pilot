"""Phone/password website accounts, independent of messaging transports.

A phone is a login identifier, not proof of phone possession. New sign-ups create
new tenants; existing tenants require existing credentials or admin review.
"""
import hashlib
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache


SQLITE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS login_phone_identities (phone TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE REFERENCES users(id), identity_version TEXT NOT NULL, approved_by TEXT NOT NULL REFERENCES users(id), approved_at TEXT NOT NULL, verified_at TEXT)",
    "CREATE TABLE IF NOT EXISTS login_phone_limits (key TEXT PRIMARY KEY, bucket_start BIGINT NOT NULL, hits INTEGER NOT NULL)",
)


def fail(code, message, status):
    from .db import DomainError
    raise DomainError(code, message, status)


def normalize_phone(value):
    """Indonesia is the default country; other countries require + or 00."""
    value = str(value or "").strip()
    if len(value) > 40 or not re.fullmatch(r"[+0-9 ()-]+", value, flags=re.ASCII):
        fail("invalid_login_phone", "Use an Indonesian mobile number (08…) or include + and the country code.", 422)
    phone = re.sub(r"[ ()-]", "", value)
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    elif phone.startswith("08"):
        phone = "+62" + phone[1:]
    elif phone.startswith("628"):
        phone = "+" + phone
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone, flags=re.ASCII):
        fail("invalid_login_phone", "Use an Indonesian mobile number (08…) or include + and the country code.", 422)
    if phone.startswith("+62") and not re.fullmatch(r"\+628[0-9]{7,11}", phone):
        fail("invalid_login_phone", "Use an Indonesian mobile number starting with 08 or +628.", 422)
    return phone


@lru_cache(maxsize=1)
def _dummy_hash():
    from .db import hash_password
    return hash_password(secrets.token_urlsafe(32))


class PhoneAuthMixin:
    def _phone_query(self, conn, sql, params=()):
        if self.inventory_postgres:
            return self._execute(conn, sql.replace("?", "%s"), params)
        return conn.execute(sql, params)

    def _phone_schema(self, conn):
        if self.inventory_postgres:
            row = self._phone_query(conn, "SELECT to_regclass('public.login_phone_identities'), to_regclass('public.login_phone_limits')").fetchone()
            if not all(row):
                fail("migration_required", "Phone login requires migration 0006_phone_login.sql.", 503)

    def _phone_locks(self, conn, keys):
        if self.inventory_postgres:
            for key in sorted(set(keys)):
                self._phone_query(conn, "SELECT pg_advisory_xact_lock(8726, hashtext(?))", (key,))

    def _phone_user(self, conn, user_id):
        if self.inventory_postgres:
            sql = "SELECT u.id,u.username,u.display_name,u.role,u.slug,r.name,u.password_hash FROM app_users u LEFT JOIN tenants t ON t.slug=u.slug LEFT JOIN restaurants r ON r.id=t.restaurant_id WHERE u.id=? FOR SHARE OF u"
        else:
            sql = "SELECT u.id,u.username,u.display_name,u.role,u.restaurant_id,r.name,u.password_hash FROM users u LEFT JOIN restaurants r ON r.id=u.restaurant_id WHERE u.id=?"
        row = self._phone_query(conn, sql, (user_id,)).fetchone()
        if not row:
            return None, None
        return dict(zip(("id", "username", "display_name", "role", "restaurant_id", "restaurant_name"), row[:6])), row[6]

    def _phone_limit(self, conn, key, timestamp, maximum):
        bucket = timestamp // 3600
        row = self._phone_query(conn, "SELECT bucket_start,hits FROM login_phone_limits WHERE key=?", (key,)).fetchone()
        hits = row[1] + 1 if row and row[0] == bucket else 1
        self._phone_query(conn, "INSERT INTO login_phone_limits (key,bucket_start,hits) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET bucket_start=excluded.bucket_start,hits=excluded.hits", (key, bucket, hits))
        return hits <= maximum

    def _phone_attempt(self, conn, phone, ip, purpose):
        timestamp = int(time.time())
        ip_digest = hashlib.sha256(ip.encode()).hexdigest()
        keys = ["password-phone:" + phone, "password-ip:" + ip_digest]
        if purpose == "register":
            keys.append("register-ip:" + ip_digest)
        self._phone_locks(conn, keys)
        self._phone_query(conn, "DELETE FROM login_phone_limits WHERE bucket_start < ?", (timestamp // 3600 - 1,))
        phone_ok = self._phone_limit(conn, keys[0], timestamp, 20)
        ip_ok = self._phone_limit(conn, keys[1], timestamp, 100)
        signup_ok = self._phone_limit(conn, keys[2], timestamp, 5) if purpose == "register" else True
        return phone_ok and ip_ok and signup_ok

    def _phone_allowed_user(self, conn, user_id, hosted, allow_demo_owner):
        from .db import verify_password
        user, password_hash = self._phone_user(conn, user_id)
        if not user or user["role"] != "owner" or not user["restaurant_id"] or not user["restaurant_name"]:
            return None
        if hosted and user["username"].casefold() == "owner" and not allow_demo_owner and verify_password("owner123", password_hash):
            return None
        return user

    def _phone_session(self, conn, user_id):
        from .db import now, SESSION_TTL
        token = secrets.token_urlsafe(32)
        sessions = "app_sessions" if self.inventory_postgres else "sessions"
        self._phone_query(conn, f"DELETE FROM {sessions} WHERE expires_at <= ?", (now(),))
        self._phone_query(conn, f"INSERT INTO {sessions} (token,user_id,created_at,expires_at) VALUES (?,?,?,?)", (token, user_id, now(), (datetime.now(timezone.utc) + SESSION_TTL).isoformat()))
        return token

    def _assign_phone(self, conn, user_id, phone, actor_id):
        from .db import now
        self._phone_query(conn, "DELETE FROM login_phone_identities WHERE user_id=?", (user_id,))
        self._phone_query(conn, "INSERT INTO login_phone_identities (phone,user_id,identity_version,approved_by,approved_at) VALUES (?,?,?,?,?)", (phone, user_id, uuid.uuid4().hex, actor_id, now()))

    def login_phone_password(self, phone, password, ip, *, hosted=False, allow_demo_owner=False):
        from .db import verify_password
        phone = normalize_phone(phone)
        result = None
        with self.transaction() as conn:
            self._phone_schema(conn)
            allowed = self._phone_attempt(conn, phone, ip, "login")
            if allowed:
                identity = self._phone_query(conn, "SELECT user_id FROM login_phone_identities WHERE phone=?", (phone,)).fetchone()
                user, stored_hash = self._phone_user(conn, identity[0]) if identity else (None, None)
                password_ok = verify_password(password, stored_hash or _dummy_hash())
                authorized = self._phone_allowed_user(conn, user["id"], hosted, allow_demo_owner) if user and password_ok else None
                if authorized:
                    result = authorized, self._phone_session(conn, user["id"])
        if not allowed:
            fail("phone_login_throttled", "Too many attempts. Please try again later.", 429)
        if not result:
            fail("invalid_credentials", "Unknown phone number or wrong password.", 401)
        return result

    def register_phone_account(self, phone, password, restaurant_name, display_name, ip):
        """Public registration creates only a new, empty, isolated restaurant."""
        from .db import hash_password, now, validate_password
        phone, password = normalize_phone(phone), validate_password(password)
        restaurant_name, display_name = restaurant_name.strip(), display_name.strip()
        if not restaurant_name or not display_name or max(len(restaurant_name), len(display_name)) > 200:
            fail("invalid_request", "Enter your name and restaurant name (up to 200 characters).", 422)
        result, conflict = None, False
        with self.transaction() as conn:
            self._phone_schema(conn)
            allowed = self._phone_attempt(conn, phone, ip, "register")
            if allowed:
                conflict = self._phone_query(conn, "SELECT 1 FROM login_phone_identities WHERE phone=?", (phone,)).fetchone() is not None
                if not conflict:
                    restaurant_id, user_id = uuid.uuid4().hex, uuid.uuid4().hex
                    username = "phone_" + uuid.uuid4().hex
                    if self.inventory_postgres:
                        cloud_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"restopilot:{restaurant_id}"))
                        self._phone_query(conn, "INSERT INTO restaurants (id,name,phone_number,address,created_at) VALUES (?,?,?,NULL,?)", (cloud_id, restaurant_name, "web+" + restaurant_id, now()))
                        self._phone_query(conn, "INSERT INTO tenants (slug,restaurant_id) VALUES (?,?)", (restaurant_id, cloud_id))
                        self._phone_query(conn, "INSERT INTO app_users (id,username,password_hash,display_name,role,slug) VALUES (?,?,?,?,'owner',?)", (user_id, username, hash_password(password), display_name, restaurant_id))
                    else:
                        self._phone_query(conn, "INSERT INTO restaurants (id,name,address,created_at) VALUES (?,?,NULL,?)", (restaurant_id, restaurant_name, now()))
                        self._phone_query(conn, "INSERT INTO users (id,username,password_hash,display_name,role,restaurant_id,created_at) VALUES (?,?,?,?,'owner',?,?)", (user_id, username, hash_password(password), display_name, restaurant_id, now()))
                    self._assign_phone(conn, user_id, phone, user_id)
                    self._audit(conn, "phone_account_created", "Website phone/password registration created a new restaurant workspace.", restaurant_id)
                    user, _ = self._phone_user(conn, user_id)
                    result = user, self._phone_session(conn, user_id)
        if not allowed:
            fail("phone_login_throttled", "Too many attempts. Please try again later.", 429)
        if conflict:
            fail("phone_account_conflict", "Unable to create this account. Try signing in or connecting your existing account.", 409)
        return result

    def connect_existing_phone(self, username, password, phone, ip, *, hosted=False, allow_demo_owner=False):
        """Authenticate an existing owner before adding their login identifier.

        Changing an already-enrolled number requires the admin's review path.
        No WhatsApp/Telegram links participate in authorization.
        """
        from .db import verify_password
        phone = normalize_phone(phone)
        result, conflict = None, False
        users = "app_users" if self.inventory_postgres else "users"
        with self.transaction() as conn:
            self._phone_schema(conn)
            # Serialize enrollment with the admin path before per-phone locks.
            self._phone_locks(conn, ["enrollment"])
            allowed = self._phone_attempt(conn, phone, ip, "connect")
            if allowed:
                row = self._phone_query(conn, f"SELECT id FROM {users} WHERE username=?", (username,)).fetchone()
                user, stored_hash = self._phone_user(conn, row[0]) if row else (None, None)
                password_ok = verify_password(password, stored_hash or _dummy_hash())
                authorized = self._phone_allowed_user(conn, user["id"], hosted, allow_demo_owner) if user and password_ok else None
                if authorized:
                    existing = self._phone_query(conn, "SELECT phone,user_id FROM login_phone_identities WHERE phone=? OR user_id=?", (phone, user["id"])).fetchall()
                    conflict = any(row[0] != phone or row[1] != user["id"] for row in existing)
                    if not conflict:
                        if not existing:
                            self._assign_phone(conn, user["id"], phone, user["id"])
                            self._audit(conn, "owner_phone_connected", "Existing owner connected a login phone using their account password.", user["restaurant_id"])
                        result = user, self._phone_session(conn, user["id"])
        if not allowed:
            fail("phone_login_throttled", "Too many attempts. Please try again later.", 429)
        if conflict:
            fail("phone_account_conflict", "This assignment needs administrator review. Existing accounts have not been changed.", 409)
        if not result:
            fail("invalid_credentials", "Unknown account or wrong password.", 401)
        return result

    def enroll_login_phone(self, restaurant_id, username, phone, admin_id):
        """Admin-reviewed assignment/recovery; never merges accounts."""
        phone = normalize_phone(phone) if phone else None
        users, tenant, sessions = ("app_users", "slug", "app_sessions") if self.inventory_postgres else ("users", "restaurant_id", "sessions")
        with self.transaction() as conn:
            self._phone_schema(conn)
            self._phone_locks(conn, ["enrollment"])
            row = self._phone_query(conn, f"SELECT id FROM {users} WHERE username=? AND {tenant}=? AND role='owner'", (username, restaurant_id)).fetchone()
            admin, _ = self._phone_user(conn, admin_id)
            if not row or not admin or admin["role"] != "admin":
                fail("not_found", "No owner with that username belongs to this restaurant.", 404)
            user_id = row[0]
            old = self._phone_query(conn, "SELECT phone FROM login_phone_identities WHERE user_id=?", (user_id,)).fetchone()
            phones = [p for p in (phone, old[0] if old else None) if p]
            # Use the same locks as password authentication and registration.
            self._phone_locks(conn, ["password-phone:" + p for p in phones])
            if phone:
                taken = self._phone_query(conn, "SELECT user_id FROM login_phone_identities WHERE phone=?", (phone,)).fetchone()
                if taken and taken[0] != user_id:
                    fail("login_phone_taken", "Revoke the existing assignment before assigning this phone to another owner.", 409)
            if (old[0] if old else None) == phone:
                return {"username": username, "phone": phone, "restaurant_id": restaurant_id}
            if phone:
                self._assign_phone(conn, user_id, phone, admin_id)
            else:
                self._phone_query(conn, "DELETE FROM login_phone_identities WHERE user_id=?", (user_id,))
            self._phone_query(conn, f"DELETE FROM {sessions} WHERE user_id=?", (user_id,))
            self._audit(conn, "owner_phone_enrolled" if phone else "owner_phone_revoked", f"Owner '{username}' login phone {'approved' if phone else 'revoked'} by administrator {admin_id}; sessions signed out.", restaurant_id)
        return {"username": username, "phone": phone, "restaurant_id": restaurant_id}

    def owner_login_phones(self, restaurant_id):
        users, tenant = ("app_users", "slug") if self.inventory_postgres else ("users", "restaurant_id")
        with self.transaction() as conn:
            self._phone_schema(conn)
            rows = self._phone_query(conn, f"SELECT u.username,p.phone,p.verified_at FROM {users} u LEFT JOIN login_phone_identities p ON p.user_id=u.id WHERE u.{tenant}=? AND u.role='owner' ORDER BY u.username", (restaurant_id,)).fetchall()
        return [{"username": row[0], "phone": row[1], "verified": row[2] is not None} for row in rows]
