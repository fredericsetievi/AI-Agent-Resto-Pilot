# Website phone-number accounts

Current scope, 2026-11-10: owners create an account and log in on the website
using **phone number + password**. This supersedes the earlier OTP proposal.
No OTP, SMS provider, Twilio configuration, WhatsApp decision or Telegram decision
is required. No separate mobile application/version was created.

Published on 2026-11-10 as **Atoms V19**, from application source
`6c166cadf9f196d597eb49e12252b79a832cbc21`, on the existing
[website](https://restopilot-dashboard.pub.atoms.world/app.html). Migration 0006
was applied once and public verification passed. See the
[release record](RELEASE_2026-11-10_PHONE_LOGIN.md). Existing unrelated
uncommitted documentation/handoffs and all branches remain intact.

## Website flow

The main sign-in form accepts phone and password. **Create restaurant account**
adds the owner's name, restaurant name and password confirmation. Successful
signup creates a new empty restaurant, its owner account, the unique phone
mapping and a server session in one database transaction, then opens that
restaurant dashboard. Both forms support Indonesian and English, loading/errors,
password-manager autocomplete and duplicate-submit protection.

Signup cannot select an existing restaurant or submit a role/user ID. Restaurant
names can coincide, but each signup has a distinct restaurant ID and only its own
business data. Duplicate/retried registration cannot create another account for
the same normalized phone. It returns a conflict directing the user to sign in
or connect their existing account. There are no startup seeds for new hosted
accounts, stock balances, sample purchases or automatic messaging links.

Existing owners expand **Existing account / admin login**, enter their existing
username/password, and optionally enter a login phone. Correct existing owner
credentials connect the number to that same user/restaurant and open the existing
dashboard. User IDs, passwords, roles, restaurant IDs, business records and prior
recovery access are retained. Leaving the phone empty keeps ordinary legacy
login. Platform administrators continue using legacy login and cannot enroll
as phone-login owners.

Changing a number already enrolled to an owner, or a conflict with another
account's phone assignment, requires administrator review. No accounts are merged
automatically. Admin → restaurant → **Owner login phone** shows current numbers
and lets an administrator explicitly review and assign/revoke the mapping.
Changing/revoking it signs out that owner's sessions; repeating the same PUT is
a no-op. Administrators can reset the existing account password through the
existing password-reset flow; that same password is used for phone login.

## Authority and messaging separation

A phone number is a login identifier, and the password authenticates the account.
It is not proof of phone possession: `verified_at` remains null. This is deliberate
under the user's choice to use phone/password before choosing message delivery.
A number registered before its rightful owner's enrollment may require admin
review; there is no phone-code-based recovery or unauthenticated remapping.

New signup grants authority only over the new restaurant it creates. Existing
restaurant authority requires its existing owner's credentials or explicit admin
review. A WhatsApp restaurant link, self-selected restaurant, phone string or
Telegram chat ID never grants access to an existing dashboard.

Website login phones and messaging sender links remain separate. Signup and
phone enrollment neither create nor transfer WhatsApp/Telegram links. Existing
open messaging onboarding, callbacks, test-account settings and sender links are
preserved. Deciding WhatsApp versus Telegram later will not block or alter this
website login. The previous optional Twilio/OTP adapter, routes, UI and configuration
were removed during implementation; no delivery integration remains
half-configured.

## Security and storage

- Phone normalization accepts Indonesian `08…`, `628…`, `+628…`, `00628…` and
  display separators. Other countries require explicit `+`/`00` country codes.
  Ambiguous bare national numbers, Unicode digits, extensions and Telegram IDs
  are rejected. Equivalent formats share one unique identity.
- Passwords use the existing salted PBKDF2 hashing and validation (8–200 characters
  for signup). No passwords are stored in phone metadata, audit events, retries
  or logs. Unknown phone and wrong password return the same 401 response.
- Database-backed counters bound phone-auth attempts to 20 per phone and 100 per
  client IP per fixed hour, with signup limited to 5 attempts per IP per fixed
  hour. Failed/conflicting attempts also count. Counters survive restarts and
  old buckets are pruned. IPs use the ASGI client address, not caller-supplied
  forwarded headers. A shared platform proxy address conservatively shares the
  IP allowance; verify the existing proxy's trusted-address behavior at release.
- SQLite uses its existing `BEGIN IMMEDIATE` transaction discipline. PostgreSQL
  uses ordered advisory locks for phone/IP enrollment and registration. Unique
  constraints prevent duplicate/competing phone assignments across workers.
  Signup/session failures roll back the new tenant, user and phone assignment.
  Authentication locks the PostgreSQL user row so a racing password reset cannot
  leave an old-password session active after the reset commits.
- All authorization still resolves the existing server user/restaurant. Sessions
  retain HttpOnly, SameSite=Lax, `/`, seven-day expiry and Secure in hosted mode.
  Logout, restart persistence and password-reset session invalidation remain.
- Hosted default demo restrictions also apply to phone login and connecting an
  existing account. The default-password `owner` requires the existing approved
  `ALLOW_DEMO_OWNER_LOGIN=1` setting; default admin credentials remain blocked.
  Hosted storage stays fail-closed with no SQLite fallback or startup migration.

## Schema and endpoints

The additive [0006_phone_login.sql](../supabase/migrations/0006_phone_login.sql)
adds only `login_phone_identities` and `login_phone_limits`, both RLS-enabled with
no client policies. It enrolls nobody and changes no existing users, tenants,
passwords, business records or messaging links. SQLite adds equivalent empty
tables locally. Hosted phone endpoints return `migration_required` 503 until the
explicit additive migration is applied during a separately authorized release;
legacy authentication remains available. Migration 0006 was applied and verified
on the existing Supabase project during the V19 release; no earlier migration
was replayed.

The migration was revised before any live application to reflect the switch
from OTP to phone/password. Any local database created during the prior OTP
implementation can retain its unused challenge table; the new source neither
reads it nor drops it. `approved_by` records the authorizing user: the new owner
for signup, the existing authenticated owner for connecting their first phone,
or the reviewing administrator for managed changes. It does not assert that a
number was independently verified.

- `POST /v1/auth/phone/register`: `{phone, password, restaurant_name, display_name}`
  → 201, existing-format public user and session cookie for a new tenant.
- `POST /v1/auth/phone/login`: `{phone, password}` → existing-format public user
  and session cookie for that account's restaurant.
- `POST /v1/auth/phone/connect`: `{username, password, phone}` → connects an
  existing owner's first phone after credential validation and opens their
  existing restaurant. Conflicting/replacement assignments require admin review.
- Admin-only `GET /v1/admin/restaurants/{id}/owner-phones`: owner assignments.
- Admin-only `PUT /v1/admin/restaurants/{id}/owner-phone`:
  `{username, phone: string|null, ownership_reviewed: true}`.
- Existing `/v1/auth/login`, `/me`, `/logout` and owner password reset retain
  their existing contracts. There are no OTP request/verify endpoints.

## Local checks

`tests/test_phone_auth.py` covers signup/login, legacy transition, no automatic
WhatsApp authority, normalization, wrong/unknown credentials, protected session
cookies, restart/expiry/logout, duplicate/concurrent signup, rate limits,
controlled assignments, password reset, hosted demo restrictions, tenant
isolation and rejecting arbitrary existing-tenant/role claims.

`tests/test_phone_auth_postgres.py` uses the fixture documented in
`tests/test_inventory_postgres.py`: an explicit local DSN, cloud hosts rejected,
and a disposable database created/dropped for the test module. It covers actual
UUID schemas, atomic signup, concurrency/conflicts, reviewed reassignment,
rollback, durable limits, password-reset races and missing migration behavior.
No legacy live Supabase tests were enabled and no real messages were sent.

Frontend tests cover signup/login payloads, confirmation, double submission,
connecting an existing owner, localized errors, password clearing and form-mode
validation. A local desktop Chromium check exercises the website against a
temporary database with external browser requests blocked.

Final results: full Python suite **559 passed, 63 skipped**; disposable local
PostgreSQL phone-auth suite **10 passed**; JavaScript frontend/Atoms checks
**37 passed**; desktop Chromium and `git diff --check` passed. Existing dependency
deprecation warnings remain. Temporary test servers were stopped, and the website
test database was removed. These were the local implementation results before
deployment. The subsequent V19 deployment and public checks are recorded in the release record; no real
messages were sent.
