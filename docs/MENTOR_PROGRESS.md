# RestoPilot mentor progress

Updated 5 November 2026.

## Restaurant data separation

**Status: Done — implementation and automated acceptance checks.**

Owner dashboards and linked WhatsApp conversations use their restaurant's own
drafts, purchases, stock, catalog, recipes, operational records and message
history. Cross-restaurant access is rejected. Purchase retry caches are now
scoped to the restaurant, including safe ownership checks before replaying
responses created by older releases. Existing owners retain their valid retries.

Verification for this release:

- 407 Python tests passed; 44 opt-in PostgreSQL/live tests skipped in the offline run.
- All 40 disposable local PostgreSQL tests passed separately, including nine
  purchase retry/isolation cases and existing ledger/concurrency checks.
- The dedicated tenant-isolation API module passed all 19 tests, including eight
  new text/photo/edit/approval retry regressions and a real catalog assertion.
- 26 JavaScript checks passed, including a packaging contract requiring each
  dashboard script and stylesheet to be exported by the Atoms adapter.

These checks used isolated synthetic data. WhatsApp restaurant self-selection
links a sender; it does not independently verify restaurant ownership. A real
multi-restaurant WhatsApp field trial remains separate user validation.

## WhatsApp to dashboard release

Version 11's main features are prepared for publication with two corrections:
the Atoms translation asset is registered for preview/build, and purchase retry
responses cannot cross restaurants. No schema migration is needed. Publication
status and the final saved version will be recorded after public checks pass.

## User research

Two restaurants have been interviewed. Feedback is held by the team in text
conversations. The number of trial users and meaningful feedback responses still
needs to be confirmed against the mentor's targets of 20 users and 10 responses.
