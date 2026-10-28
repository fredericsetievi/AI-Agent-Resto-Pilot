# RestoPilot demo and current evidence

## Five-minute local demonstration

Start with a new database and open http://127.0.0.1:8000:

```sh
.venv/bin/python -m service --db data/midterm-rehearsal.sqlite3
```

Log in as `owner / owner123` (resto owner) — the credentials are printed at startup.

1. On **Overview**, review the seeded Telur draft: 3 kg, Rp150,000, Rp50,000/kg against synthetic same-supplier history of Rp43,000/kg (16.3% increase). No stock is posted yet.
2. In **WhatsApp demo**, show the `SIMULATED` label and send `beli cabai 3 kg 150rb`. The message creates a draft, not a purchase.
3. Reply `ya` to confirm the displayed details and receipt of goods. Show the Rp150,000 purchase and one stock movement. Payment remains unknown.
4. Send `ya` again to demonstrate replay protection. Refresh or restart with the same database to show persistence.
5. Send `beli beras` to show that missing quantity and price require correction. Correct it in the dashboard and confirm again, or send `batal` to reject.
6. Use **Activity** to show the draft and processing records.

For a second rehearsal, use a different database path. Existing database files are preserved.

## Multi-tenant and login demonstration

Continue in the same session (log out with **Keluar** first):

1. Log out and log in as `admin / admin123`. The sidebar now shows **Restaurants**; the owner pages are hidden.
2. Add a new restaurant (name, owner username/password, first WhatsApp number, e.g. `628999000777`). Its owner login works immediately.
3. Open the new restaurant's detail: edit info, add and remove WhatsApp numbers, and show the rejected-attempts panel after step 5 below. Note the read-only dashboard — the admin cannot approve drafts.
4. Log in as the new restaurant's owner: an empty, isolated dashboard. The WhatsApp simulator offers only that restaurant's linked numbers.
5. From an unregistered number (live webhook or a message from any number the admin has not linked): the sender receives *"Nomor ini belum terdaftar…"*, nothing is processed, and the attempt appears in the admin console. Registering the number in the admin console makes the next message flow into that restaurant's drafts.
6. Back as the demo owner, the drafts, inventory, and activity are untouched by the second restaurant — no cross-tenant visibility anywhere.

## Provider status and test evidence

The current live transport is Meta WhatsApp Cloud API. The automated fake-provider contract tests cover webhook verification and signature/account checks, message handling, outbound request behavior and provider response handling. A real development-mode rehearsal passed on October 20, 2026: `beli bawang 2 kg 80rb` created the correct Rp80,000 draft, the draft reply was delivered, `YA` created exactly one purchase and 2 kg stock movement, and the confirmation was delivered. For another live rehearsal, use [the Meta setup and test guide](META_WHATSAPP.md).

The Business Portfolio ID, WABA ID and Phone Number ID are distinct values. A live test depends on the matching WABA ID, Phone Number ID, access token and App Secret, plus a verify token, public HTTPS origin and active `messages` webhook subscription.

## Implementation limits

The milestone uses SQLite, synchronous rules extraction and a local demo identity. Automated tests cover transaction, replay, rollback and restart behavior, but they do not establish production security, provider quality, usability or service-level objectives. Text extraction is deterministic; unsupported or incomplete details need human correction. Stock is an estimate and approval does not record payment.

Session login with owner/admin roles, per-restaurant tenant isolation, and the WhatsApp number allowlist are implemented (schema v2, with in-place migration of v1 files). PostgreSQL, private media storage, receipt OCR, durable background delivery, audited reversals and production deployment remain future work. Authentication is demo-grade: HttpOnly SameSite cookies over loopback, no HTTPS enforcement, CSRF tokens, or rate limiting. The five-item catalog and seeded purchase history are synthetic.
