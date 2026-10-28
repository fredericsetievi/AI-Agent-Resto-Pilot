# RestoPilot handoff: WhatsApp sales, usage, stock counts, and report photos

Prepared 2026-10-28. This document carries context from the current chat into a new one.

## Repository update — 2026-10-29

The user subsequently instructed: **“for what we did so far push it to main.”** The completed dashboard inventory implementation, tests, migration file, and planning documents are included in the main commit carrying this update. Check `git log` and `git status` for the actual current state rather than assuming the historical uncommitted snapshot below is still pending. This push does not authorize or imply implementation of the planned WhatsApp extension, applying live migrations, or publishing a new Atoms version.

The sections below retain the original implementation/handoff snapshot; references to “uncommitted” and “unpushed” describe the state before this repository update.

## 1. Immediate instruction and authorization boundary

The user's latest instruction is: **plan WhatsApp support, including pictures of receipts and end-of-day sales reports; do not implement it yet; provide a handoff Markdown file.**

This handoff is the deliverable for that request. No WhatsApp inventory implementation, new deployment, migration application, commit, push, or live data changes were performed for this planning request. In the next chat, read this file and inspect the existing working changes first. Wait for an instruction to implement before executing this plan; once implementation is authorized, carry it through without repeatedly asking about decisions already settled below.

User intent, in their words:

> “can we make it do it through whatsapp as well ... not just limited to text only, maybe also pictures of receipts or the sales of the day. dont implement right now just plan ... give me a handoff md ... with the plan what we have done and ... instructions ive given u throughout”

## 2. Product goal and user preferences

RestoPilot is a WhatsApp-first operations assistant for Indonesian restaurants. The owner sends purchase text or supplier receipt photos, reviews a draft, confirms receipt, and approves. The server records the purchase, increases estimated ingredient stock, and updates the dashboard in the same database. Payment status is a separate fact.

The new goal is to close the stock loop: purchases add stock; cooking, waste, and sales reduce it; physical counts reconcile it; consumption informs when to reorder. The owner should be able to report a day's sales/usage from WhatsApp using ordinary text and photos/screenshots, with clarification and review where needed.

Standing user preferences and approved decisions:

- Keep the project on **Atoms by DeepWisdom**, retain its published dashboard link and official **Made with Atoms** badge. Local coding is fine; the user explicitly said Atoms need not be used for this feature unless useful.
- GitHub is the source repository. Work should ultimately reach **main**. The user said main had the latest work, not the older `whatsapp-integration` branch.
- Preserve all branches/history. Explain concrete behavior/data that a merge would lose and ask before such losses. Additive work does not require another blanket permission request.
- The Atoms GitHub connector did not work despite workspace ownership. Git CLI credentials worked; use the existing CLI workflow when necessary.
- Use the **existing Supabase integration and data**, rather than replacing the restaurant with an empty demo or creating a new unrelated database.
- User approved owner/admin login replacing anonymous dashboard access, hosted Supabase authority with no SQLite fallback, and the existing demo owner account being enabled on the public app.
- **Allow all incoming WhatsApp numbers** at application level. New/unlinked senders must first select a restaurant; existing linked numbers keep their restaurant. Do not revert to administrator-only number linking without instruction.
- Keep the **Meta WhatsApp test account**. The user does not want business verification/production registration yet.
- User approved moving the test WhatsApp webhook from the temporary Cloudflare tunnel to Atoms; this was completed.
- User supplied environment values and wanted them updated. Values are already in the ignored local `.env` and private Atoms keys; do not duplicate credentials into this handoff, source, examples, screenshots, or logs. No need to ask for all credentials again.
- User prefers action and concise updates, dislikes repeated permission/security discussions. Ask only when a material product choice is genuinely unresolved or a tool's approval mechanism requires it.
- The user authorized focused subagents for suitable tasks. Three were used for backend, frontend, and isolated PostgreSQL tests. No agent is needed merely to repeat this handoff.

