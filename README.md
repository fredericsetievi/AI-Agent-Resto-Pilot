# RestoPilot

A local restaurant purchasing demo: Indonesian text or WhatsApp message → editable purchase draft → owner approval → persistent purchase, estimated stock, price evidence and activity.

## Run locally

Requires Python 3.11 or newer. From this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m service
```

Open **http://127.0.0.1:8000**. Data persists at `data/restopilot.sqlite3`. A first launch seeds synthetic purchase history, estimated opening stock and a Telur draft with a 16.3% price increase. Restarts retain data. Use a new database filename for a separate rehearsal:

```sh
.venv/bin/python -m service --db data/rehearsal-01.sqlite3 --port 8001
```

Use `--empty` with a new database path to omit sample purchases and stock. No reset command is provided.

## Current capabilities

- Indonesian purchase text parsing, editable drafts, owner approval/rejection and stock posting.
- Flexible ingredient catalog: the review dialog's ingredient field is searchable and accepts any typed name; unknown ingredients are added to the catalog when the purchase is approved, on the dashboard and over WhatsApp. Spelling slips close to a catalog name ("bwang putih") are recovered automatically and confirmed back in the draft reply before approval; names with no similar entry become new ingredients.
- Optional StepFun text rescue (`LLM_TEXT_EXTRACTION=1` with `STEPFUN_API_KEY`): free-form sentences, reordered clauses and voice-note transcripts that the rules parser cannot read get one model pass. Rules always run first; command words never reach the model, and any model error keeps the rules result.
- SQLite persistence, idempotency, version checks and activity records.
- WhatsApp simulator and Meta WhatsApp Cloud API webhook/send integration.
- Meta webhook verification, signature checks, account/phone-number validation and message deduplication.

Automated fake-provider contract tests cover the Meta request, response, webhook and signature behavior. A real Meta development-mode end-to-end test passed on October 20, 2026: inbound purchase text created a draft, the draft reply reached `delivered`, `YA` recorded exactly one purchase and inventory movement, and the confirmation reply reached `delivered`.

The live integration requires a WABA ID, Phone Number ID, access token, App Secret, verify token, public HTTPS origin and a subscribed `messages` webhook field. See [Meta WhatsApp setup and test guide](docs/META_WHATSAPP.md). The app binds to loopback; only the exact Meta webhook route is exposed to the configured public host. A quick-tunnel URL is temporary and must be replaced in both `.env` and Meta after it changes. The demo identity is not production authentication, and stock is an estimate.

## Rehearsal and project notes

See the [demo script and current capability notes](docs/DEMO.md), [roadmap](docs/ROADMAP.md), [Meta WhatsApp guide](docs/META_WHATSAPP.md), and [architecture](ARCHITECTURE.md). The API publishes `/openapi.json` and `/docs` while running.

The local milestone uses synchronous rules extraction and SQLite. Managed authentication, tenant isolation, PostgreSQL, private media storage, receipt OCR, durable background delivery and production deployment remain future work. Payment status remains separate from purchase approval.
