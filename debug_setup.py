import json
import urllib.request
import urllib.error

token = None
with open(".env", encoding="utf-8") as f:
    for line in f:
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.split("=", 1)[1].strip()

chat_id = "1211291657"
out = []

def call(method, payload):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}")
    req.add_header("Content-Type", "application/json")
    data = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(req, data=data, timeout=20) as resp:
            body = json.loads(resp.read().decode())
            out.append(f"{method}: HTTP {resp.status} ok={body.get('ok')}")
            return body
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        out.append(f"{method}: HTTP {e.code} — {body}")
        return None

info = call("getMe", {})
me = info.get("result", {}) if info else {}
out.append(f"bot: @{me.get('username')} (id {me.get('id')})")

r = call("sendMessage", {"chat_id": chat_id, "text": "tes diagnostik RestoPilot"})
out.append(f"sendMessage ke {chat_id}: {'BERHASIL' if r and r.get('ok') else 'GAGAL'}")

with open("debug_result.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
