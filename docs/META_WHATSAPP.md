# Meta WhatsApp Cloud API setup and test

This guide configures the current live WhatsApp transport. Automated fake-provider contract tests exercise the integration without contacting Meta. A separate real development-mode end-to-end test passed on October 20, 2026.

## Confirm account identifiers

The runtime needs two distinct numeric identifiers:

- `META_WABA_ID`: WhatsApp Business Account ID.
- `META_PHONE_NUMBER_ID`: the sender's Phone Number ID from WhatsApp API Setup.

Do not copy the Business Portfolio ID from the dashboard URL into `META_WABA_ID`. Read the WABA and Phone Number IDs from WhatsApp API Setup. The webhook payload's account ID must match the configured WABA ID, and its metadata phone ID must match the Phone Number ID.

## Required configuration

Copy `.env.example` to `.env` and fill in values from the Meta app / WhatsApp setup. Keep `.env` private and out of version control.

```dotenv
PUBLIC_BASE_URL=https://your-public-https-host.example
META_WHATSAPP_ACCESS_TOKEN=...
META_APP_SECRET=...
META_WEBHOOK_VERIFY_TOKEN=...
META_WABA_ID=...
META_PHONE_NUMBER_ID=...
META_GRAPH_API_VERSION=v23.0
```

Runtime configuration requires a WABA ID, Phone Number ID, access token, App Secret, verify token, public HTTPS origin and a `messages` webhook subscription. The verify token is a random value you choose and enter in both `.env` and Meta's webhook configuration. `PUBLIC_BASE_URL` must be the HTTPS origin only, without a path, query or credentials. The graph version should match the version configured for the app; the example is pinned rather than using `latest`.

The server binds to `127.0.0.1`. The exact `/webhooks/meta/whatsapp` route accepts the configured public host; other app routes remain loopback-only. A public HTTPS tunnel or reverse proxy must forward that route to the local server. Configure the webhook callback as:

```text
https://your-public-https-host.example/webhooks/meta/whatsapp
```

In Meta's Webhooks configuration, select the WhatsApp Business Account object and subscribe to the `messages` field. Use the same App Secret and verify token as the configured app values. Verification should return Meta's numeric challenge as plain text. Inbound POSTs must include a valid `X-Hub-Signature-256` signature; the app also checks WABA and Phone Number IDs.

## Run and test

1. Start the app with a fresh rehearsal database: `.venv/bin/python -m service --db data/meta-rehearsal.sqlite3`.
2. Confirm the startup line reports `Meta WhatsApp Cloud API: configured`.
3. In Meta, complete callback verification and enable the `messages` subscription.
4. Send a text message from a WhatsApp account allowed by the Meta test setup. Confirm the inbound message creates a persisted draft and the app sends its response through the Cloud API.
5. Confirm the draft through the supported WhatsApp flow only when its details are correct and goods were received, then verify the resulting purchase and stock movement in the dashboard. Otherwise use the dashboard correction or rejection path.
6. Re-send or replay the same provider event if possible and confirm it does not create duplicate purchase records.

The October 20 rehearsal received `beli bawang 2 kg 80rb`, returned a delivered Rp80,000 draft, accepted `YA`, persisted exactly one purchase and 2 kg movement, and returned a delivered confirmation. This is development/test-mode evidence, not a production-readiness claim. The automated fake-provider tests separately cover request/response shapes, webhook signatures, validation and retry behavior.

If the browser can reach Meta but the local Python process cannot, start the service with the existing network proxy exported as `HTTPS_PROXY` and `HTTP_PROXY`. Do not hard-code a machine-specific proxy address in the repository. `httpx` will use those standard environment variables.

Cloudflare quick-tunnel hostnames are temporary. When one changes, update `PUBLIC_BASE_URL`, restart the service and replace Meta's callback URL before testing again.

The WhatsApp sender and purchasing workflow are a local demo without production user enrollment or tenant authentication. Use synthetic rehearsal data, do not expose credentials, and do not treat the public webhook as a production deployment.
