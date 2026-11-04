import json
import os
import traceback

os.environ["FORCE_SQLITE"] = "1"

from fastapi.testclient import TestClient
from service.api import create_app

secret = None
with open(".env", encoding="utf-8") as f:
    for line in f:
        if line.startswith("TELEGRAM_WEBHOOK_SECRET="):
            secret = line.split("=", 1)[1].strip()

app = create_app(".env-tg-debug.sqlite3", seed=False,
                 telegram_bot_token="123456789:AAExampleExampleExampleExampleExam",
                 telegram_webhook_secret=secret or "fallback-secret",
                 telegram_group_seconds=0)
client = TestClient(app, raise_server_exceptions=True)

update = {"update_id": 8001, "message": {"message_id": 8001,
          "chat": {"id": 999999999}, "date": 1, "text": "halo"}}
body = json.dumps(update).encode()

try:
    response = client.post("/webhooks/telegram",
                           headers={"X-Telegram-Bot-Api-Secret-Token": secret,
                                    "Content-Type": "application/json"}, content=body)
    lines = [f"status: {response.status_code}", response.text[:300]]
except Exception:
    lines = [traceback.format_exc()[-1500:]]

with open("debug_result.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
