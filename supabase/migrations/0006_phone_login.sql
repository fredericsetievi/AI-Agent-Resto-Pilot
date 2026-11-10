-- Explicit, additive phone-login migration. No existing users, passwords,
-- WhatsApp links, sessions, tenants or business records are changed/enrolled.
CREATE TABLE IF NOT EXISTS login_phone_identities (
    phone text PRIMARY KEY,
    user_id uuid NOT NULL UNIQUE REFERENCES app_users(id),
    identity_version text NOT NULL,
    approved_by uuid NOT NULL REFERENCES app_users(id),
    approved_at timestamptz NOT NULL,
    verified_at timestamptz
);
CREATE TABLE IF NOT EXISTS login_phone_limits (
    key text PRIMARY KEY,
    bucket_start bigint NOT NULL,
    hits integer NOT NULL
);
ALTER TABLE login_phone_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_phone_limits ENABLE ROW LEVEL SECURITY;
-- Phone/password login requires no delivery provider. verified_at stays null
-- unless a future explicit phone-possession verification is implemented.
-- No client policies. Only the existing privileged application backend connects.
