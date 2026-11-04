# RestoPilot working instructions

These instructions apply throughout this repository. They describe the team's
workflow; access to a repository or the presence of this file alone is not a
request to deploy. Follow the user's current scope and any narrower limits.

## Interpret the request

- **Implement / fix / make a change:** implement and run appropriate local
  checks. Do not infer permission to push, migrate the live database or publish.
- **Implement and deploy / apply and publish / equivalent explicit release
  request:** carry the requested change through implementation, tests, release
  commit/push, required additive Supabase migrations, Atoms synchronization,
  saved-version checks, publication and public verification. Do not stop after
  the GitHub push or ask for the same rollout approval at each routine step.
- A request to push alone authorizes the GitHub update, not production rollout.
  An explicit review-first, staging-only, no-push or no-publication instruction
  overrides the normal release workflow.
- Batch related code edits into a reviewed release. Documentation-only changes
  such as these instructions need no Atoms publication unless requested.

## Start with the existing project

Inspect Git status, branch/history and existing diffs before editing. Preserve
other contributors' work and all branches. Do not reset, clean, force-push,
replace `.git` or discard behavior/data without explicit authorization. Before
a proposed merge would lose behavior or data, explain the concrete loss and ask.

Application source is `service/` and `frontend/`; generated vendored backend
copies are not the editing source. GitHub repository
`fredericsetievi/AI-Agent-Resto-Pilot` is the development source of truth.
Do not switch to older feature branches based on historical notes.

Read relevant current guides:

- [Inventory and planning](docs/INVENTORY.md)
- [WhatsApp reporting and release verification](docs/WHATSAPP_INVENTORY.md)
- [Team release workflow](docs/ATOMS_GITHUB_WORKFLOW.md), mandatory for deployment

The handoff contains useful rationale but also historical planning/status.
Some README notes and `deploy/atoms/` describe older exports. Use current source,
the guides above and actual deployed state to resolve historical contradictions.

## Preserve the product and data

- Keep the existing Atoms project/domain and official Atoms badge, existing
  Supabase project/data, and Meta WhatsApp **test** account/callback. Do not create
  replacement infrastructure or start business verification as routine setup.
- Keep restaurant selection for new WhatsApp numbers and existing sender links.
  Do not silently restore an administrator-only enrollment restriction.
- Preserve tenant isolation, owner authentication, the approved owner-login
  setting, blocked default admin login, secure cookies and original API/error
  behavior. Hosted storage fails closed; no SQLite fallback or startup seeding.
- Database migrations are explicit and additive. Never reset/reseed/truncate,
  backfill balances, or replay applied migrations as part of deployment. Inspect
  the target schema and preserve existing business records.
- Sales/usage/waste/counts reuse the inventory ledger, recipes and planning.
  Clarify uncertain quantities, recipes, package sizes and document types. Show
  calculations and require the restaurant's explicit reviewed confirmation
  before stock changes. Preserve duplicate/retry and version protections.
- Deployment approval does not approve business ledger entries or sending test
  WhatsApp messages to people. Prefer isolated tests and read-only live checks;
  a simulator review/retry/cancel check must not approve stock changes. Report
  any deliberately retained QA records.
- Keep credentials in private server settings or ignored local configuration.
  Never commit/upload whole workspaces, `.env`, browser profiles, databases,
  private history archives or receipt originals. Use each collaborator's own
  configured GitHub commit identity; Atoms and GitHub emails need not match.

## Verification and release execution

Use an activated project Python environment with `requirements.txt` installed;
do not depend on another collaborator's ignored `tmp/` environment or sessions.

```sh
env -u TEST_PG -u TEST_LOCAL_PG_DSN FORCE_SQLITE=1 python -m pytest -q
node --test tests/frontend.test.cjs tests/atoms_adapter.test.cjs
git diff --check
```

Run checks appropriate to the change. For transaction, concurrency or stock
posting changes, also run the applicable real disposable local PostgreSQL tests;
setup is documented in `tests/test_inventory_postgres.py`. Do not run the older
opt-in live Supabase tests indiscriminately: their fixtures can change live data.

When deployment is requested, follow the complete release sequence in the team
guide. Use local coding/tools for implementation and prompt Atoms only for
necessary platform work. Preserve the native launcher/proxy/plugin instead of
overwriting them from the older export. GitHub pushes do not automatically
synchronize or publish this app.

Collaborators must use their own authorized access to the existing services.
Discover available integrations and current sessions; do not assume another
person's login, browser profile or temporary scripts exist. If access is missing,
finish independent preparation before requesting only the missing access.

Report what actually happened: source SHA, Atoms version/domain, migrations
applied versus already present, checks performed and material limitations. A
saved preview is not a publication; a simulator check is not an actual Meta
photo end-to-end test. Do not claim the release is live until public checks pass.
