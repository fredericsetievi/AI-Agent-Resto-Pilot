# RestoPilot on Atoms

- Published dashboard: https://restopilot-dashboard.pub.atoms.world/
- Project: https://atoms.dev/chat/2cc522e0e3264f43a31c2e2a62d0ba27
- Export: published version 4, “Prepared owner login flag”.

This directory preserves the exported Atoms frontend, native backend, Lambda
handler, platform plugins (including Atoms branding), and managed startup script.
The hosted application changes are applied to the repository's `service/` and
`frontend/`, on top of the newer integration commits including the kitchen filter.
This GitHub sync does not republish the live site or merge into `main`.

## Sources and build

Use the repository root `service/` and `frontend/` as the application source.
The Vite plugin reads the root frontend directly. Before development or a build,
`backend/restopilot_app/sync_from_source.sh` copies the root service into the
ignored `backend/restopilot_app/service/` package for Atoms Lambda assembly.
Do not edit that generated copy. Both Vite's `predev` and `prebuild`, and the
managed startup script, run this preparation automatically.

With Python 3.11+, Node 20+, and pnpm 9.15.9:

```sh
# From the repository root; use a separate environment from the local demo.
python3 -m venv .venv-atoms
.venv-atoms/bin/python -m pip install -r deploy/atoms/backend/requirements.txt
.venv-atoms/bin/python -m pip install -r deploy/atoms/backend/requirements.default
cd deploy/atoms/frontend
corepack pnpm install --frozen-lockfile
corepack pnpm run build
corepack pnpm run lint
```

For the managed Atoms workspace, use this directory's `.mgx/config.yaml` and
`bash start_app_v2.sh` as the development entry point. Retain the full repository
above this directory so the source paths resolve. Package the generated backend
service together with `backend/` and frontend `dist/` when publishing. The exported
`lambda_handler.py` handles the platform's frontend/backend routing. Existing
Atoms private keys remain in Atoms; GitHub contains no copy of them.

## Server configuration

Enter values through Atoms' private server keys, never frontend `VITE_*` values:

| Setting | Purpose |
| --- | --- |
| `SUPABASE_URL` | Existing Supabase project's public URL |
| `SUPABASE_DB_PASSWORD` | Original database password; required |
| `SUPABASE_DB_HOST` | Optional session-pooler host for IPv4-only runtimes |
| `SUPABASE_DB_USER` | Pooler user `postgres.<project-ref>` |
| `SUPABASE_DB_PORT` | Session pooler port, normally `5432` |
| `ALLOW_DEMO_OWNER_LOGIN` | Exact value `1` opts the existing `owner` account into normal password verification |

The deployment currently has the owner opt-in enabled with the user's approval.
All other documented default credentials, including the default admin login,
remain blocked. Cookies are Secure, HttpOnly, and SameSite=Lax.

`routers/restopilot.py` enables hosted mode. Startup validates the existing schema
without seeding, migrating, or changing users or stock. Missing configuration,
unreachable Postgres, or schema mismatch returns `storage_unavailable`; it never
creates a SQLite substitute. Database connections require TLS.

Optional Meta and StepFun keys follow the root `.env.example`; the adapter only
uses explicitly configured server values. This sync does not configure or alter
any live WhatsApp webhook. The root CLI remains the separate local demo launcher.

## Routing and verification

Atoms forwards `/api/v1/*` to Python. The adapter exposes the full RestoPilot ASGI
app at `/api/v1/restopilot`; the browser shim rewrites `/v1`, `/health`, and
`/webhooks` fetches to that prefix, preserving requests and session cookies.

```sh
# Offline application and adapter regression tests, from repository root:
FORCE_SQLITE=1 .venv/bin/python -m pytest tests -q
node --test tests/frontend.test.cjs tests/atoms_adapter.test.cjs
# Hosted readiness (read-only):
curl https://restopilot-dashboard.pub.atoms.world/api/v1/restopilot/health/ready
```

A successful hosted readiness response reports `storage: supabase`; a signed-out
request to `/api/v1/restopilot/v1/overview` returns 401. Live Postgres integration
tests require separate explicit configuration and are skipped in the offline suite.
The original import bundles, private Git history archives, browser profiles,
logs, database files, credentials, and build outputs are excluded from this sync.
