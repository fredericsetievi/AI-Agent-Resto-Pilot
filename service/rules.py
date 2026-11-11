"""Deterministic parsing of per-restaurant agent rules.

An owner writes rules as free Indonesian text ("kabari kalau cabai di bawah
3 kg", "jangan ingatkan stok telur"). Only two directive kinds are honored,
and both parse without any model: a per-ingredient threshold override and a
mute. Everything else stays free text that rides along as LLM prompt context
and never changes a number — the ledger remains the only source of figures.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .extraction import catalog_aliases


_THRESHOLD = re.compile(r"(?i)\b(?:di\s*bawah|dibawah|kurang\s*dari|less\s*than|kureng\s*dari|minimum|minimal)\s*(?:dari\s*)?(?P<threshold>[0-9]+(?:[.,][0-9]+)?)")
_MUTE = re.compile(r"(?i)\bjangan\b[^.]{0,40}?\b(?:kabari|ingatkan|notif(?:ikasi)?|kirim|pantau|tandai)\b")
_NUMBER_OK = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_MAX_THRESHOLD = Decimal("1000000")


def _ingredient_ids(text, catalog):
    """Catalog ids whose names appear in the text, longest name first.

    Mirrors the stock-question matcher: the shared alias list plus each
    catalog surface (slug and display name), longest match first with its
    span masked so "bawang putih" cannot also pull in plain "bawang".
    """
    lowered = " " + re.sub(r"\s+", " ", str(text).strip().casefold()) + " "
    covered = {ingredient_id for _alias, ingredient_id in catalog_aliases()}
    aliases = list(catalog_aliases())
    for ingredient in catalog:
        if ingredient["id"] in covered:
            continue
        for surface in (str(ingredient["id"]).replace("_", " "), str(ingredient["name"]).casefold()):
            surface = surface.strip()
            if surface:
                aliases.append((surface, ingredient["id"]))
    found, seen = [], set()
    masked = lowered
    for alias, ingredient_id in sorted(aliases, key=lambda pair: len(pair[0]), reverse=True):
        if ingredient_id in seen:
            continue
        match = re.search(r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])", masked)
        if match:
            found.append(ingredient_id)
            seen.add(ingredient_id)
            masked = masked[:match.start()] + "?" * (match.end() - match.start()) + masked[match.end():]
    return found


def _decimal(value):
    try:
        parsed = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed > _MAX_THRESHOLD:
        return None
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or None


def parse_rule(text, catalog):
    """Parse one rule into a directive the proactive agent can honor.

    Returns {"kind": "threshold", "ingredient_id", "threshold"} for
    "kabari kalau cabai di bawah 3 kg"-style rules, {"kind": "mute",
    "ingredient_id"} for "jangan ingatkan stok telur"-style rules, or None
    when the text names no catalog ingredient or carries no readable number
    — such rules stay free-text context only.
    """
    if not isinstance(text, str) or not text.strip() or not catalog:
        return None
    ids = _ingredient_ids(text, catalog)
    if not ids:
        return None
    if _MUTE.search(text):
        return {"kind": "mute", "ingredient_id": ids[0]}
    match = _THRESHOLD.search(text)
    if not match:
        return None
    threshold = _decimal(match.group("threshold"))
    if threshold is None:
        return None
    return {"kind": "threshold", "ingredient_id": ids[0], "threshold": threshold}


def effective_directives(rules, catalog):
    """Fold parsed rules into (threshold_overrides, muted_ids).

    Rules apply in the order the owner added them; a later rule about the
    same ingredient wins. Only ingredients of this restaurant's catalog are
    affected — a rule can never reach another restaurant.
    """
    overrides, muted = {}, set()
    for entry in rules if isinstance(rules, list) else []:
        if isinstance(entry, dict):
            if entry.get("enabled") is False:
                continue
            text = entry.get("rule")
        else:
            text = entry
        parsed = parse_rule(text, catalog)
        if parsed is None:
            continue
        if parsed["kind"] == "mute":
            muted.add(parsed["ingredient_id"])
            overrides.pop(parsed["ingredient_id"], None)
        else:
            muted.discard(parsed["ingredient_id"])
            overrides[parsed["ingredient_id"]] = parsed["threshold"]
    return overrides, muted
