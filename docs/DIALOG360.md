# 360dialog sandbox test

This integration is for the free 360dialog sandbox only. It is not a production
WhatsApp channel and does not bypass a restricted Meta WhatsApp Business
Account. The sandbox lets one WhatsApp account exchange test messages with the
shared 360dialog number, with a maximum of 200 sandbox messages.

Official documentation:

- https://docs.360dialog.com/docs/get-started/sandbox
- https://docs.360dialog.com/docs/messaging/webhook

## What this branch implements

`POST /webhooks/360dialog/sandbox/<secret>` accepts both sandbox webhook
envelopes observed in the documentation/live service, including phone-number
and business-scoped-user-ID accounts. A text
message enters the same persistent draft and `ya`/`batal` workflow as the local
simulator. RestoPilot then sends the generated reply through:

```text
POST https://waba-sandbox.360dialog.io/v1/messages
```

Status-only callbacks are acknowledged and ignored for now. Media produces the
existing safe request-for-text reply; media content is never fetched. Webhook
events and outbound delivery claims are deduplicated in SQLite.

The sandbox does not sign webhook requests or document custom callback headers.
For that reason this test route uses a long random secret in its URL and accepts
the configured public host only. Use the production provider's authenticated
webhook features before treating this as production infrastructure.

## 1. Rotate the API key

An API key pasted into chat, a ticket, or a log should be considered exposed.
Send `START` again to the 360dialog sandbox number and use the newly issued key.
Do not add it to a committed file.

If the response also gives a business-scoped user ID, no special application
configuration is required. The webhook parser detects that form and replies
using the API's `recipient` field. Otherwise it replies using `to` and your phone
number.

## 2. Start a public HTTPS tunnel

360dialog cannot call `127.0.0.1`. In one terminal, run:

```sh
cloudflared tunnel --url http://127.0.0.1:8000
```

Copy the generated `https://...trycloudflare.com` origin. Keep the tunnel
running. Its address changes whenever a new quick tunnel starts.

If a Cloudflare quick-tunnel hostname is created but does not appear in public
DNS, `localhost.run` is a suitable testing fallback:

```sh
ssh -o StrictHostKeyChecking=accept-new -R 80:127.0.0.1:8000 nokey@localhost.run
```

Use the generated `https://...lhr.life` origin in the same way. Neither quick
tunnel is production hosting.

## 3. Create the ignored local environment file

Generate a callback token:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Create `.env` locally from `.env.example` and set:

```dotenv
PUBLIC_BASE_URL=https://your-random-host.trycloudflare.com
DIALOG360_API_KEY=your-new-sandbox-api-key
DIALOG360_WEBHOOK_TOKEN=your-generated-url-safe-token
```

`.env` is ignored by Git. `DIALOG360_WEBHOOK_TOKEN` must contain 24–128 letters,
digits, underscores, or hyphens.

## 4. Run RestoPilot

In a second terminal:

```sh
.venv/bin/python -m service --db data/dialog360-demo.sqlite3
```

The startup output must say `360dialog sandbox: configured`.

## 5. Register the callback

Substitute the same three values used in `.env`:

```sh
curl --request POST \
  --url https://waba-sandbox.360dialog.io/v1/configs/webhook \
  --header 'Content-Type: application/json' \
  --header 'D360-API-KEY: YOUR_NEW_API_KEY' \
  --data '{"url":"https://YOUR_PUBLIC_HOST/webhooks/360dialog/sandbox/YOUR_WEBHOOK_TOKEN"}'
```

A successful response returns HTTP 200 and the callback URL. Never put the API
key in the callback URL. Repeat this step whenever the tunnel URL or sandbox API
key changes.

## 6. Test the real message loop

From the same WhatsApp account that requested the API key, message the 360dialog
sandbox number:

```text
beli cabai 3 kg 150rb
```

RestoPilot should reply with the draft summary. Then send:

```text
ya
```

It should reply that the purchase was recorded. Open the dashboard at
`http://127.0.0.1:8000` and confirm that the purchase and stock movement appear
exactly once.

The live sandbox currently returns HTTP 200 when it accepts an outbound message,
although its documentation example shows HTTP 201. RestoPilot accepts both. The
live callback can also include phone-number and business-scoped sender IDs
together; RestoPilot safely prefers the phone number for a key issued without a
BSUID-only instruction.

If no reply arrives, check in this order:

1. The tunnel and RestoPilot processes are both still running.
2. The startup output says the sandbox is configured.
3. `PUBLIC_BASE_URL` exactly matches the current tunnel origin.
4. The registered callback contains the exact token from `.env`.
5. The API key is the latest key issued; generating a new one invalidates the old
   key and requires registering the webhook again.
6. The 200-message sandbox limit has not been reached.

Do not use the production `waba-v2.360dialog.io` base URL with this sandbox key.
Moving to a real 360dialog number still requires a valid Meta/WABA onboarding
path and commercial 360dialog service.
