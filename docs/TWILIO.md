# Twilio WhatsApp testing setup

The application supports two related transports:

- **Dashboard simulator:** runs entirely on the local machine, persists a Twilio-shaped conversation and needs no Twilio account.
- **Twilio testing environment:** accepts a signed inbound WhatsApp webhook and prepares a TwiML reply for compatible environments. It needs a Twilio account, a joined test phone and a public HTTPS callback; the current Trial may ignore dynamic TwiML.
- **Trial Messages poller:** optional rehearsal fallback that reads recent inbound messages through Twilio's REST API and writes them through the same RestoPilot WhatsApp transaction path. It does not send replies.

The simulator is the midterm fallback and should always remain usable. Never label it as a live WhatsApp integration.

## Twilio account preparation

Twilio’s current documentation distinguishes the legacy **Sandbox for WhatsApp** from the newer trial-account **Try out WhatsApp** flow. The legacy Sandbox is only available in the legacy Console; a trial account may present Try out WhatsApp instead. Use whichever test environment the existing account provides.

**Team status (October 17, 2026):** Try out WhatsApp is available, a Twilio phone number is registered, and the presentation phone has joined the free WhatsApp trial. A read-only API check reports the account type as `Trial`. After the linked session refreshed, the Console exposed **Receive a message → Custom**, and the signed callback was saved successfully. The phone sent `beli bawang 2 kg 80rb` followed by `ya`; Twilio delivered both webhook requests, RestoPilot recorded one Rp80,000 purchase, and the dashboard showed the new purchase and stock update. The polling fallback remains available for rehearsal resilience.

Twilio’s current Try out WhatsApp documentation says direct TwiML XML replies are not supported during the trial response. Therefore the trial can prove real inbound transport, signature validation, persistent draft/decision processing and dashboard updates, but it may not deliver RestoPilot’s dynamic summary back to the phone. The dashboard simulator remains the honest two-way midterm presentation. Dynamic live replies require a Twilio environment/sender that supports the normal WhatsApp reply flow; do not label a locally prepared TwiML response as delivered.

The testing environment does not require a registered production WhatsApp sender. A phone must join the specific testing environment before it can exchange messages. Twilio notes that Sandbox sessions expire after three days and that the shared Sandbox number can be temporarily restricted for some destinations, including Indonesia. Rejoin shortly before the presentation and rehearse the local simulator regardless.

Official references:

- https://www.twilio.com/docs/whatsapp/sandbox
- https://www.twilio.com/docs/whatsapp/quickstart
- https://www.twilio.com/docs/messaging/guides/webhook-request

## Local configuration

Install the pinned dependencies and run the app using the environment prepared for the demo:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Keep the real token out of shell history. Create the ignored local file with `cp .env.example .env`, fill it on the laptop, and run `chmod 600 .env`. The launcher reads only the following two literal settings; it does not execute the file or expand shell expressions:

```text
TWILIO_AUTH_TOKEN=<the account auth token>
PUBLIC_BASE_URL=https://<the exact public HTTPS host>
```

`PUBLIC_BASE_URL` must be the exact public origin Twilio calls, without a path or trailing slash. Signature validation uses:

```text
PUBLIC_BASE_URL + /webhooks/twilio/whatsapp
```

Start the service locally and expose only the required webhook through the chosen HTTPS development tunnel. The dashboard should continue to be opened at `http://127.0.0.1:8000`.

A development tunnel is a temporary public HTTPS address that forwards requests to the application on the presenter’s laptop. Twilio’s servers cannot call `127.0.0.1`, because that address exists only on the laptop. The tunnel is therefore used only for `/webhooks/twilio/whatsapp`; it is not a deployment and must not be treated as production hosting.

## Trial polling fallback

Use this path only as a rehearsal fallback when the temporary tunnel/callback is unavailable or the Trial Console records messages without forwarding them. The dashboard server and poller must use the same database path.

Credentials for the poller are accepted only from the process environment. Do not put the Auth Token in a command argument, committed file, screenshot, report, or terminal command that will be retained in shell history. The optional recipient filter must use Twilio's channel-qualified format:

