import json
import time
import urllib.request
import urllib.error

results = []

def time_get(url, headers=None, label="", n=3):
    times = []
    for _ in range(n):
        req = urllib.request.Request(url)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
            times.append(time.perf_counter() - t0)
        except Exception as e:
            times.append(-1)
            results.append(f"{label}: ERROR {e}")
            break
    if times and min(times) >= 0:
        results.append(f"{label}: min={min(times):.2f}s avg={sum(times)/len(times):.2f}s max={max(times):.2f}s")

def time_pg(label, sql, params=(), n=3):
    from service.pg import pg_connection
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            with pg_connection() as conn:
                cur = conn.cursor()
                cur.execute(sql, params)
                cur.fetchall()
            times.append(time.perf_counter() - t0)
        except Exception as e:
            times.append(-1)
            results.append(f"{label}: ERROR {e}")
            break
    if times and min(times) >= 0:
        results.append(f"{label}: min={min(times):.2f}s avg={sum(times)/len(times):.2f}s max={max(times):.2f}s")

# 1) Server lokal, endpoint ringan (tanpa DB berat)
time_get("http://127.0.0.1:8000/health/live", label="1. lokal /health (tanpa tunnel)")

# 2. Server lokal, overview penuh (login owner, DB Supabase)
import base64
from service.api import create_app
from service.pg_store import PostgresStore
import tempfile
app = create_app(tempfile.mktemp(), seed=False)
from fastapi.testclient import TestClient
client = TestClient(app, raise_server_exceptions=False)
client.post("/v1/auth/login", json={"username": "owner", "password": "owner123"})
t0 = time.perf_counter()
try:
    r = client.get("/v1/overview", headers={"Idempotency-Key": "x"})
    results.append(f"2. lokal /v1/overview (authority SUPABASE, payload penuh): {time.perf_counter()-t0:.2f}s (code {r.status_code}, size {len(r.content)//1024}KB)")
except Exception as e:
    results.append(f"2. overview ERROR {e}")

# 3. Supabase langsung: 1 query sederhana
time_pg("3. Supabase SELECT 1 (latensi jaringan DB saja)", "SELECT 1")

# 4. Supabase: query ringkasan ala overview
time_pg("4. Supabase COUNT purchases per tenant", "SELECT COUNT(*) FROM purchases")

# 5. Tunnel ngrok: endpoint ringan yang sama
time_get("https://leotard-delegate-usable.ngrok-free.dev/health/live",
         headers={"ngrok-skip-browser-warning": "1"}, label="5. via ngrok /health (tunnel)")

# 6. Tunnel ngrok: halaman login (payload UI)
time_get("https://leotard-delegate-usable.ngrok-free.dev/", headers={"ngrok-skip-browser-warning": "1"}, label="6. via ngrok GET / (halaman login)")

# 7. api.telegram.org (jalur balasan bot)
time_get("https://api.telegram.org", label="7. api.telegram.org (jarak ke Telegram)")

with open("debug_result.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(results))
print("done")
