"""Sales & Menu insights: deterministic answers from recorded sales.

The Sales & Menu agent, MVP slice. Reads the sale events the owners
already record (recipe-based sales with portions and revenue) and the
recipes, and answers plain-language questions — menu ranking, per-menu
trends, and per-portion margin (revenue minus ingredient cost, priced
from the restaurant's own recorded purchase prices). Every number in a
reply comes from these computations; nothing is invented and no model
runs in this module.

Windows mirror data_qa: a window of N days and the equal-length window
immediately before it, so every answer carries its own comparison.
"""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from .data_qa import find_windows, parse_window

_MENU_HINT_RE = re.compile(
    r"(?i)(\b(menu|terlaris|paling\s+laku|paling\s+banyak|paling\s+dicari|best\s*seller|best\s*selling|populer|penjualan|terjual|omzet|margin|untung|laba|keuntungan|rekap\s+penjualan)\b|\bjualan)")
_RANK_RE = re.compile(r"(?i)\b(terlaris|paling\s+laku|paling\s+banyak\s*terjual|populer|ranking|peringkat)\b")
_MARGIN_RE = re.compile(r"(?i)\b(margin|untung|laba|keuntungan|profit)\b")
_TREND_RE = re.compile(r"(?i)\b(penjualan|terjual|omzet)\b")
_COMPARE_RE = re.compile(r"(?i)\b(vs|dibanding|dibandingkan|banding|kenaikan|penurunan|turun|naik)\b")
# Narrow preemption set for the operations classifier: rank/margin/recap
# words are questions about recorded sales, never a sale recording itself
# ("terjual 4 nasi goreng" must keep reaching the operations parser).
_OPS_PREEMPT_RE = re.compile(r"(?i)\b(terlaris|paling\s+laku|paling\s+banyak\s*terjual|populer|margin|untung|laba|keuntungan|rekap|peringkat)\b")


def menu_question_preempts_operations(text):
    """True when the message asks about recorded sales (never a recording).

    Two shapes: the narrow rank/margin/recap words, and a question-shaped
    sales phrase with a time window ("penjualan nasi goreng minggu ini?")
    — questions never carry quantities, so recordings with numbers still
    reach the operations parser.
    """
    text = str(text or "")
    if _OPS_PREEMPT_RE.search(text):
        return True
    if "?" in text and re.search(r"(?i)\b(penjualan|omzet|terjual)\b", text) \
            and re.search(r"(?i)(minggu\s+(ini|lalu|terakhir)|bulan\s+(ini|lalu)|hari\s+ini|kemarin|\d+\s*hari|\bvs\b)", text) \
            and not re.search(r"\d", text):
        return True
    return False


_STOPWORDS = {"berapa", "yang", "ini", "itu", "minggu", "hari", "bulan", "ini", "di", "dari", "untuk",
              "apa", "adalah", "dan", "atau", "vs", "dibanding", "dibandingkan", "pada", "bulan", "7",
              "14", "30", "hari", "minggu", "terakhir", "kemarin"}


