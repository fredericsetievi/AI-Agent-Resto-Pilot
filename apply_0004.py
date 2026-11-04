"""Apply migration 0004 to Supabase (idempotent, additive only)."""
import sys
from pathlib import Path

from service.pg import db_config
import psycopg2

sql = Path("supabase/migrations/0004_platform_channels_and_agent_features.sql").read_text(encoding="utf-8")
cfg = db_config()
conn = psycopg2.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
                        user=cfg["user"], password=cfg["password"], sslmode="require")
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute(sql)
conn.close()
print("migration 0004 applied")
