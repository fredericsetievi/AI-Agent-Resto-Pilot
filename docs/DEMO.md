# RestoPilot demo and current evidence

## Five-minute local demonstration

Start with a new database and open http://127.0.0.1:8000:

```sh
.venv/bin/python -m service --db data/midterm-rehearsal.sqlite3
```

1. On **Overview**, review the seeded Telur draft: 3 kg, Rp150,000, Rp50,000/kg against synthetic same-supplier history of Rp43,000/kg (16.3% increase). No stock is posted yet.
2. In **WhatsApp demo**, show the `SIMULATED` label and send `beli cabai 3 kg 150rb`. The message creates a draft, not a purchase.
3. Reply `ya` to confirm the displayed details and receipt of goods. Show the Rp150,000 purchase and one stock movement. Payment remains unknown.
4. Send `ya` again to demonstrate replay protection. Refresh or restart with the same database to show persistence.
5. Send `beli beras` to show that missing quantity and price require correction. Correct it in the dashboard and confirm again, or send `batal` to reject.
6. Use **Activity** to show the draft and processing records.

For a second rehearsal, use a different database path. Existing database files are preserved.

## Provider status and test evidence

The current live transport is Meta WhatsApp Cloud API. The automated fake-provider contract tests cover webhook verification and signature/account checks, message handling, outbound request behavior and provider response handling. A real development-mode rehearsal passed on October 20, 2026: `beli bawang 2 kg 80rb` created the correct Rp80,000 draft, the draft reply was delivered, `YA` created exactly one purchase and 2 kg stock movement, and the confirmation was delivered. For another live rehearsal, use [the Meta setup and test guide](META_WHATSAPP.md).

The Business Portfolio ID, WABA ID and Phone Number ID are distinct values. A live test depends on the matching WABA ID, Phone Number ID, access token and App Secret, plus a verify token, public HTTPS origin and active `messages` webhook subscription.

## Implementation limits

The milestone uses SQLite, synchronous rules extraction and a local demo identity. Automated tests cover transaction, replay, rollback and restart behavior, but they do not establish production security, provider quality, usability or service-level objectives. Text extraction is deterministic; unsupported or incomplete details need human correction. Stock is an estimate and approval does not record payment.

Managed authentication, tenant isolation, PostgreSQL, private media storage, receipt OCR, durable background delivery, audited reversals and production deployment are future work. The five-item catalog and seeded purchase history are synthetic.