## 3. Repository, live deployment, and environment

- Workspace: `/Users/Muneeb1/Desktop/AI-Agent-Resto-Pilot`
- Shell: zsh; timezone of user computer: Asia/Shanghai. Restaurant business timezone currently **Asia/Jakarta**.
- Repository: https://github.com/fredericsetievi/AI-Agent-Resto-Pilot
- Current local branch: **main**.
- Last committed HEAD at handoff: **`9fb15f1d9b77c7e69fcb90da826129aa572114bd`** (`Isolate hosted credential tests from deployment login flag`). Check current Git state before doing anything; it may evolve after this document.
- Earlier Atoms/Supabase merge: PR https://github.com/fredericsetievi/AI-Agent-Resto-Pilot/pull/5, merged as `92ba8a1216175ac909fcd8b0ffba71f4473c5a71`.
- Published dashboard: **https://restopilot-dashboard.pub.atoms.world/**
- Atoms project: **https://atoms.dev/chat/2cc522e0e3264f43a31c2e2a62d0ba27**
- Last verified published Atoms version: **V5, “WhatsApp Restaurant Onboarding.”** The new inventory work described below is NOT published.
- Last saved V5 preview: https://1631824-2cc522e0e3264f43a31c2e2a62d0ba27-v5-dev.dev.atoms.dev/
- Active Meta callback: **https://restopilot-dashboard.pub.atoms.world/api/v1/restopilot/webhooks/meta/whatsapp**
- Supabase URL: https://dptyxkmomskjvwbrqpnx.supabase.co
- Session pooler configuration previously verified: host `aws-0-ap-northeast-1.pooler.supabase.com`, port `5432`, user `postgres.dptyxkmomskjvwbrqpnx`, TLS required.
- Root `.env` is ignored and mode0600. It contains the supplied integration values and current Atoms origin. It does **not** contain the database password; that is an Atoms private key. The service API key does not replace a direct PostgreSQL password.
- Atoms private keys include the database password, pooler settings, StepFun/Meta/Supabase credentials, `PUBLIC_BASE_URL`, `WHATSAPP_OPEN_ONBOARDING=1`, and `ALLOW_DEMO_OWNER_LOGIN=1`.
- Demo owner: **`owner / owner123`**, Warung Bu Sari, tenant **`demo-resto`**. This exact public demo login was explicitly authorized by the user. Other restaurant tenants/data also exist.
- Platform admin's documented local demo credential is `admin / admin123`; hosted default admin credentials are blocked. Do not weaken that guard to test inventory.

Meta test-account metadata last verified:

- WABA `28816562587964227`, phone-number ID `1455505374302051`, app `2575202799571316` (“Resto Pilot”).
- Display sender **+1 555-666-1772**, “Test Number.”
- WABA is “Test WhatsApp Business Account”; business verification is `not_verified`; phone code verification `NOT_VERIFIED`.
- Subscription field `messages`, version v26.0. Existing outbound configuration is v23.0. Do not change versions merely to make them match.
- Meta's test-recipient restrictions still apply independently of the app's open restaurant onboarding. No production verification was started.

## 4. What is live versus implemented locally

### Existing committed/live baseline

- FastAPI service; same-origin static HTML/CSS/JavaScript dashboard.
- Supabase PostgreSQL is hosted authority. Local SQLite remains available for offline development.
- Hosted startup checks schema read-only, fails closed when Supabase is unavailable, and never seeds/migrates automatically.
- Restaurant-scoped login sessions, purchases, drafts, catalog, stock, activity, and WhatsApp threads.
- Purchase extraction/review/approval; exact Decimal quantities; transactional ledger; retries/version guards.
- Meta signed webhook verification, account/phone validation, sender onboarding, message deduplication, outbound replies.
- **Supplier receipt photo extraction already exists** through StepFun vision, including WhatsApp media download. It currently assumes the image is a purchase invoice. A POS sales screenshot MUST NOT be sent through that extractor and interpreted as a purchase.
- Voice-note transcription plumbing already exists in `service/audio.py` and the Meta handler, currently routed into purchase extraction. Configuration availability must be verified before claiming live voice support.
- WhatsApp stock questions read the ledger. Sales/usage/count reporting through WhatsApp is **not implemented**.