def _normalized(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _event_date(event):
    """The sale's day: business_date when known, else the recorded day (WIB-naive)."""
    raw = event.get("business_date") or str(event.get("created_at") or "")[:10]
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _window_pair(text, default_days=7):
    """Current and previous (label, start_date, end_date_exclusive) windows.

    Explicit phrases win, including comparisons ("minggu ini vs minggu
    lalu"); otherwise the previous window is the equal-length window
    immediately before the current one. Calendar windows ("bulan ini")
    use the days elapsed so far.
    """
    today = date.today()
    found = find_windows(text, limit=2)

    def bounds(win, fallback_days):
        start = date.fromisoformat(win["start"][:10]) if win.get("start") else today - timedelta(days=fallback_days)
        end = date.fromisoformat(win["end"][:10]) if win.get("end") else today + timedelta(days=1)
        label = win.get("label") or f"{fallback_days} hari terakhir"
        return start, end, label

    if found:
        cur_start, cur_end, cur_label = bounds(found[0], default_days)
        if len(found) > 1:
            prev_start, prev_end, prev_label = bounds(found[1], default_days)
        else:
            length = max((cur_end - cur_start).days, 1)
            prev_start, prev_end, prev_label = cur_start - timedelta(days=length), cur_start, f"{length} hari sebelumnya"
        return (cur_start, cur_end, cur_label), (prev_start, prev_end, prev_label)
    start = today - timedelta(days=default_days)
    return ((start, today + timedelta(days=1), f"{default_days} hari terakhir"),
            (start - timedelta(days=default_days), start, f"{default_days} hari sebelumnya"))


def _in_window(day, start, end):
    """start inclusive, end exclusive; day may be None (never matches)."""
    return day is not None and start <= day < end


def _percent_change(current, previous):
    if previous in (None, Decimal(0)):
        return None
    try:
        change = (Decimal(current) - Decimal(previous)) / Decimal(previous) * 100
        return float(round(change, 1))
    except Exception:
        return None


def _delta_words(change):
    if change is None:
        return ""
    direction = "naik" if change >= 0 else "turun"
    return f" ({direction} {abs(change):g}% dari periode sebelumnya)"


class MenuInsightsMixin:
    """Chat answers from recorded sales: ranking, trends, and margin."""

    def menu_facts(self, text, restaurant_id, conn=None):
        """Return {"body": ...} for a sales/menu question, or None when the
        message is not one. Detection is conservative: a purchase question
        like 'berapa pengeluaran cabai' never reaches this module."""
        text = str(text or "").strip()
        if len(text) > 300:
            return None
        has_hint = bool(_MENU_HINT_RE.search(text))
        spend_guard = re.search(r"(?i)\b(pengeluaran|belanja|beli|pembelian)\b", text) and not re.search(r"(?i)\b(penjualan|omzet|terjual|margin|untung|laba|menu|terlaris)\b", text)
        stock_guard = re.search(r"(?i)\b(sisa|stok|stock)\b", text)
        vs_or_window = re.search(r"(?i)(\bvs\b|minggu\s+(ini|lalu|terakhir)|bulan\s+(ini|lalu)|\d+\s*hari)", text)
        questionish = "?" in text or re.search(r"(?i)\b(berapa|gimana|bagaimana)\b", text)
        if spend_guard:
            # Purchase-spend questions belong to the data-question engine.
            return None
        if stock_guard and not vs_or_window:
            # Stock questions ("berapa sisa cabai?") belong to stock_facts.
            return None
        if conn is None:
            with self.transaction() as conn:
                events = self._menu_sale_events(conn, restaurant_id)
                recipes = self._menu_recipes(conn, restaurant_id)
                prices = self._ingredient_unit_prices(conn, restaurant_id)
        else:
            events = self._menu_sale_events(conn, restaurant_id)
            recipes = self._menu_recipes(conn, restaurant_id)
            prices = self._ingredient_unit_prices(conn, restaurant_id)
        menu_name = self._menu_name_from_text(text, events, recipes)
        if not has_hint and not (menu_name and vs_or_window and questionish):
            # Without a hint, only a named menu inside a window question
            # ("nasi goreng minggu ini vs minggu lalu?") is a menu question.
            return None
        (cur_start, cur_end, cur_label), (prev_start, prev_end, prev_label) = _window_pair(text)

        if _MARGIN_RE.search(text) and (menu_name or len(text.split()) <= 6):
            body = self._margin_answer(menu_name, events, recipes, prices, cur_start, cur_end, cur_label, prev_start, prev_end)
            if body:
                return {"body": body}
        if menu_name:
            body = self._menu_trend_answer(menu_name, events, cur_start, cur_end, cur_label, prev_start, prev_end)
            if body:
                return {"body": body}
        return {"body": self._ranking_answer(events, recipes, prices, cur_start, cur_end, cur_label, prev_start, prev_end)}

    # ---- storage reads ----------------------------------------------------

    def _menu_sale_events(self, conn, restaurant_id):
        events = []
        for row in self._inventory_query(conn, "SELECT body FROM inventory_events WHERE restaurant_id=?", (restaurant_id,)).fetchall():
            try:
                event = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else dict(row[0])
            except ValueError:
                continue
            if event.get("kind") == "sale" and event.get("confirmed") and not event.get("void"):
                events.append(event)
        return events

    def _menu_recipes(self, conn, restaurant_id):
        recipes = []
        for row in self._inventory_query(conn, "SELECT body FROM inventory_recipes WHERE restaurant_id=?", (restaurant_id,)).fetchall():
            try:
                recipe = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else dict(row[0])
            except ValueError:
                continue
            recipes.append(recipe)
        return recipes

    def _ingredient_unit_prices(self, conn, restaurant_id, days=30):
        """Latest recorded unit price per ingredient, from purchases in the
        window (the same evidence the price comparisons use)."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        prices = {}
        for row in self._inventory_query(conn, "SELECT body, created_at FROM purchases WHERE restaurant_id=? AND created_at>=? ORDER BY created_at, id", (restaurant_id, cutoff)).fetchall():
            try:
                purchase = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else dict(row[0])
            except ValueError:
                continue
            for item in purchase.get("items") or []:
                ingredient = item.get("ingredient_id")
                price = item.get("unit_price_idr")
                if ingredient and price not in (None, "", 0, "0"):
                    prices[ingredient] = Decimal(str(price))
        return prices

    # ---- matching ----------------------------------------------------------

    def _menu_names(self, events, recipes):
        names = {event.get("name") for event in events if event.get("name")}
        names |= {recipe.get("name") for recipe in recipes if recipe.get("name")}
        return {name for name in names if name}

    def _menu_name_from_text(self, text, events, recipes):
        """A menu name mentioned in the question, matched fuzzily."""
        lowered = text.casefold()
        words = [_normalized(w) for w in lowered.split()]
        words = [w for w in words if w and w not in _STOPWORDS]
        for name in sorted(self._menu_names(events, recipes), key=len, reverse=True):
            normalized = _normalized(name)
            if not normalized:
                continue
            if normalized in _normalized(lowered) or any(w and (w in normalized or normalized.startswith(w) and len(w) >= 4) for w in words):
                return name
        # Not a known menu: maybe a free-form name — only accept when the
        # message is short and clearly about one named thing after keywords.
        return None

    # ---- answers -----------------------------------------------------------

    def _aggregate(self, events, start, end, menu_name=None):
        """Aggregated sale facts for one window: per-menu portions, revenue."""
        stats = {}
        total_revenue = Decimal(0)
        total_portions = Decimal(0)
        revenue_known = False
        for event in events:
            if menu_name and _normalized(event.get("name") or "") != _normalized(menu_name):
                continue
            day = _event_date(event)
            if not _in_window(day, start, end):
                continue
            name = event.get("name") or "(tanpa nama)"
            entry = stats.setdefault(name, {"portions": Decimal(0), "revenue": Decimal(0), "revenue_known": False, "events": 0})
            entry["events"] += 1
            portions = event.get("portions")
            if portions not in (None, ""):
                try:
                    entry["portions"] += Decimal(str(portions))
                    total_portions += Decimal(str(portions))
                except Exception:
                    pass
            revenue = event.get("revenue_idr")
            if revenue not in (None, ""):
                try:
                    value = Decimal(str(revenue))
                    entry["revenue"] += value
                    entry["revenue_known"] = True
                    revenue_known = True
                    total_revenue += value
                except Exception:
                    pass
        return stats, total_revenue, total_portions, revenue_known

    def _ranking_answer(self, events, recipes, prices, cur_start, cur_end, cur_label, prev_start, prev_end):
        current, revenue, portions, revenue_known = self._aggregate(events, cur_start, cur_end)
        if not current:
            return (f"Belum ada penjualan tercatat dalam {cur_label}. "
                    f"Catat dulu lewat chat, misalnya 'terjual 4 nasi goreng'.")
        previous, prev_revenue, prev_portions, _ = self._aggregate(events, prev_start, prev_end)
        ranked = sorted(current.items(), key=lambda kv: (kv[1]["portions"], kv[1]["revenue"]), reverse=True)
        lines = [f"Penjualan {cur_label}:"]
        for index, (name, entry) in enumerate(ranked[:5], start=1):
            portions_text = f"{entry['portions']:g} porsi" if entry["portions"] else f"{entry['events']}x catatan"
            revenue_text = f" · Rp{entry['revenue']:,.0f}".replace(",", ".") if entry["revenue_known"] else ""
            lines.append(f"{index}. {name} — {portions_text}{revenue_text}")
        lines.append(f"Total {cur_label}: {portions:g} porsi" + (f" · omzet Rp{revenue:,.0f}".replace(",", ".") if revenue_known else ""))
        change = _percent_change(portions, prev_portions)
        if change is not None:
            lines.append(f"vs periode sebelumnya ({prev_label}): {change:+g}%.")
        return "\n".join(lines)

    def _menu_trend_answer(self, menu_name, events, cur_start, cur_end, cur_label, prev_start, prev_end):
        current, revenue, portions, revenue_known = self._aggregate(events, cur_start, cur_end, menu_name=menu_name)
        previous, prev_revenue, prev_portions, _ = self._aggregate(events, prev_start, prev_end, menu_name=menu_name)
        if not current and not previous:
            return (f"Belum ada penjualan tercatat untuk '{menu_name}'. "
                    f"Catat dulu, misalnya 'terjual 4 {menu_name}'.")
        lines = [f"Penjualan {menu_name} ({cur_label}):"]
        if current:
            portions_text = f"{portions:g} porsi" if portions else f"{sum(e['events'] for e in current.values())}x catatan"
            lines.append(f"• {portions_text}" + (f" · omzet Rp{revenue:,.0f}".replace(",", ".") if revenue_known else ""))
        else:
            lines.append("• Tidak ada di periode ini")
        if previous:
            prev_text = f"{prev_portions:g} porsi" if prev_portions else "ada catatan"
            lines.append(f"• Periode sebelumnya: {prev_text}")
        change = _percent_change(portions, prev_portions)
        if change is not None:
            lines.append(f"• {change:+g}% vs periode sebelumnya.")
        return "\n".join(lines)

    def _menu_cost_per_portion(self, recipe, prices):
        """Ingredient cost for one portion; returns (cost, missing_prices)."""
        cost = Decimal(0)
        missing = []
        for item in recipe.get("items") or []:
            ingredient = item.get("ingredient_id")
            quantity = item.get("quantity")
            if not ingredient or quantity in (None, ""):
                continue
            price = prices.get(ingredient)
            if price is None:
                missing.append(item.get("raw_name") or ingredient)
                continue
            try:
                cost += price * Decimal(str(quantity))
            except Exception:
                missing.append(item.get("raw_name") or ingredient)
        return cost, missing

    def _margin_answer(self, menu_name, events, recipes, prices, cur_start, cur_end, cur_label, prev_start, prev_end):
        recipes_by_name = {r.get("name"): r for r in recipes if r.get("name")}
        recipe = recipes_by_name.get(menu_name) if menu_name else None
        if menu_name and recipe is None:
            return (f"Belum ada resep untuk '{menu_name}', jadi biaya per porsi belum bisa dihitung. "
                    f"Buat resepnya dulu di dashboard Inventory (bahan + jumlah per porsi).")
        if recipe is None:
            # No menu named: rank by margin where revenue and a recipe both exist.
            lines = ["Margin per porsi (harga jual − biaya bahan dari pembelian tercatat):"]
            any_line = False
            for name in sorted(self._menu_names(events, recipes)):
                recipe = recipes_by_name.get(name)
                stats, _, _, revenue_known = self._aggregate(events, cur_start, cur_end, menu_name=name)
                entry = stats.get(name)
                if not recipe or not entry or not entry.get("revenue_known") or not entry["portions"]:
                    continue
                cost, missing = self._menu_cost_per_portion(recipe, prices)
                revenue_per = entry["revenue"] / entry["portions"]
                if missing:
                    lines.append(f"• {name}: omzet Rp{revenue_per:,.0f}/porsi · biaya bahan belum lengkap ({', '.join(missing[:2])} belum ada harga pembelian)")
                else:
                    margin = revenue_per - cost
                    lines.append(f"• {name}: Rp{margin:,.0f}/porsi (omzet Rp{revenue_per:,.0f} − bahan Rp{cost:,.0f})")
                any_line = True
            if not any_line:
                return ("Belum cukup data margin: butuh penjualan dengan omzet tercatat dan resep bahannya. "
                        "Catat 'terjual 4 nasi goreng, omzet 60rb' dan pastikan resepnya ada di Inventory.")
            lines.append("Biaya bahan memakai harga pembelian terakhir 30 hari; stok adalah estimasi.")
            return "\n".join(lines)
        # One named menu with a recipe.
        cost, missing = self._menu_cost_per_portion(recipe, prices)
        stats, revenue, portions, revenue_known = self._aggregate(events, cur_start, cur_end, menu_name=menu_name)
        entry = stats.get(menu_name)
        lines = [f"Margin {menu_name}:"]
        if missing:
            lines.append(f"• Biaya bahan per porsi: belum lengkap ({', '.join(missing[:2])} belum punya harga pembelian tercatat)")
        elif cost is not None:
            lines.append(f"• Biaya bahan per porsi: Rp{cost:,.0f}".replace(",", "."))
        if entry and entry.get("revenue_known") and entry["portions"]:
            revenue_per = entry["revenue"] / entry["portions"]
            lines.append(f"• Omzet per porsi: Rp{revenue_per:,.0f}".replace(",", "."))
            if not missing:
                margin = revenue_per - cost
                lines.append(f"• Margin: Rp{margin:,.0f} per porsi".replace(",", "."))
        elif not missing:
            lines.append("• Omzet per porsi belum tercatat — catat penjualan dengan omzetnya, misalnya 'terjual 4 nasi goreng, omzet 60rb'.")
        lines.append("Biaya memakai harga pembelian terakhir 30 hari; angka adalah estimasi.")
        return "\n".join(lines)