```sh
export TWILIO_ACCOUNT_SID='AC...'
read -s TWILIO_AUTH_TOKEN && export TWILIO_AUTH_TOKEN
export TWILIO_WHATSAPP_TO='whatsapp:+<trial-recipient-number>'
```

After starting the dashboard with `.venv/bin/python -m service --db data/twilio-demo.sqlite3`, choose one polling mode in a second terminal:

```sh
# Read current recent history once; useful for a deterministic rehearsal.
.venv/bin/python -m service.twilio_poller --db data/twilio-demo.sqlite3 --once

# Keep importing during the live demo. The interval is bounded to 2-300 seconds.
.venv/bin/python -m service.twilio_poller --db data/twilio-demo.sqlite3 --interval 5
```

Each request is capped at 100 recent records (50 by default). When `TWILIO_WHATSAPP_TO` is set, the poller passes it to Twilio's list query and independently rejects records for other recipients. It also rejects outbound records, SMS records, malformed channel addresses, oversized bodies and excessive media counts. Messages are processed oldest-first inside each batch so a later `ya` follows its purchase note.

Processed `MessageSid` values are persisted in the existing `whatsapp_events` and idempotency tables. Re-running `--once`, restarting the process, or later receiving the same event through the webhook cannot duplicate a draft, purchase, or stock movement. The generated RestoPilot response is stored with status `not_delivered_trial_poll`; the poller never calls Twilio's outbound send API. Use the dashboard to inspect the result and the simulator to demonstrate a visible two-way conversation.

## Console callback

In **Try out WhatsApp → Inbound**, choose **Custom** for Auto-Reply and set the webhook to:

```text
https://<public-host>/webhooks/twilio/whatsapp
```

Use HTTP `POST`. Do not configure the simulator API as the Twilio callback. The webhook accepts Twilio’s form-encoded fields, including `MessageSid`, `From`, `To`, `Body` and `NumMedia`, validates `X-Twilio-Signature`, persists the event, and prepares an XML/TwiML response. Try out WhatsApp may ignore that dynamic response under the documented trial limitation; inspect the dashboard/activity rather than claiming phone delivery.

Twilio signs webhook requests. A request with a missing or invalid signature must be rejected. The code uses Twilio’s official `RequestValidator`; it does not trust forwarded host headers to reconstruct the signed URL. If the public URL changes, update both `PUBLIC_BASE_URL` and the Console callback before testing.

## Rehearsal

1. Join the Twilio WhatsApp testing environment from the presentation phone.
2. Send `beli cabai 3 kg 150rb`.
3. With the Custom callback configured, confirm that the dashboard approval count changes and the activity identifies a live Twilio message. If webhook forwarding is unavailable, run the poller with `--once` or leave its bounded loop running. A dynamic phone reply is not expected on the current Trial flow.
4. Send `ya`. Confirm in the dashboard that exactly one purchase is recorded and inventory changes once.
5. Run `--once` again and confirm old `MessageSid` records do not duplicate the purchase.
6. Send `beli beras`. Confirm that RestoPilot asks for missing details rather than guessing.
7. Send `batal` for a pending valid draft. Confirm that inventory is unchanged.
8. Restart the service and confirm the conversation and business records remain.

The first live test should use synthetic purchase data. Do not send real receipts or private restaurant information through the test environment until consent, retention, private storage and access controls are in place.

## Current limitations

- Text messages only. Media receives a clear fallback response until private receipt ingestion is implemented.
- The endpoint prepares a synchronous TwiML response for compatible Twilio environments, but Try out WhatsApp currently documents direct TwiML responses as unsupported. Delivery callbacks and a supported durable outbound sender are part of the full-MVP work.
- REST polling is a bounded trial/demo fallback, not a production ingestion architecture. It can only see the recent batch requested on each cycle; use a supported webhook or durable queue before production.
- The local demo identity is not authentication. Do not expose dashboard or mutation APIs publicly.
- Twilio’s 24-hour customer-service window and template rules still apply. The demo is based on user-initiated messages and immediate replies.
- The joined-phone inbound and approval round trip passed on October 17, 2026. Re-test shortly before the presentation because the quick-tunnel URL and trial linking session are temporary.
