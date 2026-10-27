# RestoPilot

A local restaurant purchasing demo: Indonesian text or WhatsApp message → editable purchase draft → owner approval → persistent purchase, estimated stock, price evidence and activity.

## Run locally

Requires Python 3.11 or newer. From this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m service
```

Open **http://127.0.0.1:8000**. Data persists at `data/restopilot.sqlite3`. Log in at the prompt — the demo credentials are printed at startup:

- `owner / owner123` — resto owner: the full dashboard for Warung Bu Sari.
- `admin / admin123` — platform admin: manages restaurants, their owner logins, and each restaurant's linked WhatsApp numbers; every restaurant's dashboard is visible read-only.

A first launch seeds the demo restaurant with its two logins, the WhatsApp number `628123456789`, synthetic purchase history, estimated opening stock and a Telur draft with a 16.3% price increase. Restarts retain data; existing v1 databases migrate in place into the demo tenant. Use a new database filename for a separate rehearsal:

```sh
.venv/bin/python -m service --db data/rehearsal-01.sqlite3 --port 8001
```

Use `--empty` with a new database path to omit sample purchases and stock. No reset command is provided.

## Storage: Supabase Postgres (authority) with SQLite fallback

When `SUPABASE_URL` and `SUPABASE_DB_PASSWORD` are set (see `.env.example`), everything — logins, sessions, restaurants, drafts, purchases, stock, WhatsApp threads — lives in your Supabase Postgres project, and `/health/ready` reports `"storage": "supabase"`. The schema lives in `supabase/migrations/` (`0003_authority_postgres.sql` creates the authority tables; run it in the Supabase SQL Editor if your project predates it).

To copy an existing local SQLite file into Supabase once:

```sh
.venv/Scripts/python -m service.migrate_to_pg --db data/restopilot.sqlite3
```

The SQLite file is kept untouched. Without the password set, the app falls back to the local SQLite file so it still boots offline. Tests stay hermetic: they only touch Supabase when run with `TEST_PG=1` (see `tests/test_pg_store.py`).

## Login roles and multi-tenant isolation

Every restaurant is a tenant with its own drafts, purchases, inventory, ingredient catalog, price baselines and activity. An owner only ever sees their own restaurant; the admin console can view any restaurant's dashboard but cannot log, edit, or approve purchases — those decisions stay with the owner.

WhatsApp numbers are linked to exactly one restaurant (the `whatsapp_links.phone` column is unique). A message from an unregistered number is rejected with a polite Indonesian reply; nothing is processed, no draft or model call happens, and the attempt appears in the admin console. A registered number's conversation — drafts, `YA` approvals, summaries — is scoped to its linked restaurant only, so a number can never read or ask about another restaurant's data. When the optional StepFun models are invoked (text rescue, invoice vision), the prompts carry an explicit tenant rule naming the sender's restaurant as defense in depth; the actual isolation is the allowlist plus per-tenant storage, and the model never receives another restaurant's content.

Sessions are HttpOnly `SameSite=Lax` cookies (7-day expiry, stored server-side so restarts keep logins). Owner passwords can be reset by the admin, which signs out that owner's sessions.

## Current capabilities

- Indonesian purchase text parsing, editable drafts, owner approval/rejection and stock posting.
- Flexible ingredient catalog: the review dialog's ingredient field is searchable and accepts any typed name; unknown ingredients are added to the catalog when the purchase is approved, on the dashboard and over WhatsApp. Spelling slips close to a catalog name ("bwang putih") are recovered automatically and confirmed back in the draft reply before approval; names with no similar entry become new ingredients.
- Optional StepFun text rescue (`LLM_TEXT_EXTRACTION=1` with `STEPFUN_API_KEY`): free-form sentences, reordered clauses and voice-note transcripts that the rules parser cannot read get one model pass. Rules always run first; command words never reach the model, and any model error keeps the rules result.
- SQLite persistence, idempotency, version checks and activity records.
- WhatsApp simulator and Meta WhatsApp Cloud API webhook/send integration.
- Meta webhook verification, signature checks, account/phone-number validation and message deduplication.

Automated fake-provider contract tests cover the Meta request, response, webhook and signature behavior. A real Meta development-mode end-to-end test passed on October 20, 2026: inbound purchase text created a draft, the draft reply reached `delivered`, `YA` recorded exactly one purchase and inventory movement, and the confirmation reply reached `delivered`.

The live integration requires a WABA ID, Phone Number ID, access token, App Secret, verify token, public HTTPS origin and a subscribed `messages` webhook field. See [Meta WhatsApp setup and test guide](docs/META_WHATSAPP.md). The app binds to loopback; only the exact Meta webhook route is exposed to the configured public host. A quick-tunnel URL is temporary and must be replaced in both `.env` and Meta after it changes. This remains demo-grade authentication (no HTTPS enforcement, no CSRF tokens, no rate limiting), and stock is an estimate.

## Rehearsal and project notes

See the [demo script and current capability notes](docs/DEMO.md), [roadmap](docs/ROADMAP.md), [Meta WhatsApp guide](docs/META_WHATSAPP.md), and [architecture](ARCHITECTURE.md). The API publishes `/openapi.json` and `/docs` while running.

The local milestone uses synchronous rules extraction and SQLite. Multi-user session login, tenant isolation, and the WhatsApp number allowlist are implemented; PostgreSQL, private media storage, receipt OCR, durable background delivery and production deployment remain future work. Payment status remains separate from purchase approval.
