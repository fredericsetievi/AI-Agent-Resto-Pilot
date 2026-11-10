# Website phone/password release — 2026-11-10

Published and publicly verified as **Atoms Version 19 — Website Phone Password
Login** on the existing project and domain.

- Application import: `6c166cadf9f196d597eb49e12252b79a832cbc21` from
  `fredericsetievi/AI-Agent-Resto-Pilot/main`.
- Public landing: https://restopilot-dashboard.pub.atoms.world/
- Public login/signup: https://restopilot-dashboard.pub.atoms.world/app.html
- Verified saved origin:
  https://1631824-2cc522e0e3264f43a31c2e2a62d0ba27-v19-dev.dev.atoms.dev/
- Supabase project: `dptyxkmomskjvwbrqpnx`, retained.

The website accepts phone number and password, with Indonesian/English signup
and login. New accounts create their own empty restaurant workspace. Existing
owners can connect a phone using their existing credentials and keep their
restaurant/data. Messaging links confer no dashboard authority. No OTP provider
or messaging-channel choice is needed for website accounts.

## Source and native synchronization

The reviewed application and landing-packaging correction were committed/pushed
normally with the collaborator's configured Git identity. Unrelated edits to
AGENTS.md, the working release guide and the existing handoffs were retained
outside that commit; branches/history were preserved.

The existing private deployment copy, `Muneeb-Alvi/restopilot-atoms-deploy/main`,
was verified private and still at `fdfb40d4c0de1ec404ccc6dd0223a979fccae86a`.
It lagged the actual saved native V18, so it was not pulled over newer Atoms work.
The existing workflow's pinned public codeload import was used instead, with
archive root `AI-Agent-Resto-Pilot-6c166cadf9f196d597eb49e12252b79a832cbc21`.
Only the tracked allowlist, including `landing/`, was imported into
`app/deploy/restopilot-integration/`.

The release marker and 133-file manifest identify the full import SHA. The
backend was re-vendored and byte-checked, including `phone_auth.py`. The native
V18 asset plugin was retained, with its landing/app.html routes, i18n asset,
Rollup registration, missing-source guard and copy checks. Launcher, Vite proxy,
port file, API shim and backend router were preserved and checked by hash.
Existing archives and saved history were retained. No whole workspace or private
configuration was uploaded. The repeatable packaging tool now includes landing
assets and its regression checks pass.

## Migration and preserved data

Exact additive `0006_phone_login.sql` was applied **once in one transaction**.
The two new tables, `login_phone_identities` and `login_phone_limits`, were empty
immediately afterward. Their UUID foreign keys, phone primary key, unique owner
user ID, enabled RLS and absence of client policies were verified in Atoms.
Public table count changed from 28 to 30. No earlier migration was replayed.

Independent service-role REST snapshots compared stable, sorted SHA-256 row
fingerprints before migration, after migration and after publication. All 14
existing tables matched exactly:

| Table | Rows, unchanged |
| --- | ---: |
| restaurants | 50 |
| tenants | 41 |
| app_users | 4 |
| whatsapp_links | 6 |
| stock | 23 |
| purchases | 25 |
| movements | 42 |
| drafts | 57 |
| inventory_events | 3 |
| inventory_recipes | 2 |
| inventory_settings | 1 |
| whatsapp_operations | 12 |
| inventory_packages | 0 |
| whatsapp_purchase_sources | 1 |

No phone identity was guessed, backfilled or enrolled. No QA restaurant/account,
receipt, stock movement or business ledger entry was created. Verification used
normal login/logout sessions and one failed example-number phone login, which
creates ordinary rate-limit metadata in the new limits table. No business data
was reset, deleted, reseeded or changed.

## Runtime configuration

Only `PROACTIVE_CHECK_HOURS=0` was added through Atoms Cloud → Keys → Test Keys →
In This Project, with Save to Library unchecked. The UI confirmed it was saved.
The managed launcher was restarted, and Atoms confirmed the actual backend
process environment contained this non-secret value. The setting disables
scheduled proactive delivery. The publication dialog retained the Test value
for this setting along with the existing credential selections.

All other keys, the approved demo-owner-login setting, blocked default admin,
Supabase project, Meta test account/callback and Telegram configuration were
retained. No WhatsApp/Telegram message was sent and no business verification or
replacement infrastructure was started. The official Made by Atoms badge is
still visible on the published login page.

## Checks and public acceptance

- Local source Python: **559 passed, 63 skipped**; separate disposable local
  PostgreSQL phone-auth checks: **10 passed**.
- Local JavaScript: **37 passed**, including 32 frontend and 5 export-adapter
  checks. Packaging's eight tests and `git diff --check` passed.
- Atoms Python: **548 passed, 64 skipped, 0 failed**. The 11-pass difference is
  exactly three GitHub-layout adapter tests excluded by the workflow plus eight
  development-only packaging tests whose tool is not exported. The packaging
  module adds one skipped item in the hosted copy.
- Atoms frontend: **32 passed**; native lint and build passed. The five additional
  source JS checks target the development repository's exported adapter layout.
- A focused rerun of
  `test_whatsapp_link_does_not_enable_owner_login` passed; this test was also
  included in the full suites.

The saved V19 origin and the public domain were independently checked:

- `/` serves the landing and `/app.html` the phone/password login/signup page;
  landing CSS and application assets return 200.
- Both health endpoints return 200 with hosted/Supabase storage. Signed-out
  inventory returns 401; an existing approved owner logs in and reads inventory
  with 200; default admin credentials remain blocked.
- Published unmapped-phone login returns `401 invalid_credentials` with no session
  cookie. Empty registration/connection requests return validation 422.
- Fresh desktop Chromium verified ID/EN forms and persistence, signup-form
  switching, one-request existing-owner login, correct restaurant scoping,
  secure HttpOnly/SameSite cookies, reload persistence and logout, with no page
  errors. Signup and phone-connection POSTs were blocked in this browser check
  to guarantee no production account changes.

Normal browser asset URLs are byte-identical to the import:

| Asset | SHA-256 |
| --- | --- |
| app.js | `a3f926cd194dc1a432a551bff4cb3d84b2388d2b8cde25e46f8604d19320d8ef` |
| i18n.js | `718e92aa5743151b3556097a5f3329179b6e1df560e8d0a7a66f7b8c996bc96b` |
| styles.css | `1c988d159c84c85232ebbc43abb9924e361038e9909f10c4ed2f6e37c9275326` |

The first HTTP audit sent an explicit Origin header on static-asset GETs and hit
an older CDN variant (`Vary: Origin`). Paired requests demonstrated that normal
browser-style asset GETs serve the exact hashes above, and the real public
browser flow passed. The audit now sends Origin on authentication POSTs, while
modeling normal script/style GETs. A separate explicit-Origin cache variant can
remain stale until its cache expires; no cache-busting source change was made
for that nonstandard request variant.

Successful signup, phone connection and phone/password account login were
exercised with isolated SQLite and disposable PostgreSQL data. Production smoke
checks intentionally did not create/enroll an account or post stock. This release
is not a real Meta/Telegram messaging end-to-end test.

Local sanitized evidence is retained under ignored `tmp/phone-release-*.json`
and the public-login screenshot. Documentation-only follow-up does not require
another Atoms publication. Application provenance remains the pinned SHA above.