### New local inventory implementation — completed, uncommitted, unpushed, undeployed

The preceding task implemented and tested these dashboard/backend features:

1. `usage` and `waste` subtract explicit ingredient quantities.
2. Explicit package conversion: `0.5 × 12 kg = 6 kg`. Unknown box sizes are never guessed.
3. `count` sets the absolute remaining stock; includes `expected_quantity` and rejects a stale balance.
4. Saved recipes specify ingredient quantities per sold portion. Sales with recipe+portions deduct ingredients atomically and retain the recipe snapshot.
5. Recipe versions protect confirmation: a sale sends `expected_recipe_version`, and a changed recipe yields `recipe_conflict`. UI freezes the reviewed recipe snapshot while a form is open.
6. Sales without recipes can record portions and/or revenue with no stock deduction. Revenue is optional, integer IDR; this is not profit or payment settlement.
7. Negative-stock deductions reject the whole operation; no partial ingredient posting.
8. Event, signed movements, activity and retry response commit together; retrying a successful operation does not deduct twice.
9. PostgreSQL stock row locks are shared with receipt posting and acquired in sorted order. Identical request keys are serialized with a transaction advisory lock.
10. Forecasts use either owner-supplied per-ingredient daily rates or an explicitly confirmed complete history period of **3–90 completed Jakarta calendar days ending before today**. Average consumption divides by ALL days. Usage, waste and ingredient-backed sales count; purchases and count corrections do not.
11. Manual rates override observed rates; explicit zero suppresses a projection for that ingredient; missing evidence yields `insufficient_history`.
12. `days_left = stock / daily_usage`; `order_in_days = max(0, days_left - lead_days - safety_days)`. Restaurant-wide defaults: lead2days, safety1day. These are dashboard notices, not scheduled WhatsApp messages or automatic ordering.

New owner-only endpoints:

- `GET /v1/inventory/workspace`
- `POST /v1/inventory/events`
- `POST /v1/inventory/recipes`
- `POST /v1/inventory/settings`

All mutations require `Idempotency-Key`. See Pydantic models in `service/api.py` and methods in `service/inventory.py` for exact contracts.

