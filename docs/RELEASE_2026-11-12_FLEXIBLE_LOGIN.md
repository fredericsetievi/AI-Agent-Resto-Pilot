# Flexible login identifier release — 2026-11-12

Published and publicly verified as **Atoms Version 26 — Flexible Login Release**
on the existing project and domain.

- Application import: `0455714509565ac6c5eb7a456b72cf313bfc49a3` from
  `fredericsetievi/AI-Agent-Resto-Pilot/main` ("Unify website login into one
  flexible identifier field", on top of the supplier commits 8900535/0f8bf44
  already imported and published as V20–V25).
- Public landing: https://restopilot-dashboard.pub.atoms.world/
- Public login/signup: https://restopilot-dashboard.pub.atoms.world/app.html
- Saved origin verified: https://1631824-2cc522e0e3264f43a31c2e2a62d0ba27-v26-dev.dev.atoms.dev/
- Supabase project `dptyxkmomskjvwbrqpnx`, retained.

## What changed

The dashboard login page now shows a single flexible identifier field
("Username atau nomor ponsel" / "Username or phone number") plus password.
Registration moved to its own `#register` view. The stacked legacy username
form, its optional phone-connect field, and the login/register mode toggle
were removed. `/v1/auth/login` already accepted a phone-shaped identifier
after a username miss (frontend/app.js, frontend/i18n.js,
frontend/index.html, frontend/styles.css, tests/frontend.test.cjs,
tests/test_auth.py; backend part arrived on main with the supplier merge).

## Source and native synchronization

Verified `0455714` is a descendant of `0f8bf44` (4 ahead, 0 behind) before
importing; nothing from published V25 was lost. Pinned public codeload
archive imported into `app/deploy/restopilot-integration/` (tracked
allowlist only); `app/deploy/SOURCE_COMMIT` set; `restopilot-release.json`
refreshed (151 files). All 32 backend modules byte-match the source,
including `suppliers.py`, `phone_auth.py`, `pg_store.py`, `api.py`. Native
launcher, Vite config, ports file, API base script, router and plugin were
unchanged (md5 checked). Migrations 0007/0008 were already applied; verified
never replayed (custom_ingredients (slug,id) PK; three supplier tables with
RLS and no client policies; purchases.supplier_id present; public table
count 33; row fingerprints identical before/after: restaurants 53, tenants
44, app_users 7, purchases 30, stock 28, movements 47, drafts 64,
whatsapp_links 9).

## Checks

- Local source Python: 638 passed, 64 skipped, 0 failed (offline,
  FORCE_SQLITE=1, test_atoms_adapter excluded on Atoms).
- Frontend: 32 of 32 passed; native lint and build passed; built assets
  byte-match the source; plugin ASSETS map unchanged.
- Saved-origin checks all PASS: health/ready 200 (storage supabase);
  signed-out inventory 401; one existing-owner login (restopilot_session
  cookie HttpOnly + Secure) then read-only inventory 200 (2 recipes,
  3 events, 18 forecasts); default admin 401 default_credentials_rejected;
  new single-field login page with working ID/EN switch that persists
  across reload; no page errors. Only PROGRESS.md was edited.
- Live-domain checks after publication: `/app.html` serves the new page;
  `/assets/app.js` sha256 `ee392515…` equals the saved origin; `/assets/i18n.js`
  contains the new keys; landing and `/landing-assets/styles.css` 200;
  unknown phone-shaped login → 401 invalid_credentials (phone fallback
  path), unknown username → 401. No accounts, purchases or messages created.

## Publication cache propagation

Immediately after Publish → Update, plain `/assets/app.js` and
`/assets/i18n.js` kept serving the previous publication through the CDN
(`Vary: Origin`, no Cache-Control) while `/app.html` and query-busted asset
URLs served V26 — the same propagation delay observed after the V25
publish. It cleared by itself within minutes; re-running Publish → Update
was registered harmlessly. Recommendation carried from V19/V25: make
`app.html` reference release-specific asset URLs (for example
`app.js?v=<source sha>`) so a publication can never reuse stale cached
copies.
