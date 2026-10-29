"""Durable WhatsApp inventory reviews. Extraction never authorizes a write."""
import copy
import re
import uuid
from datetime import datetime, date, timedelta
from decimal import Decimal

from .inventory import body, quantity


SQLITE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS whatsapp_operations (id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL, thread_id TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS whatsapp_operations_thread ON whatsapp_operations(restaurant_id,thread_id,status)",
    "CREATE INDEX IF NOT EXISTS whatsapp_operations_tenant ON whatsapp_operations(restaurant_id,created_at)",
    "CREATE TABLE IF NOT EXISTS inventory_packages (restaurant_id TEXT NOT NULL, ingredient_id TEXT NOT NULL, package_name TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(restaurant_id,ingredient_id,package_name))",
)


def normalized(value):
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


class OperationsMixin:
    def _operations_available(self, conn):
        if self.inventory_postgres:
            return all(self._inventory_query(conn, "SELECT to_regclass('public.whatsapp_operations'),to_regclass('public.inventory_packages')").fetchone())
        return conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('whatsapp_operations','inventory_packages')").fetchone()[0] == 2

    def has_active_operation(self, sender, channel=None):
        with self.transaction() as conn:
            if not self._operations_available(conn):
                return False
            sql = "SELECT o.id FROM whatsapp_operations o JOIN whatsapp_threads t ON t.id=o.thread_id WHERE t.sender=? AND o.status IN ('clarification','review','collision')"
            params = [sender]
            if channel is not None:
                sql += " AND t.channel=?"
                params.append(channel)
            return bool(self._inventory_query(conn, sql + " LIMIT 1", params).fetchone())

    def _operation_save(self, conn, op):
        from .db import encode, now
        op["updated_at"] = now()
        self._inventory_query(conn, "INSERT INTO whatsapp_operations(id,restaurant_id,thread_id,status,body,created_at,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,body=excluded.body,updated_at=excluded.updated_at", (op["id"], op["restaurant_id"], op["thread_id"], op["status"], encode(op), op["created_at"], op["updated_at"]))

    def _operation_rows(self, conn, tenant):
        return [body(row[0]) for row in self._inventory_query(conn, "SELECT body FROM whatsapp_operations WHERE restaurant_id=? ORDER BY created_at DESC,id DESC", (tenant,)).fetchall()]

    def _operation_duplicate(self, rows, op):
        sources = set(op.get('source_digests', [])) | {op.get('source', {}).get('digest')}
        sources.discard(None)
        c = op['candidate']
        for old in rows:
            if old['id'] == op['id'] or old['status'] != 'posted':
                continue
            prior = old['candidate']
            prior_sources = set(old.get('source_digests', [])) | {old.get('source', {}).get('digest')}
            if sources & prior_sources or (c.get('report_id') and c.get('report_id') == prior.get('report_id') and c.get('business_date') == prior.get('business_date') and normalized(c.get('coverage')) == normalized(prior.get('coverage'))):
                return old
        return None

    def _operation_reply(self, op, reply, action=None):
        return {"reply": reply, "result": {"action": action or "operation_" + op["status"], "operation": copy.deepcopy(op)}}

    def _operation_review_text(self, op):
        candidate = op["candidate"]
        header = f"Operasi {op['id'][:8]} V{op['version']} · {op['restaurant_id']} · {candidate.get('business_date') or 'tanggal belum jelas'}"
        if op.get("questions"):
            return header + "\n" + op["questions"][0] + "\nBelum dicatat. Balas BATAL untuk membatalkan."
        lines = [header, "Cek perhitungan berikut:"]
        for raw, event in zip(candidate.get("lines", []), op.get("preview", [])):
            name = raw.get("name") or "Penjualan"
            if event["kind"] == "sale":
                sold = event.get("portions") or raw.get("quantity")
                line = f"• Terjual {sold or '—'} {name}"
                if event.get("recipe"):
                    line += f" → resep {event['recipe']['name']} V{event['recipe'].get('version',1)}"
                else:
                    line += " (tanpa pengurangan stok)"
                if event.get("revenue_idr") is not None:
                    line += f"; pendapatan Rp{event['revenue_idr']:,}"
                lines.append(line)
            else:
                lines.append(f"• {event['kind']}: {name}")
            if raw.get("pack_size"):
                lines.append(f"  {raw.get('quantity')} × {raw['pack_size']} {raw.get('pack_unit') or ''} per {raw.get('unit') or 'kemasan'}")
            for item in event.get("items", []):
                unit = op.get("units", {}).get(item["ingredient_id"], "")
                if event.get("recipe"):
                    per = next(x['quantity'] for x in event['recipe']['items'] if x['ingredient_id'] == item['ingredient_id'])
                    lines.append(f"  {sold} × {per} = {item.get('planned_quantity',item['quantity'])} {unit} {item['ingredient_id']}")
                lines.append(f"  {item['ingredient_id']}: {item['before']} → {item['after']} {unit} (perubahan {item['delta']})")
        if candidate.get("revenue_idr") is not None and len(candidate.get("lines", [])) > 1:
            lines.append(f"Total pendapatan laporan Rp{candidate['revenue_idr']:,}; dicatat sekali.")
        lines.append("Cakupan: " + (candidate.get("coverage") or "belum diberi nama"))
        for package in op.get('packages',[]):
            lines.append(f"Ukuran kemasan {package['ingredient_id']}: 1 {package['package_name']} = {package['quantity']} {op.get('units',{}).get(package['ingredient_id'],'')} (versi {package['version']}; {'ditetapkan saat konfirmasi' if package.get('explicit') else 'tersimpan'}).")
        if op.get("history_only"):
            lines.append("Riwayat saja: stok saat ini tidak dikurangi; penggunaan historis tetap masuk perencanaan.")
        lines.append("Resep memperkirakan pemakaian; jangan laporkan bahan resep yang sama lagi sebagai pemakaian.")
        lines.append(f"Balas YA {op['id'][:8]} V{op['version']} untuk mencatat, atau BATAL. Koreksi dengan kirim laporan lengkap yang diperbaiki.")
        result = "\n".join(lines)
        if len(result) > 3900:
            return header + "\nRincian terlalu panjang untuk satu tinjauan WhatsApp. Belum dicatat. Balas BATAL lalu kirim laporan per bagian dengan cakupan berbeda (maksimal 30 baris dan rincian bahan yang dapat ditampilkan)."
        return result

    def _operation_resolve(self, conn, op):
        """Resolve only exact tenant facts; every unknown blocks approval."""
        from .db import decimal_string, RESTAURANT_TIMEZONE
        candidate, tenant = op["candidate"], op["restaurant_id"]
        questions = []
        today = datetime.now(RESTAURANT_TIMEZONE).date()
        try:
            business_date = date.fromisoformat(candidate.get("business_date"))
            if business_date > today:
                raise ValueError()
        except (ValueError, TypeError):
            questions.append("Tanggal laporan ini kapan? Balas hari ini, kemarin, atau YYYY-MM-DD.")
        if candidate.get("document_type") in {"ambiguous", "purchase_invoice"}:
            questions.append("Ini nota pembelian, penjualan, pemakaian, limbah, atau hitungan stok? Sebutkan jenis dan rincian yang terbaca; tidak ada stok berubah.")
        for issue in candidate.get("unresolved") or []:
            # Missing fields are regenerated below after a focused answer.
            if re.search(r"refund|void|gross|bruto|neto|netto|diskon|discount|pajak|tax|total|terpotong|truncat|tidak terbaca|unsupported|belum dapat|format",str(issue),re.I) or not re.search(r"quantity|quantit|jumlah|tanggal|date|recipe|resep|package|kemasan|unit|satuan|document.type|jenis dokumen|coverage", str(issue), re.I):
                questions.append("Perlu diperjelas: " + str(issue)[:300] + ". Kirim ulang rincian lengkap yang sudah benar.")
        catalog = self._catalog_for(tenant)
        recipes = [body(row[0]) for row in self._inventory_query(conn, "SELECT body FROM inventory_recipes WHERE restaurant_id=?", (tenant,)).fetchall()]
        op["units"] = {x["id"]: x["unit"] for x in catalog["list"]}
        payloads, packages = [], []
        lines = candidate.get("lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= 30:
            questions.append("Kirim rincian paling banyak 30 baris: nama menu/bahan, jumlah dan satuan. Untuk laporan besar, kirim per shift dengan cakupan jelas.")
            lines = []
        sales_lines = [line for line in lines if line.get('kind') == 'sale']
        first_sale_index = next((i for i,line in enumerate(lines) if line.get('kind') == 'sale'), None)
        revenues = [line.get('revenue_idr') for line in sales_lines]
        if candidate.get('revenue_idr') is not None and not sales_lines:
            questions.append('Nilai penjualan perlu baris penjualan yang jelas. Kirim rincian lengkap yang sudah benar.')
        if candidate.get('revenue_idr') is not None and any(r is not None for r in revenues):
            if any(r is None for r in revenues) or any(isinstance(r,bool) or not isinstance(r,int) for r in revenues) or sum(r for r in revenues if isinstance(r,int)) != candidate['revenue_idr']:
                questions.append('Total pendapatan dan rincian belum cocok/lengkap. Kirim rincian yang benar; jumlah porsi tidak ditebak dari nilai uang.')
        for index, raw in enumerate(lines):
            kind, name = raw.get("kind"), str(raw.get("name") or "").strip()
            p = {"kind": kind, "note": f"WhatsApp {op['id'][:8]}: {name or 'penjualan'}"[:500], "business_date": candidate.get("business_date"), "confirmed": True, "operation_id": op["id"], "source_digest": op.get("source", {}).get("digest"), "report_id": candidate.get("report_id"), "coverage": candidate.get("coverage"), "items": []}
            if op.get("history_only"):
                p["history_only"] = True
            if kind not in {"sale", "usage", "waste", "count"}:
                questions.append(f"Baris {index+1}: ini penjualan, pemakaian, limbah, atau hitungan stok?")
                continue
            amount = None
            if raw.get("quantity") is not None:
                try:
                    amount = quantity(raw["quantity"], zero=kind == "count")
                except Exception:
                    questions.append(f"Berapa jumlah {name}? Gunakan angka yang jelas.")
            if kind == "sale":
                revenue = raw.get("revenue_idr")
                if index == first_sale_index and candidate.get("revenue_idr") is not None and all(x.get("revenue_idr") is None for x in sales_lines):
                    revenue = candidate["revenue_idr"]
                p["revenue_idr"] = revenue
                if amount is not None:
                    p["portions"] = decimal_string(amount)
                elif revenue is None or (name and not raw.get('sales_only')):
                    questions.append(f"Berapa porsi {name} yang terjual?")
                if raw.get('unit') and normalized(raw['unit']) not in {'porsi','portion','portions','pcs','buah'}:
                    questions.append(f"Jumlah penjualan {name} memakai satuan {raw['unit']}. Jelaskan porsi terjual atau pilih TANPA RESEP; berat/volume bukan jumlah porsi.")
                if not raw.get("sales_only") and name:
                    matches = [r for r in recipes if (raw.get("recipe_id") and r["id"] == raw["recipe_id"]) or (not raw.get("recipe_id") and normalized(r["name"]) == normalized(name))]
                    if len(matches) != 1:
                        choices = ", ".join(r["name"] for r in recipes[:8]) or "belum ada resep"
                        questions.append(f"Resep untuk {name} belum pasti. Balas RESEP <nama resep persis> atau TANPA RESEP (tanpa pengurangan stok). Pilihan: {choices}. Resep baru dapat dibuat di dashboard Inventory.")
                    elif amount is not None:
                        p.update(recipe_id=matches[0]["id"], expected_recipe_version=raw.get("recipe_version", matches[0].get("version",1)))
                        raw.update(recipe_id=matches[0]["id"], recipe_version=p["expected_recipe_version"])
                payloads.append(p)
                continue
            matches = [item for item in catalog["list"] if normalized(name) in {normalized(item["id"]), normalized(item["name"])}]
            if not matches:
                # Existing purchase aliases are tenant-scoped; no fuzzy matching.
                from .extraction import catalog_aliases
                alias_id = dict(catalog_aliases()).get(normalized(name))
                matches = [catalog["by_id"][alias_id]] if alias_id in catalog["by_id"] else []
            if len(matches) != 1:
                questions.append(f"Bahan {name or index+1} yang mana? Balas BAHAN <nama bahan persis dari dashboard>.")
                payloads.append(p)
                continue
            ingredient = matches[0]
            raw["ingredient_id"] = ingredient["id"]
            if amount is None:
                questions.append(f"Berapa jumlah {name}? Sertakan satuan ({ingredient['unit']}) atau ukuran kemasan.")
                payloads.append(p)
                continue
            unit = normalized(raw.get("unit"))
            conversions = {"kg": ("kg", Decimal(1)), "kilogram": ("kg", Decimal(1)), "g": ("kg", Decimal("0.001")), "gram": ("kg", Decimal("0.001")), "l": ("l", Decimal(1)), "liter": ("l", Decimal(1)), "litre": ("l", Decimal(1)), "ml": ("l", Decimal("0.001")), "pcs": ("pcs", Decimal(1)), "buah": ("pcs", Decimal(1)), "butir": ("pcs", Decimal(1))}
            converted_unit, factor = conversions.get(unit, (unit, Decimal(1)))
            if converted_unit != ingredient["unit"]:
                if raw.get("pack_size") is not None:
                    pack_unit, pack_factor = conversions.get(normalized(raw.get("pack_unit")), (normalized(raw.get("pack_unit")), Decimal(1)))
                    try:
                        pack_size = quantity(raw["pack_size"]) * pack_factor
                    except Exception:
                        pack_size = None
                    if pack_unit != ingredient["unit"] or pack_size is None:
                        questions.append(f"Ukuran {unit or 'kemasan'} {name} harus jelas dalam {ingredient['unit']}; kg dan liter tidak dikonversi tanpa pemetaan yang sah.")
                        payloads.append(p)
                        continue
                    key = (tenant, ingredient["id"], unit)
                    saved = self._inventory_query(conn, "SELECT body FROM inventory_packages WHERE restaurant_id=? AND ingredient_id=? AND package_name=?", key).fetchone()
                    existing = body(saved[0]) if saved else None
                    packages.append({"ingredient_id":ingredient["id"], "package_name":unit, "quantity":decimal_string(pack_size), "version":(existing or {}).get("version",0), "explicit":True})
                    amount *= pack_size
                else:
                    saved = self._inventory_query(conn, "SELECT body FROM inventory_packages WHERE restaurant_id=? AND ingredient_id=? AND package_name=?", (tenant,ingredient["id"],unit)).fetchone()
                    if not saved:
                        questions.append(f"Satu {unit or 'kemasan'} {name} berisi berapa {ingredient['unit']}? Balas 1 {unit or 'kemasan'} = 12 {ingredient['unit']} (ganti 12 dengan ukuran sebenarnya).")
                        payloads.append(p)
                        continue
                    package = body(saved[0])
                    packages.append({**package, "ingredient_id":ingredient["id"], "package_name":unit, "explicit":False})
                    amount *= quantity(package["quantity"])
            else:
                amount *= factor
            item = {"ingredient_id":ingredient["id"], "quantity":decimal_string(amount)}
            if kind == "count":
                table, column = ("stock","slug") if self.inventory_postgres else ("inventory","restaurant_id")
                row = self._inventory_query(conn,f"SELECT quantity FROM {table} WHERE {column}=? AND ingredient_id=?",(tenant,ingredient["id"])).fetchone()
                item["expected_quantity"] = raw.setdefault("expected_quantity", row[0] if row else "0")
            p["items"] = [item]
            payloads.append(p)
        if candidate.get("document_type") == "ambiguous":
            questions.sort(key=lambda q: 0 if re.search(r'format|tidak terbaca|belum dapat|unsupported',q,re.I) else 2 if q.startswith("Tanggal") else 1)
        same_day_counts = []
        if not op.get('history_only'):
            consumed = {item['ingredient_id'] for p in payloads if p['kind'] in {'usage','waste'} for item in p['items']}
            for p in payloads:
                if p.get('recipe_id'):
                    recipe = next((r for r in recipes if r['id'] == p['recipe_id']),None)
                    if recipe:
                        consumed.update(i['ingredient_id'] for i in recipe['items'])
            for row in self._inventory_query(conn,'SELECT body FROM inventory_events WHERE restaurant_id=?',(tenant,)).fetchall():
                event = body(row[0])
                if event['kind'] == 'count' and self._inventory_event_date(event) == candidate.get('business_date') and any(i['ingredient_id'] in consumed for i in event['items']):
                    same_day_counts.append(event['id'])
            if set(same_day_counts) - set(op.get('after_count_ids',[])):
                questions.insert(0,'Sudah ada hitungan fisik pada tanggal ini. Apakah pemakaian laporan sudah termasuk hitungan itu? Balas RIWAYAT SAJA jika sudah termasuk (stok sekarang tidak dikurangi), atau SETELAH HITUNGAN jika seluruh pemakaian ini terjadi sesudah hitungan fisik tersebut.')
                op['same_day_count_ids'] = same_day_counts
                op['reconciliation_required'] = True
        op.update(questions=list(dict.fromkeys(questions)),payloads=payloads,packages=packages)
        if not questions:
            from .db import DomainError
            try:
                op["preview"] = self._preview_inventory_batch_conn(conn, payloads, tenant)
            except DomainError as exc:
                op["questions"] = ["Perlu ditinjau: " + exc.message]
                if exc.code == "reconciliation_required":
                    op["questions"] = ["Laporan ini lebih lama dari hitungan fisik berikutnya. Mengurangi lagi bisa menghitung dua kali. Balas RIWAYAT SAJA untuk mencatat penggunaan historis tanpa mengurangi stok sekarang, atau BATAL."]
                    op["reconciliation_required"] = True
                if exc.code == "stock_conflict":
                    op["questions"] = ["Stok berubah sejak hitungan disiapkan. Balas TINJAU ULANG untuk menghitung ulang terhadap saldo terbaru; setelah itu konfirmasi versi baru."]
                if exc.code == "recipe_conflict":
                    op["questions"] = ["Resep berubah. Balas TINJAU ULANG untuk melihat resep dan perhitungan terbaru sebelum konfirmasi lagi."]
        op["status"] = "clarification" if op["questions"] else "review"
        if op['status'] == 'review' and 'Rincian terlalu panjang' in self._operation_review_text(op):
            op['questions'] = ['Rincian terlalu panjang untuk tinjauan WhatsApp. Balas BATAL, lalu kirim per bagian dengan cakupan berbeda; tidak ada stok berubah.']
            op['status'] = 'clarification'

    def _operation_handle(self, conn, thread, tenant, text, *, candidate=None, source=None, purchase_draft=None):
        from .db import DomainError, encode, now
        from .operation_extraction import parse_operations_text
        if not tenant:
            return None
        text = str(text or "").strip()
        command = normalized(text)
        candidate = candidate if isinstance(candidate, dict) else parse_operations_text(text)
        available = self._operations_available(conn)
        if not available:
            if candidate:
                return {"reply":"Pelaporan operasi belum diaktifkan (migrasi 0005 diperlukan). Stok tidak berubah; gunakan dashboard Inventory.","result":{"action":"operation_migration_required"}}
            return None
        if self.inventory_postgres:
            # All source identities and approvals in a restaurant serialize;
            # stock locks remain shared with receipts in sorted ingredient order.
            self._inventory_query(conn,"SELECT pg_advisory_xact_lock(hashtextextended(?,0))",("whatsapp-operations:"+tenant,))
        rows = self._operation_rows(conn, tenant)
        active = next((o for o in rows if o["thread_id"] == thread["id"] and o["status"] in {"clarification","review","collision"}),None)
        source_digest = (source or {}).get('digest')
        if source_digest:
            if not candidate:
                prior_source = next((o for o in rows if o['status'] in {'posted','review','clarification','collision'} and source_digest in (set(o.get('source_digests',[])) | {o.get('source',{}).get('digest')})),None)
                if prior_source:
                    return self._operation_reply(prior_source,'Foto ini sudah menjadi laporan operasi yang '+('tercatat' if prior_source['status']=='posted' else 'menunggu tinjauan')+'. Tidak dibuat pembelian dari foto yang sama.','operation_duplicate')
            else:
                _, prior_purchase = self._purchase_source_guard(conn,tenant,source)
                if prior_purchase and prior_purchase['result']['action'] == 'purchase_source_duplicate':
                    return prior_purchase
        forecast_question = not candidate and bool(re.search(r'\breorder\b|\b(?:perlu|harus|kapan)\b.*\b(?:pesan|dipesan|beli)\b|\b(?:stok|bahan)\b.*\bberapa hari\b|\bcukup berapa\b',command))
        if forecast_question:
            workspace = self._inventory_workspace_conn(conn, tenant)
            text = 'Perkiraan stok (berdasarkan laju harian yang dikonfirmasi):\n' + '\n'.join(f"• {f['ingredient_id']}: " + (f"{f['days_left']} hari; pesan dalam {f['reorder_in_days']} hari ({f['source']})." if f.get('days_left') else 'riwayat/laju harian belum cukup.') for f in workspace['forecasts'][:30])
            return {'reply':text,'result':{'action':'operation_forecast'}}
        from .whatsapp import is_stock_question
        if active and not candidate and is_stock_question(text) and (command.startswith(('berapa','apa','cek','stok','stock')) or command.endswith('?')):
            named = self._stock_ingredient_ids(text,tenant)
            catalog = self._catalog_for(tenant)
            ingredients = [catalog['by_id'][i] for i in named if i in catalog['by_id']] if named else catalog['list']
            table, column = ('stock','slug') if self.inventory_postgres else ('inventory','restaurant_id')
            facts = []
            for ingredient in ingredients[:30]:
                row = self._inventory_query(conn,f'SELECT quantity FROM {table} WHERE {column}=? AND ingredient_id=?',(tenant,ingredient['id'])).fetchone()
                facts.append(f"• {ingredient['name']}: {row[0] if row else '0'} {ingredient['unit']}")
            return {'reply':'Perkiraan stok saat ini:\n'+'\n'.join(facts)+'\nDraf operasi tetap menunggu konfirmasi.','result':{'action':'stock_answer'}}
        approval = re.fullmatch(r"ya\s+([a-f0-9]{8})\s+v(\d+)",command)
        if approval and not active:
            previous = next((o for o in rows if o["id"].startswith(approval[1]) and o["thread_id"] == thread["id"]),None)
            if previous:
                return self._operation_reply(previous,"Operasi ini sudah " + ("tercatat" if previous["status"] == "posted" else "dibatalkan") + "; stok tidak diubah lagi.","operation_duplicate")
        if not active and not candidate:
            return None
        if active:
            if command in {"batal","batal operasi","cancel"}:
                active["status"] = "cancelled"
                self._operation_save(conn,active)
                return self._operation_reply(active,"Operasi dibatalkan. Stok tidak berubah. Draf pembelian yang sudah ada tetap menunggu pemeriksaan.")
            if active["status"] == "collision":
                return self._operation_reply(active,"Ada draf pembelian dan laporan operasi. YA tidak akan mencatat keduanya. Balas BATAL OPERASI, selesaikan draf pembelian, lalu kirim ulang laporan operasi.","operation_collision")
            if approval or command in {"ya","oke","ok","setuju","y","betul","sip","approve","ya benar"}:
                if not approval or approval[1] != active["id"][:8] or int(approval[2]) != active["version"] or active["status"] != "review":
                    return self._operation_reply(active,self._operation_review_text(active),"operation_review_required")
                duplicate = self._operation_duplicate(rows,active)
                if duplicate:
                    active.update(status='cancelled',duplicate_of=duplicate['id'])
                    self._operation_save(conn,active)
                    return self._operation_reply(active,'Sumber/ID laporan sudah tercatat sebagai '+duplicate['id'][:8]+'. Tidak ada pengurangan stok kedua.','operation_duplicate')
                # Re-resolve and compare precisely what was reviewed. No stale
                # balance, recipe, package, overlap or version is auto-approved.
                old_preview, old_packages = copy.deepcopy(active.get("preview")),copy.deepcopy(active.get("packages"))
                self._operation_resolve(conn,active)
                self._operation_overlap(rows,active)
                if active["status"] != "review" or old_preview != active.get("preview") or old_packages != active.get("packages"):
                    active["version"] += 1
                    self._operation_save(conn,active)
                    return self._operation_reply(active,"Perhitungan berubah; tinjau versi baru.\n"+self._operation_review_text(active),"operation_review_required")
                self._inventory_query(conn,"SAVEPOINT operation_post")
                try:
                    events = self._post_inventory_batch_conn(conn,active["payloads"],tenant,expected_preview=old_preview)
                    # Retained stock locks also protect count evidence. A count
                    # may leave the numeric balance unchanged, so comparing the
                    # reviewed balances alone cannot detect this race.
                    consumed = {item['ingredient_id'] for event in events if event['kind'] in {'usage','waste','sale'} and not event.get('history_only') for item in event['items']}
                    own_events = {event['id'] for event in events}
                    acknowledged = set(active.get('after_count_ids',[]))
                    if consumed:
                        for row in self._inventory_query(conn,'SELECT body FROM inventory_events WHERE restaurant_id=?',(tenant,)).fetchall():
                            count = body(row[0])
                            if count['kind'] == 'count' and count['id'] not in own_events and count['id'] not in acknowledged and self._inventory_event_date(count) == active['candidate']['business_date'] and any(item['ingredient_id'] in consumed for item in count['items']):
                                raise DomainError('count_conflict','Hitungan fisik baru tercatat saat konfirmasi. Tinjau ulang apakah pemakaian sudah termasuk hitungan itu; tidak ada pengurangan stok.',409)
                    for package in active.get("packages",[]):
                        if not package.get("explicit"):
                            continue
                        stored = {k:package[k] for k in ("ingredient_id","package_name","quantity")}
                        stored["version"] = package["version"]+1
                        self._inventory_query(conn,"INSERT INTO inventory_packages(restaurant_id,ingredient_id,package_name,body) VALUES (?,?,?,?) ON CONFLICT(restaurant_id,ingredient_id,package_name) DO UPDATE SET body=excluded.body",(tenant,package["ingredient_id"],package["package_name"],encode(stored)))
                    active.update(status="posted",events=events,posted_at=now())
                    self._operation_save(conn,active)
                    self._inventory_query(conn,"RELEASE SAVEPOINT operation_post")
                except DomainError as exc:
                    self._inventory_query(conn,"ROLLBACK TO SAVEPOINT operation_post")
                    self._inventory_query(conn,"RELEASE SAVEPOINT operation_post")
                    active.update(status="clarification",questions=[exc.message],version=active["version"]+1)
                    if exc.code == 'count_conflict':
                        self._operation_resolve(conn,active)
                    self._operation_save(conn,active)
                    return self._operation_reply(active,self._operation_review_text(active),"operation_review_required")
                workspace = self._inventory_workspace_conn(conn,tenant)
                reorder = [f["ingredient_id"] for f in workspace["forecasts"] if f["status"] == "reorder_now"]
                reply = "Tercatat sekali: " + ", ".join(e["id"][:8] for e in events) + ".\n" + "\n".join(f"{i['ingredient_id']}: {i['after']} {active['units'].get(i['ingredient_id'],'')}" for e in events for i in e["items"])
                reply += "\n" + ("Perlu dipesan: " + ", ".join(reorder) if reorder else "Lihat Inventory untuk perkiraan pemesanan; tanpa laju/riwayat lengkap, perkiraan belum tersedia.")
                return self._operation_reply(active,reply,"operation_posted")
            document_answer = active['candidate'].get('document_type') == 'ambiguous' and command in {'penjualan','pemakaian','limbah','hitungan stok','stok fisik','pembelian','ini penjualan','ini pemakaian','ini limbah','ini hitungan stok','ini pembelian'}
            if document_answer:
                self._operation_clarify(conn,active,text)
            elif candidate and candidate.get("document_type") != "ambiguous":
                active["candidate"] = copy.deepcopy(candidate)
                if source:
                    old_sources = set(active.get('source_digests',[])) | {active.get('source',{}).get('digest'),source.get('digest')}
                    active['source_digests'] = sorted(x for x in old_sources if isinstance(x,str))
                    active['source'] = {k:str(v)[:500] for k,v in source.items() if k in {'digest','media_kind','caption','event_id'} and v is not None}
                active.pop("overlap_accepted",None)
                active.pop("history_only",None)
                active.pop('after_count_ids',None)
            else:
                self._operation_clarify(conn,active,text)
                if active['status'] == 'cancelled':
                    self._operation_save(conn,active)
                    return self._operation_reply(active,'Laporan sudah termasuk catatan sebelumnya; dibatalkan tanpa mengubah stok.')
            active["version"] += 1
            self._operation_resolve(conn,active)
            self._operation_overlap(rows,active)
            self._operation_save(conn,active)
            return self._operation_reply(active,self._operation_review_text(active))
        source = {k:str(v)[:500] for k,v in (source or {}).items() if k in {"digest","media_kind","caption","event_id"} and v is not None}
        op = {"id":uuid.uuid4().hex,"restaurant_id":tenant,"thread_id":thread["id"],"version":1,"status":"clarification","created_at":now(),"candidate":copy.deepcopy(candidate),"source":source,"source_digests":[source['digest']] if source.get('digest') else []}
        exact = self._operation_duplicate(rows,op)
        if exact:
            return self._operation_reply(exact,'Sumber/ID laporan ini sudah tercatat; stok tidak dikurangi lagi.','operation_duplicate')
        if purchase_draft:
            op.update(status="collision",questions=["Ada draf pembelian aktif. Balas BATAL OPERASI, selesaikan pembelian, kemudian kirim ulang laporan ini. YA tidak menyetujui keduanya."])
        else:
            self._operation_resolve(conn,op)
            self._operation_overlap(rows,op)
        self._operation_save(conn,op)
        return self._operation_reply(op,self._operation_review_text(op))

    def _operation_overlap(self, rows, op):
        c = op["candidate"]
        overlaps = []
        names = {normalized(x.get("name")) for x in c.get("lines",[]) if x.get("kind") == "sale"}
        for old in rows:
            prior = old["candidate"]
            if old["id"] == op["id"] or old["status"] != "posted" or prior.get("business_date") != c.get("business_date"):
                continue
            old_names = {normalized(x.get("name")) for x in prior.get("lines",[]) if x.get("kind") == "sale"}
            if not names or not old_names:
                continue
            if c.get("document_type") == "sales_report" or prior.get("document_type") == "sales_report" or names & old_names:
                accepted = op.get("overlap_accepted",{})
                if accepted.get("prior_ids") and old["id"] in accepted["prior_ids"] and normalized(c.get("coverage")) != normalized(prior.get("coverage")):
                    continue
                overlaps.append(old["id"])
        if overlaps:
            op["overlaps"] = overlaps
            op["questions"].insert(0,"Ada penjualan pada tanggal yang sama ("+", ".join(i[:8] for i in overlaps)+"). Apakah sudah termasuk? Balas SUDAH TERMASUK untuk batal, atau TERPISAH <cakupan shift/channel yang berbeda>. Penggantian total memerlukan rekonsiliasi; tidak dijumlah otomatis.")
            op["status"] = "clarification"

    def _operation_clarify(self, conn, op, text):
        from .db import RESTAURANT_TIMEZONE
        command, c = normalized(text),op["candidate"]
        if c.get('document_type') == 'ambiguous':
            answer = command.removeprefix('ini ')
            types = {'penjualan':('sales_report','sale'),'pemakaian':('usage','usage'),'limbah':('waste','waste'),'hitungan stok':('count','count'),'stok fisik':('count','count')}
            if answer in types:
                c['document_type'], kind = types[answer]
                for line in c.get('lines',[]):
                    line['kind'] = kind
                return
            if answer == 'pembelian':
                c['unresolved'] = ['Untuk nota pembelian yang belum terbaca, balas BATAL lalu kirim ulang foto dengan keterangan nota pembelian. Belum ada pembelian dicatat.']
                return
        if command == "sudah termasuk":
            op["status"] = "cancelled"
            op["candidate"]["unresolved"] = ["Laporan sudah termasuk catatan sebelumnya; balas BATAL untuk menutup tanpa perubahan."]
            return
        if command.startswith("terpisah ") and op.get("overlaps"):
            c["coverage"] = text.split(" ",1)[1].strip()[:200]
            op["overlap_accepted"] = {"prior_ids":list(op["overlaps"]),"coverage":c["coverage"]}
            return
        if command == "riwayat saja" and op.get("reconciliation_required"):
            op["history_only"] = True
            return
        if command == 'setelah hitungan' and op.get('same_day_count_ids'):
            op['after_count_ids'] = list(op['same_day_count_ids'])
            return
        if command == "tinjau ulang":
            for line in c.get("lines",[]):
                line.pop("expected_quantity",None)
                line.pop("recipe_version",None)
            return
        today = datetime.now(RESTAURANT_TIMEZONE).date()
        if command in {"hari ini","kemarin"} or re.fullmatch(r"\d{4}-\d{2}-\d{2}",command):
            c["business_date"] = (today-timedelta(days=command=="kemarin")).isoformat() if command in {"hari ini","kemarin"} else command
            return
        lines = c.get("lines",[])
        if command.startswith("resep ") or command == "tanpa resep":
            recipes = [body(r[0]) for r in self._inventory_query(conn,"SELECT body FROM inventory_recipes WHERE restaurant_id=?",(op["restaurant_id"],)).fetchall()]
            target = next((l for l in lines if l.get("kind")=="sale" and not l.get("recipe_id") and not l.get("sales_only")),None)
            if target is not None:
                if command == "tanpa resep":
                    target["sales_only"] = True
                else:
                    matches = [r for r in recipes if normalized(r["name"])==command[6:] or r["id"]==command[6:]]
                    if len(matches)==1:
                        target.update(recipe_id=matches[0]["id"],recipe_version=matches[0].get("version",1))
            return
        if command.startswith("bahan "):
            target = next((l for l in lines if l.get("kind")!="sale" and not l.get("ingredient_id")),None)
            if target is not None:
                target["name"] = text[6:].strip()
            return
        package = re.fullmatch(r"(?:1\s+)?([\w ]+)\s*=\s*(\d+(?:[.,]\d+)?)\s*(kg|g|gram|l|liter|ml|pcs|buah|butir)",command)
        if package:
            target = next((l for l in lines if normalized(l.get("unit"))==package[1].strip() and not l.get("pack_size")),None)
            if target is not None:
                target.update(pack_size=package[2].replace(",","."),pack_unit=package[3])
            return
        amount = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(kg|g|gram|l|liter|ml|porsi|pcs|dus|box|kotak|karung)?",command)
        if amount:
            target = next((l for l in lines if l.get("quantity") is None or not l.get("unit") and l.get("kind")!="sale"),None)
            if target is not None:
                target["quantity"] = amount[1].replace(",",".")
                if amount[2]:
                    target["unit"] = amount[2]