Changed/new files to preserve (inspect `git diff` and untracked files; don't reset/clean):

| File | Purpose |
|---|---|
| `service/inventory.py` | Shared SQLite/PostgreSQL inventory mixin, validation, event posting, recipes, settings, forecasts |
| `service/db.py` | Mixin + additive SQLite schema |
| `service/pg_store.py` | Mixin + stock locking + request-key locking |
| `service/api.py` | Inventory request models and routes |
| `frontend/app.js`, `frontend/index.html`, `frontend/styles.css` | Inventory workspace/dialogs, sales, recipes, planning, history |
| `supabase/migrations/0004_inventory_operations.sql` | New inventory tables, RLS, movement-event reference |
| `tests/test_inventory.py` | Isolated real API/SQLite tests |
| `tests/test_inventory_postgres.py` | Real disposable local PostgreSQL transaction/concurrency tests |
| `tests/frontend.test.cjs` | Added inventory behavior tests; unref harness timers |
| `pytest.ini` | Restricts normal discovery to `tests/`, excluding old exported copies under tmp |
| `docs/INVENTORY.md` | Current feature guide/deployment notes |
| `README.md`, `docs/ROADMAP.md` | Updated capability notes |
| This file | Planning-only handoff |

**Migration0004 has NOT been applied to live Supabase.** It adds `inventory_events`, `inventory_recipes`, `inventory_settings`, and nullable `movements.inventory_event_id`, with RLS on new tables. It does not rewrite existing balances/purchases. Hosted inventory endpoints return clean `503 migration_required` before it exists; older purchase functionality remains compatible.

### Verification already completed

- Regular Python suite: **241 passed,15 skipped**. The skipped tests were 11 opt-in local PostgreSQL tests and4 older opt-in live Supabase tests.
- All **11 local PostgreSQL tests were run separately and passed** using real PostgreSQL17, bringing unique backend tests verified to **252**. No new feature test connected to live Supabase.
- Following the final recipe-version change, all **27 inventory SQLite/PostgreSQL tests passed again**.
- **25 JavaScript tests passed** (22 frontend +3 Atoms adapter).
- Final Atoms package production build passed; lint and `git diff --check` passed.
- Playwright/headless Chrome verified desktop+mobile: count30kg rice → half12kgbox usage removes6kg →20 recipe portions ×0.15kg removes3kg → remaining21kg → rate2kg/day forecasts10.5days left/order in7.5days. No browser errors or mobile horizontal overflow.
- Browser screenshots/helper are ignored under `tmp/inventory-ui/`.
- Local test dashboard was left running at **http://127.0.0.1:8012/app#inventory**, using **`tmp/inventory-browser.sqlite3`**, an isolated test database. Verify the server is still running before linking it. It is not the Supabase dataset.
- Disposable PostgreSQL cluster at `tmp/inventory-pg` was stopped after tests. Setup/start/test/stop instructions are in `tests/test_inventory_postgres.py`.
- Working interpreter: **`tmp/atoms-test-venv/bin/python`**. Root `.venv` lacks needed dependencies; the system Python lacks the Supabase client package. Avoid wasting time repeating those failures.

The four old live tests in `tests/test_pg_store.py` remain unrun in the offline suite:
`test_ready_reports_supabase`, `test_login_and_overview_over_postgres`, `test_intake_approve_and_whatsapp_flow_persist`, `test_unregistered_sender_is_rejected_over_postgres`. Their shared setup can seed/change login/link/ledger data, so don't run them indiscriminately against the user's database.

### Previously authorized live purchase test (separate earlier task)

The user explicitly requested testing approval and stock writes against their existing database. That targeted test passed via the public Atoms APIs, with independent read-only Supabase verification: received-goods guard422, purchase+1kg stock, retry creates no duplicate, fresh dashboard read reflects it.

It intentionally retained one QA purchase of Rp1,000 and ingredient **`QA approval test b95a36c0`** (`qa_approval_test_b95a36c0`),1kg, in `demo-resto`. Purchase ID `d4bdaed835da41968031ea174db96cd0`. Report: `tmp/live-approval-check/20261028T151131Z-b95a36c0.json`. Do not delete this or mistake it for real operating consumption without instruction.

## 5. Proposed WhatsApp experience — NOT IMPLEMENTED

### Entry examples

| Owner sends | Proposed behavior |
|---|---|
| “Hari ini terjual30 nasi goreng dan15 mie goreng” | Resolve menu items to recipes; show sales and calculated ingredient usage; request confirmation |
| “Penjualan hari ini Rp2 juta” | Prepare a revenue-only sales record; no ingredient usage can be derived from revenue alone |
| “Pakai setengah dus minyak” | Ask which oil/package size if unknown; after explicit mapping, propose canonical deduction |
| “Buang cabai0,5kg karena busuk” | Propose waste deduction and reason |
| “Sisa beras5kg malam ini” | Clarify/count intent, then propose absolute remaining stock, distinct from stock question “berapa sisa beras?” |
| Photo of supplier purchase receipt | Existing purchase draft behavior, explicitly classified as purchase |
| Photo/screenshot of POS daily sales report | Extract sold dishes/counts, date/period, optional revenue and printed totals; propose recipe mappings and a daily sales draft |
| Photo of individual customer receipt | Extract a single sale; clarify whether it is already included in a daily report |
| Photo of handwritten stock/usage sheet | Extract proposed usage/count lines; clarify what columns mean before calculating |
| Voice report | Reuse transcription, then send through the SAME intent/draft pipeline as typed text (if included in first release) |

Use natural Indonesian and existing command conventions. No required rigid command syntax, but explicit commands such as `penjualan`, `pemakaian`, `stok sisa` can aid ambiguous messages. Never let a low-confidence sales/usage message fall through into purchase extraction.

### Conversation and review

1. Validate Meta envelope and resolve the sender's restaurant before downloading media or calling a model. New senders complete existing restaurant selection first.
2. Detect whether the message is a command, question, purchase, sale, usage, waste, count, correction, or uncertain. For images, consider caption plus image content; absence of a caption does not imply purchase.
3. Build a durable, versioned draft with source provenance. AI proposes extracted facts only; deterministic code resolves units, recipes, totals, dates and stock effects.
4. Ask one focused question when necessary: “Ini laporan penjualan atau nota pembelian?”, “Satu dus berisi berapa liter?”, “Nasi spesial memakai resep yang mana?”, or “Tanggal laporan ini kemarin atau hari ini?”
5. Show a concise review: restaurant, operation/date/period, sold quantities, mappings/recipe versions, revenue if known, ingredient deductions, resulting estimated balances, warnings. Long reports should use bounded chunks and a clear draft reference.
6. Accept corrections and regenerate preview/version. The owner can choose a sales-only line for an unmapped dish; never invent a recipe or use another tenant's mapping.
7. Require explicit `YA`/confirmation of that active draft version. `BATAL` cancels. If a purchase draft is already active, explicitly resolve which draft is being confirmed; a bare `YA` must never approve an unintended operation.
8. Revalidate recipe versions, current balance/count guard, sender scope, and duplicate/overlap decisions at posting. Atomically commit the approved batch and source links through the shared inventory ledger.
9. Reply with recorded IDs/summary and updated stock/reorder information. Refreshing the dashboard must show the same records. A failed outbound reply or webhook retry must not repost stock.

Recipe-driven deductions are estimates. Owners should record variable/extra usage and waste separately, then periodically reconcile a physical count. Explain the double-counting rule in the draft when appropriate.

## 6. Engineering plan and dependency order

### Phase A — preserve and finish the local baseline

- Read this file, `docs/INVENTORY.md`, relevant source/tests and `git diff`. Preserve all uncommitted feature files.
- Confirm current branch/remotes and check for newer user changes. Do not reset/switch to old branches just because an old note mentions them.
- Reuse the finished inventory ledger; don't build a second WhatsApp stock calculator or separate storage authority.
- Keep migration0004 as existing local work. New changes should use a NEW additive migration0005 or later, rather than editing a migration after it has been applied anywhere. Verify actual database state before rollout.

### Phase B — durable operational drafts and shared posting service

Proposed new shared component: `service/operations.py` (name flexible).

- Typed operational drafts for sale, usage, waste and count, plus explicit purchase routing. Include restaurant, source channel/sender/message/media hash, business date/report period, status, version, extracted evidence, clarification needs, recipe/package mappings and expected recipe versions, preview, approved event IDs.
- Persist drafts/clarifications in both SQLite and PostgreSQL. Don't keep pending choices solely in Python memory or a model conversation.
- Existing `whatsapp_threads.active_draft_id` refers to purchase drafts, and `pending_followup` holds purchase payment followup. Add explicit active operation kind/id/version or a generalized compatible reference; don't overload payment strings with JSON and break the purchase flow.
- Support multiple dishes in one end-of-day report. The current `record_inventory_event` is one recipe/event per call. Refactor reusable validation/posting to accept an existing DB connection, then post an approved daily batch atomically. Do NOT loop over independent committing event calls or invoke nested store transactions to simulate a batch.
- Persist one report/batch identity with per-line events, recipe snapshots, and signed movements. Lock all relevant stock rows in the same sorted order used by receipts.
- Treat document ingestion retry keys and approval keys separately. Approval should be tied to restaurant+draft+version. Concurrent confirmations must yield one batch.
- Keep corrections/audit traceable. No revenue rollback currently exists; define an explicit cancel-before-approval and reviewed reversal/amendment path before promising edits to recorded sales. Never delete posted movements to fix a typo.

### Phase C — bounded extraction for text, images, and optional voice

- Keep purchase extraction intact behind intent routing. Existing `service/vision.py` has a PURCHASE-only prompt; introduce a separate document classifier/sales-report extractor or a typed multimodal extractor. Sales images must not produce purchase drafts automatically.
- Output validated structured candidates: document type, printed date/period, document/report ID, lines with raw menu name, sold quantity, optional line revenue, printed totals/discounts/refunds/tax, confidence/evidence and unresolved fields.
- Missing values remain null. Do not infer sold portions from money, infer profit from revenue, fabricate recipes, or guess package size/density. Discounts/tax can explain why totals differ; don't force totals to match by altering quantities.
- Resolve gross/net quantities, voids, refunds, and modifiers explicitly. If ambiguous, hold for clarification. A refund does not automatically put ingredients back into stock.
- Bound media size, supported MIME/signatures, rows/pages, provider timeouts and reply sizes. Current image support: JPEG/PNG/WEBP,5MB, existing extractor20items. Set/report explicit new limits; don't silently truncate a daily report.
- First release can support photos/screenshots rather than PDF/XLS/CSV. Unsupported or unreadable documents should ask for a clearer image or typed summary. If PDF support is later desired, plan it explicitly.
- Reuse `service/audio.py` for voice transcription if included; classify the transcript before any purchase parsing. Show uncertain transcription back in the review.
- Keep model output untrusted, never executable. Deterministic stock math remains the only write authority.
- Preserve source evidence appropriately. Current invoice intake uses digests and transient media; durable private originals are not yet generally implemented. Decide retention/privacy/expiry and private storage before promising permanent receipt viewing. Never store public receipt URLs with customer data.

### Phase D — menu mappings and package definitions

- Tenant-specific menu aliases → saved recipe IDs; exact match first, offer near matches for confirmation. Similar names across restaurants must never cross-match.
- For ambiguous dishes/modifiers, ask. Don't silently map a “large” portion to the normal recipe.
- Allow explicit recipe setup/correction from WhatsApp, or link to the existing recipe form as an initial fallback. Sales-only recording remains available when a recipe is unknown.
- Tenant/ingredient package definitions need canonical quantity per package and an explicit confirmation. For example, one oil box=12litres. kg↔litres requires an explicit valid mapping, not a guessed density.
- Recipe or package changes invalidate an old pending preview; re-review before approving. Approved events keep immutable snapshots.

### Phase E — business dates, duplicate sources, and reconciliation

These are required for reliable end-of-day reporting, not optional polish:

- Current events use `created_at` as posting time. Add a separate **business date/report period** in restaurant timezone, plus retained server receipt/post times. “Yesterday” after midnight must be resolved and reviewed.
- Update history averaging to use the explicitly confirmed consumption coverage/business dates. Keep purchases and physical-count adjustments excluded; use all calendar days, with manual overrides preserved.
- Handle transport duplicates (same Meta message), byte-identical photo reuploads (digest), document/report IDs, and semantic overlap (individual receipts later included in a daily total). Scope matching by tenant, business date and source. Exact duplicates replay; suspected overlap asks the user. A model's similarity guess cannot silently discard or approve business records.
- A daily report can span multiple shifts/channels. Two different reports on the same date are not automatically duplicates. Display coverage and whether the draft supplements or replaces an earlier report.
- If a new report replaces one already approved, require a reviewed delta/reversal workflow; never apply both full totals silently.
- Backdated reports after a physical count require special handling: stock counted afterwards may already include that consumption. Do not subtract historical usage again from the reconciled current stock. Show the conflict and offer explicit sales-only/history recording or a reviewed reconciliation strategy. Keep “current balance” semantics clear.
- End-of-day batches can include sales, extra usage/waste and a final count. Preview the combined effect and apply final counts after planned reductions; avoid counting the same recipe usage twice.
- Protect owner/sender authority consistently with existing approved restaurant linking. Open restaurant selection is not proof of owner identity; if stronger approval roles are proposed for counts/sales, present that concrete change to the user rather than silently reverting their open-onboarding choice.

### Phase F — summaries and reorder notices

- On successful recording, provide a concise sales/consumption summary and current estimated stock/reorder status from the same deterministic ledger.
- Read-only WhatsApp questions can include “stok cukup berapa hari?” and “apa yang perlu dipesan?”. State assumptions/coverage and insufficient history where applicable.
- Scheduled outbound WhatsApp reminders are a separate addition: durable scheduler/outbox, opt-in, send-window/template requirements, dedupe, timezone, recipient and failure/retry policy. Check current official Meta documentation when implementing. The present task does not authorize sending test messages to arbitrary recipients or starting verification.
- Initial release can return reorder advice in-session after the daily report. Do not claim automatic scheduled reminders until actually built and tested.

## 7. Acceptance tests for the next implementation

Use isolated fixtures first, then controlled authorized live tests. Preserve the existing purchase/onboarding flows.

1. Typed daily sales with two known dishes produces a reviewed batch; no write before confirmation; correct combined recipe deductions and optional revenue after approval.
2. POS screenshot produces a SALES draft, supplier invoice remains PURCHASE, ambiguous receipt asks a question, unreadable numbers remain unresolved.
3. A voice sales report (if supported) follows exactly the same draft/review/post path as text.
4. Unknown dish can be mapped/clarified or explicitly recorded sales-only; no fabricated ingredient usage.
5. Half-box usage with known size converts exactly; unknown size prompts; no unapproved conversion/mixed-unit guessing.
6. Waste subtracts; remaining-stock count sets an absolute amount. Stock questions remain read-only.
7. Version-changed recipe or stock-changed count refuses stale approval and asks for review.
8. Double webhook delivery, double `YA`, outbound reply failure/retry, and same-photo resubmission never double-post. Same daily report sent as text and image triggers overlap review when identity/coverage matches.
9. Individual receipt plus inclusive daily summary cannot silently double-deduct. Separate shifts on one date can both be recorded with explicit coverage.
10. Midnight/yesterday handling, backdated report after a later count, refunds/voids, tax/discount mismatch and partial reports have explicit tested behavior.
11. Multi-dish insufficient stock fails the whole batch; concurrent receipt and usage preserve both; race tests run against real disposable PostgreSQL as well as SQLite.
12. Restart retains draft/clarifications. Tenant isolation covers messages, media, recipe/package lookup, drafts, report identity, confirmations and forecast reads.
13. Mixed daily report with sales+extra waste+final count has a single reviewed interpretation and no double usage.
14. Dashboard events and WhatsApp confirmation show the same event IDs/balances, with source and business date available for audit.
15. Provider timeout, invalid model JSON, oversized/wrong-type photo and unsupported PDF produce a useful no-write fallback.
16. Forecast uses complete calendar coverage, excludes counts, distinguishes manual/recorded sources, and exposes insufficient history.

## 8. Rollout workflow when subsequently authorized

1. Finish/review code and tests against the existing uncommitted baseline. Keep user changes intact.
2. Prepare a focused branch/PR following the repo's existing process; preserve history. Review any actual behavior losses with the user before merging. No blanket need to re-ask about previously approved owner login/Supabase/onboarding.
3. Apply additive migrations explicitly to the existing Supabase project only after verifying their compatibility and deployment authorization. Hosted app startup must remain read-only. No seed/reset/truncate or replacement database.
4. Sync current sources into the Atoms runtime, run tests/build and inspect preview. Retain private key values and official badge.
5. Publish the reviewed version on the same Atoms domain. Keep the existing Meta test callback/account connected. Don't enable business verification.
6. Verify health, auth, old receipt approval, new draft flows, retries and readback. Real WhatsApp/provider messages and live ledger tests should use a clearly identified controlled recipient/data set when authorized; report what was retained.
7. Report actual deployment commit/version and any limitations (unsupported formats, incomplete recipes, no scheduler), not only local test success.

## 9. Practical tools, gotchas, and historical notes

- Source of truth: `service/` and `frontend/`. Exported Atoms runtime is `deploy/atoms/`. Backend sync script: `deploy/atoms/backend/restopilot_app/sync_from_source.sh`.
- Atoms forwarding prefix: `/api/v1/restopilot/<original route>`, e.g. `/api/v1/restopilot/v1/inventory/workspace`. The adapter mounts the complete original ASGI app and preserves error/auth middleware.
- Native Atoms workspace source path was `app/deploy/restopilot-integration/`; it has different frontend plugin-relative paths than the GitHub export. Don't blindly overwrite native Vite path configuration with the GitHub layout.
- Earlier Atoms sync downloaded a pinned public GitHub codeload archive through the Atoms AI. It preserved original source snapshots/history/private keys. Do not upload the whole local workspace/tmp or an environment-filled archive.
- `tools/browser/` contains the Playwright MCP setup (`npm run atoms`, `atoms:chrome`, `check`). User previously supplied an extension token; treat it as private, never paste it into handoff. Temporary helpers holding it were deleted. Rediscover available browser tools/current session rather than assuming old Node REPL sessions survive.
- For local browser QA, Playwright's downloaded headless binary was absent, but `chromium.launch({channel:'chrome',headless:true})` worked using installed Chrome, with tool-required escalation. The helper is `tmp/inventory-ui/check.mjs`.
- Old Atoms UI operations often needed `dispatchEvent('click')`. Saved preview and published version must be separately checked; an old saved V1 artifact was only a stub. Current known good publication is V5.
- Atoms Test Keys are used for the publication when no Production override is configured; changes require republish. Preserve existing overrides deliberately.
- `tmp/atoms-import/status.json` contains mixed historical fields. Its old top-level source SHA/stage/preview are stale; newer fields record V5/main9fb15f1. Use this handoff plus live inspection, not its first stage string alone.
- Existing isolated worktree `tmp/atoms-sync` remains on `feat/atoms-deployment`. A named stash retains original local Playwright setup: `stash@{0}: On main: Preserve local Playwright setup before approved main sync`. It was preserved intentionally; don't drop it.
- All original remote branches were preserved during PR5. `feature/db-server` had historical unique commits whose patches were already represented on main. Do not repeat wholesale merges without inspecting current topology.
- Original planning files `ATOMS.md`, `ARCHITECTURE.md`, `PROGRESS.md`, and `restopilot-technical-plan.md` contain obsolete audit-only/Twilio/no-Atoms/no-Supabase/no-construction claims. Those are historical, superseded by later user instructions and implemented code. They are also locally ignored. Do not treat them as current prohibitions.
- No secrets belong in this handoff. The user asked for context, not a new credential inventory with raw values.

## 10. Suggested opening prompt for the next chat

> Read `docs/HANDOFF_WHATSAPP_INVENTORY.md` (including its repository update) and `docs/INVENTORY.md`, then inspect Git status and the completed stock/sales implementation on main. The previous chat implemented the dashboard inventory feature and planned WhatsApp text/photo sales, usage and count reporting. The completed work was authorized for a main push; it has not been published to Atoms or migrated to live Supabase. Preserve that work and all existing data/branches. Follow my latest instruction about whether to continue planning or start implementing. Use the existing Supabase/Atoms/Meta test setup; don't restart setup or ask me to repeat decisions already in the handoff.
