# RestoPilot

A local working demo of the restaurant purchase loop: Indonesian quick log or WhatsApp-style message → editable draft → owner approval → persistent purchase and estimated stock → price evidence and activity.

## Run locally

Requires Python 3.11 or newer. From this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m service
```

Open **http://127.0.0.1:8000**. The environment used to build this demo already has the required packages, so `python3 -m service` also works here.

Data persists at `data/restopilot.sqlite3`. The first launch seeds synthetic purchase history, estimated opening stock, and a Telur draft with a **16.3%** price increase. Restarts do not reset data. To rehearse separately, use a new database filename:

```sh
python3 -m service --db data/rehearsal-01.sqlite3 --port 8001
```

Use `--empty` with a **new** database path to start with the ingredient catalog but no sample purchases or stock. No reset command is provided. Preserve previous databases if they contain records you want to keep.

## What works

- Text such as `beli cabai 3 kg 150rb`, decimal-comma quantities, grams/kilograms and milliliters/liters.
- Missing fields, unknown ingredients and unsupported packs stay unresolved for human correction.
- Review, add/remove/correct lines, reject, or approve after confirming goods received.
- Atomic purchase, stock movement, audit and idempotency records; version checks and replay protection.
- Overview, approvals, purchases, inventory, and activity backed by the same SQLite database.
- Comparable same-supplier history within 30 days; exact arithmetic and preserved approval evidence.
- Persistent WhatsApp simulator: purchase note, correlated `ya`/`batal`, replay protection, dashboard correction, and restart-safe conversation history.
- Live-ready Twilio webhook with official signature validation, strict public-route scoping, TwiML preparation, sender/channel isolation, and a safe text fallback for media.
- Trial-account polling fallback that imports only inbound WhatsApp records from Twilio's Messages API, reuses the same transaction/idempotency path, and never claims or attempts an outbound reply.

The dashboard labels simulated and live behavior explicitly. Extraction uses deterministic rules; Atoms and StepFun are not part of the active scope. **A real joined-phone → Twilio → signed webhook → draft → `ya` → recorded purchase round trip has passed.** OCR, voice, managed login, PostgreSQL, and production deployment are not implemented. The demo identity and phone-to-owner mapping are not production authentication. The launcher binds to loopback; when configured, only the exact signed Twilio webhook may accept the configured public host. Stock is an estimate, and purchase approval does not mark a payment as made.

## Optional Twilio testing

The team has confirmed access to **Try out WhatsApp**, joined a phone to the trial, and configured the trial's **Receive a message → Custom** HTTP POST callback. A temporary public tunnel passed signed fixture, route-isolation, and real joined-phone tests. The phone sent `beli bawang 2 kg 80rb` followed by `ya`; RestoPilot recorded exactly one Rp80,000 purchase and stock movement. The trial still does not support direct dynamic TwiML replies, so the simulator remains the honest two-way conversation. The included poller is a rehearsal fallback if the temporary callback or tunnel is unavailable.

Run the dashboard and poller against the exact same database. Polling credentials are read only from the process environment; they are not command-line arguments or `.env` values. Set `TWILIO_WHATSAPP_TO` to the trial recipient to restrict both Twilio's query and RestoPilot's local filter:

```sh
export TWILIO_ACCOUNT_SID='AC...'
read -s TWILIO_AUTH_TOKEN && export TWILIO_AUTH_TOKEN
export TWILIO_WHATSAPP_TO='whatsapp:+<trial-recipient-number>'

.venv/bin/python -m service --db data/twilio-demo.sqlite3
# In a second terminal, deterministically import current inbound history once:
.venv/bin/python -m service.twilio_poller --db data/twilio-demo.sqlite3 --once
# Or poll continuously for the live demo (allowed interval: 2-300 seconds):
.venv/bin/python -m service.twilio_poller --db data/twilio-demo.sqlite3 --interval 5
```

The poller never sends a Twilio message. See [the Twilio setup guide](docs/TWILIO.md) for safe setup, filtering, idempotency, and trial limitations. If a webhook-capable sender becomes available, `.env.example` documents the separate signed-webhook configuration.

## Verify and rehearse

```sh
.venv/bin/python -m pytest -q
node --check frontend/app.js
node --test tests/frontend.test.cjs
```

See [the demo script and next milestones](docs/DEMO.md), [dated delivery roadmap](docs/ROADMAP.md), [Twilio setup guide](docs/TWILIO.md), and [current architecture](ARCHITECTURE.md). The API exposes its local OpenAPI schema at `/openapi.json` and interactive documentation at `/docs` while running.

The dependency pins reproduce the installed environment tested for this local milestone. Review and upgrade dependencies, implement managed authentication and tenant isolation, and verify PostgreSQL concurrency before any public deployment.

The supplied audit and source documents remain in the repository. Their earlier audit-only status describes the previous assignment; construction was authorized in the current request. Current delivery targets are October 29, 2026 for the midterm progress demo and November 19, 2026 for the full flagship MVP.
