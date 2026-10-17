# RestoPilot: local milestone and rehearsal

Built on October 17, 2026 from the seven supplied project documents. This is the first runnable local milestone, not the full provider-connected capstone MVP. The current targets are a midterm progress demo on October 29, 2026 and the flagship MVP on November 19, 2026. See `docs/ROADMAP.md`.

## Five-minute midterm demonstration

Start `.venv/bin/python -m service --db data/midterm-rehearsal.sqlite3` with a new database filename and open http://127.0.0.1:8000. A new database has two synthetic approved purchases, estimated opening inventory and one pending Telur draft. Explain the visible local-demo and simulated labels before presenting.

1. Open **Overview**. Review the Telur draft: 3 kg, Rp150,000, Rp50,000/kg versus the synthetic same-supplier Rp43,000/kg baseline. The calculated increase is 16.3%. No new stock has posted yet.
2. Open **WhatsApp demo**. Point out the explicit `SIMULATED` badge, then send `beli cabai 3 kg 150rb`. Show the persistent inbound message and the structured Rp150,000 draft summary. No purchase or stock change exists yet.
3. Reply `ya`. Explain that this explicitly means the summarized details are correct and the goods were received. Open **Overview** after the refresh completes: purchases today is Rp150,000, recorded purchases is 3, and Cabai stock changed exactly once from 3 kg to 6 kg. Payment remains unknown.
4. Return to **WhatsApp demo** and send `ya` again. It reports no active draft and cannot duplicate the purchase. Refresh or restart with the same database to show that the conversation, purchase, activity and stock survive.
5. Send `beli beras`. It remains a draft needing details instead of inventing quantity or price. Open its dashboard review, correct the fields, and explain why a dashboard edit requires a fresh `ya`. Alternatively use `batal` to show rejection without a stock change.
6. Open **Activity**. Show the draft and transport processing records. If the live Twilio path has passed the rehearsal gate, send the purchase and `ya` from the joined phone and show their effects in the dashboard. State that the current Trial does not deliver dynamic TwiML replies; the simulator is the honest two-way presentation fallback.

For a second rehearsal, use a new path, e.g. `python3 -m service --db data/rehearsal-02.sqlite3 --port 8001`. Existing database files are preserved. Tests use temporary databases and do not change the demo database.

## Actual capabilities

| Capability | Current evidence | Remaining work |
|---|---|---|
| Local purchase and review UI | Implemented HTML/CSS/JavaScript; complete Safari desktop flow visually verified | Mobile/presenter rehearsal with the project team |
| Text extraction | Bounded rules parser with synthetic regression tests | Expand and evaluate against the team’s redacted/synthetic Indonesian examples |
| Purchase and stock posting | SQLite transactions; concurrent decisions, replay, rollback and restart tests | PostgreSQL migrations and equivalent real-database race tests |
| Identity | Fixed local demo identity; staff-role denial tested as a simulation | Managed authentication, memberships, two-tenant security matrix |
| Price comparison | Decimal arithmetic, same catalog ingredient/unit/supplier within 30 days; comparison retained on approval | Real supplier/grade/pack settings and reviewed receipt corpus |
| Dashboard updates | Reads after writes and five-second polling; Safari message→stock update verified | Failure/reconnection rehearsal and presenter timing |
| WhatsApp simulator | Persistent sender-correlated messages and decisions; restart and duplicate tests | Use as the dependable midterm fallback |
| Twilio testing webhook | Official signature validation, exact public route, dedupe and media fallback verified through a temporary public tunnel; real joined-phone purchase plus `ya` passed | Rehearse again before presenting because the trial session and quick-tunnel URL are temporary; Trial does not support direct dynamic TwiML replies |
| Photos and voice | Not implemented | Private media, bounded extraction, correction and quality evaluation |
| Atoms / StepFun | Removed from active scope by the team | None unless the course later makes either mandatory |

No synthetic checks establish real-provider quality, production security, user usability or SLOs. No held-out OCR accuracy or cost claims are made.

## Implementation choices for this milestone

This milestone uses SQLite and a static browser client served by FastAPI so the complete text/approval loop can run using installed dependencies. It intentionally implements synchronous rules extraction; no background-job or outbound-delivery durability is claimed. The simulator and Twilio endpoint call the same transaction-safe purchase workflow. A signed inbound request proves Twilio transport authenticity, but the current demo does not provide production phone-owner enrollment or multi-restaurant authentication.

All quantity and price calculations are server-side Decimal operations. Browser number formatting is presentation only. Unsupported packs, unknown ingredients, omitted amounts, ambiguous tax or discount language, and too many lines require correction. The five-item catalog is synthetic and is not a general restaurant alias mapping.

Approvals create one purchase and one movement per distinct ingredient in a transaction; repeated ingredient lines are aggregated for the stock movement. The local idempotency namespace is global for the fixed demo workspace, not the future multi-tenant actor/operation namespace. A retry with the same key/body returns the original result; a changed body conflicts. Recorded drafts are immutable; audited reversals and counts are a later milestone, so use synthetic data only.

## Next delivery milestones

1. **Rehearse the completed live Twilio path.** Reconnect the trial phone if needed, start a fresh tunnel, update `PUBLIC_BASE_URL` and the Console callback together, then repeat purchase plus `ya`. Keep the simulator for dynamic two-way replies while the account remains Trial.
2. **Apply the team’s examples.** Turn the supplied Indonesian messages into named fixtures, measure parser behavior, and add aliases or explicit review rules instead of silent guesses.
3. **Prepare the shared backend.** PostgreSQL schema/migrations, managed identity/memberships, private storage and a durable jobs/outbox worker. Port concurrent approval and rollback tests; add two-tenant access tests.
4. **Add receipt photos for the full MVP.** Private media ingestion, bounded extraction, retained source evidence and a mandatory correction/approval step; report fixture and live-provider results separately.
5. **Harden and rehearse.** Audited reversal and stock counts, worker restart/retry tests, browser/mobile QA, backup restore and two-user task rehearsal. Freeze features before the confirmed dates.

These are ordered against the confirmed October 29 midterm and November 19 flagship-MVP dates. Voice, marketing, supplier outreach, payments, and POS connectors remain outside the first live purchase-loop demo.

## Operational notes

- Stop with Ctrl+C; restart with the same `--db` path to retain records.
- `/health/live` and `/health/ready` report process/database readiness, not provider readiness.
- The CLI binds `127.0.0.1`. With Twilio configuration, only the exact signed POST webhook accepts the configured public host; all dashboard/API routes remain loopback-only. These controls do not constitute production authentication.
- Unknown pending draft totals are `null`, never a guessed Rp0. Purchases today uses the restaurant's Asia/Jakarta day.
- Approval requires current version, valid fields, and explicit received-goods confirmation. Conflicts require reopening the draft.
- The single-workspace read model loads all drafts and purchases, with the latest 100 activity entries. Add pagination before scaling.
- To back up a demo database, stop the server and copy its `.sqlite3` file. Production backup/restore has not been implemented or rehearsed.
- The supplied report refers to `restopilot-backlog.csv`, `documentation-probes.json`, and `local-protocol-poc-results.json`. Those files were not present in this checkout; earlier reported probe results have not been treated as current test evidence.
