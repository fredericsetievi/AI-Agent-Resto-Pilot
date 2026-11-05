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

**Published on 5 November 2026 as Atoms Version 13** on the existing
https://restopilot-dashboard.pub.atoms.world/ domain. This contains Version 11's
features, the translation fix and the restaurant-scoped purchase retry fix.
Application import: `89edc13cfa8e0246fc3e7625d230c65faae13dc1`.
Equivalent native packaging correction on main:
`d1f34bb9ee873afeea9433499aac95348d0f3ed5`.

The native plugin now exports `i18n.js` and registers all dashboard assets in the
Rollup build output. The final correction was made directly through the Code
editor and built with Submit all; no further Atoms AI prompt was used. Version
12's saved preview passed but publication exposed missing/stale static assets.
Version 13 and CDN cache expiry resolved the public asset checks. Public browser
checks confirmed Indonesian → English → Indonesian and persistence after reload.

No migration was required or applied. Hosted Supabase health/readiness,
signed-out inventory401, owner login and inventory readback passed. Existing
business-table fingerprints were preserved. No business entry or WhatsApp test
message was created during this release. A proposed content-hash URL enhancement
remains an ignored local patch and is not part of Version 13.

## User research

Two restaurants have been interviewed. Feedback is held by the team in text
conversations. The number of trial users and meaningful feedback responses still
needs to be confirmed against the mentor's targets of 20 users and 10 responses.
