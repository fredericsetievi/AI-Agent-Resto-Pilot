# RestoPilot delivery roadmap

Planning date: Saturday, October 17, 2026.

The phrase “in two weeks, the Thursday” is recorded as **Thursday, October 29, 2026**. Three weeks after that is **Thursday, November 19, 2026**. If the course calendar uses a different Thursday, update these dates immediately; the scope gates remain useful.

## Midterm progress demo — October 29

The midterm should show a real, persistent money-out loop rather than a collection of disconnected screens:

```text
WhatsApp-shaped message
        ↓
Structured purchase draft + source text
        ↓
Deterministic unit price and historical comparison
        ↓
Explicit owner approval (“ya” or dashboard review)
        ↓
One persistent purchase + estimated stock movement + activity trail
        ↓
Dashboard visibly updates and survives refresh/restart
```

### Required demo scope

- Polished overview, approval, purchases, inventory and activity screens.
- A persistent Twilio-shaped WhatsApp simulator in the dashboard.
- `beli cabai 3 kg 150rb` produces a draft, Rp50,000/kg and a comparison to approved history.
- `ya` approves only the draft correlated to that simulated sender; replay creates no duplicate purchase.
- `batal` rejects without stock changes; incomplete text such as `beli beras` requires dashboard correction.
- Twilio inbound webhook endpoint with signature validation and TwiML response, verified locally with signed fixtures.
- Live Twilio Try out WhatsApp is included only after the joined phone reaches the signed callback and the dashboard shows the correct effect. The current Trial flow does not support direct dynamic TwiML replies, so phone reply delivery is not a midterm claim.
- Every screen and message labels `simulated` versus `live` accurately.

### Build and freeze dates

| Date | Deliverable | Exit check |
|---|---|---|
| Oct 17–20 | Local purchase loop, dashboard, WhatsApp simulator and Twilio webhook contract | Automated replay/approval tests pass; Safari desktop flow visually checked |
| Oct 21–22 | Presenter mode polish, mobile layout and fresh rehearsal database | One person completes the script without developer tools; missing-field path is understandable |
| Oct 23–24 | Twilio Try out WhatsApp setup | **Completed early on Oct 17:** real phone purchase and `ya` reached the signed HTTPS webhook and appeared on dashboard; Trial reply limitation documented |
| Oct 25–26 | Failure cases, browser/mobile pass and evidence capture | Duplicate webhook, bad signature, ambiguous `ya`, correction and restart all behave visibly |
| Oct 27 | Feature freeze | No new features; fresh database rehearsal succeeds twice |
| Oct 28 | Final rehearsal and backup recording/screenshots | Live and simulator fallbacks both prepared; exact IDs/results retained |
| Oct 29 | Midterm demo | Present the real path available that day and label every fallback |

The simulator is the dependable midterm path. Twilio Try out WhatsApp is an inbound transport upgrade, not a reason to delay or destabilize the core workflow.

## Full flagship MVP — November 19

“Full” means the proposal’s flagship procurement and money-out loop for one pilot restaurant. It does not mean all future agents, autonomous ordering, payments, marketing, customer service or a POS replacement.

### Required full-MVP scope

1. Official Twilio WhatsApp text intake and correlated confirmation/rejection, plus manual dashboard intake.
2. Receipt photo ingestion from WhatsApp/dashboard into an editable draft, with retained private source evidence and explicit uncertainty.
3. Owner and staff identity, memberships and authorization for one pilot restaurant; two-tenant negative tests.
4. PostgreSQL persistence, atomic purchase/stock posting, idempotent webhook processing, durable jobs/outbox and visible recovery states.
5. Configurable ingredients, aliases, units, suppliers, reorder thresholds and stock-count reconciliation.
6. Approved-history unit-price comparisons with supplier/date evidence, insufficient-history behavior and no unsupported pack conversion.
7. Purchases, estimated inventory, pending approvals, supplier history and activity views backed by the same records.
8. Audited correction/reversal after posting; purchase and payment remain separate states.
9. Evaluation on consented/redacted or synthetic Indonesian text and printed receipt fixtures; no unsupported accuracy claims.
10. Runbook, backup/restore rehearsal, credential handling, demo/pilot privacy notes and a reproducible release tag.

### Post-midterm delivery plan

| Window | Focus | Demonstrable result |
|---|---|---|
| Oct 30–Nov 5 | Replace SQLite demo authority with migrations, PostgreSQL and managed identity; retain contract | Same approved purchase stays singular under concurrency and two-tenant access checks |
| Nov 6–12 | WhatsApp media/private storage, receipt extraction and correction; configurable catalog and counts | Printed receipt photo → draft → correction → approval → stock, with retained evidence |
| Nov 13–15 | Durable jobs/outbox, restart/retry, reversal, backup/restore and security pass | Worker/webhook failures recover without duplicate business records |
| Nov 16 | Feature freeze | Required flow complete; no P0/P1 data-loss or access issue |
| Nov 17–18 | User rehearsal, evaluation and final fixes | Two users complete the defined tasks; measured failures and limitations documented |
| Nov 19 | Full flagship MVP handoff/demo | Reproducible build, live/fallback transport, test evidence and runbook delivered |

### Cut order if time slips

Cut narrative AI explanations, scheduled alerts, voice, supplier discovery and rich charts first. Keep approval correlation, transaction safety, authentication, source evidence, correction, stock reconciliation and recovery. The midterm simulator remains available as the transport fallback throughout development.

## Inputs needed from the team

- Twilio account access is available: the account uses **Try out WhatsApp**, and a phone has joined the free trial.
- A public HTTPS development URL/tunnel for the callback, or approval to set one up. The local server must remain private except for the exact signed webhook route.
- A decision on the production database/identity package; PostgreSQL plus managed authentication is still recommended.
- Five to ten redacted or synthetic Indonesian purchase messages and, for the full MVP, printed receipt samples with expected fields.
- Confirmation that the course accepts Twilio as the WhatsApp transport and does not require Atoms or StepFun.

Never put account tokens in chat or version control. Configure them in the server environment and rotate any token that is exposed.
