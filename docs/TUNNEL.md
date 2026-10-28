# Public HTTPS tunnel for the Meta WhatsApp webhook

Meta delivers WhatsApp messages to a public HTTPS webhook. The RestoPilot
server binds to `127.0.0.1`, so a tunnel forwards exactly the webhook route
from a public origin to the local server. Everything except
`/webhooks/meta/whatsapp` stays loopback-only.

## One-time: claim a free static ngrok domain

Ephemeral tunnel URLs change on every restart, and Meta's webhook
configuration must be updated each time. ngrok's free plan includes one
static domain:

1. Sign in at [dashboard.ngrok.com](https://dashboard.ngrok.com) (an
   authtoken in ngrok's local config is already present on this machine).
2. Open **Universal Gateway → Domains → New Domain** and claim the offered
   `something.ngrok-free.dev` name.
3. Add it to the repo's `.env` (git-ignored):

   ```dotenv
   NGROK_STATIC_DOMAIN=your-name.ngrok-free.dev
   ```

## Every session

From the repo root:

```sh
bash scripts/dev-tunnel.sh
# or with a separate rehearsal database:
bash scripts/dev-tunnel.sh --db data/rehearsal-01.sqlite3 --port 8001
```

The script starts ngrok and the RestoPilot server together, sets
`PUBLIC_BASE_URL` for the server process (without editing `.env`), and
prints the exact callback URL to configure:

```text
https://your-name.ngrok-free.dev/webhooks/meta/whatsapp
```

Paste that URL into **Meta → WhatsApp → Configuration → Callback URL** once;
with a static domain it stays valid across restarts. The verify token is the
`META_WEBHOOK_VERIFY_TOKEN` value already configured on Meta's side.

## Notes

- The tunnel forwards every path to the local server, but the server still
  answers only the webhook route on a public host (`invalid_host` elsewhere).
  Meta webhook signature validation (`X-Hub-Signature-256`) is unchanged.
- When the tunnel changes (ephemeral mode), update both the Meta callback URL
  and nothing else; `PUBLIC_BASE_URL` follows the tunnel automatically.
- For a permanent setup without ngrok, deploy the hosted demo to Vercel
  (see `vercel.json` and `api/index.py`) and point Meta at that origin.
