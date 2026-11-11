"""Field registry: user-selectable fields with declared data sources."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── paths ───────────────────────────────────────────────────────────────────
# Kits and themes live at v2/kits/ and v2/themes/ relative to the worktree root.
# From v2/fields/__init__.py, go up one level to reach them.
_WORKTREE_ROOT = Path(__file__).parent.parent.parent  # fields-kits root
_KITS_DIR = _WORKTREE_ROOT / "v2" / "kits"
_THEMES_DIR = _WORKTREE_ROOT / "v2" / "themes"

# ── field definitions ──────────────────────────────────────────────────────
# Each entry: (name, data_source_fn, default_when_missing)
# data_source_fn receives the public receipt dict and returns the value or None
FIELD_SPECS: Tuple[Tuple[str, ...], ...] = (
    ("tokens_in",),
    ("tokens_out",),
    ("tokens_total",),
    ("cost",),
    ("tool_call_count",),
    ("top_tools",),
    ("duration",),
    ("model",),
    ("provider",),
    ("profile",),
    ("turns",),
    ("files_touched",),
    ("claims_detected",),
    ("verdict_per_claim",),
    ("evidence_locators",),
    ("task_session_id",),
    ("timestamp",),
)


def _load_json(path: Path) -> dict:
    """Load a JSON file, returning an empty dict on failure."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_kits() -> Dict[str, dict]:
    """Load all kit JSON files from the kits directory."""
    kits: Dict[str, dict] = {}
    if _KITS_DIR.is_dir():
        for name in sorted(os.listdir(_KITS_DIR)):
            if name.endswith(".json"):
                data = _load_json(_KITS_DIR / name)
                if "name" in data:
                    kits[data["name"]] = data
    return kits


def _load_themes() -> Dict[str, dict]:
    """Load all theme JSON files from the themes directory."""
    themes: Dict[str, dict] = {}
    if _THEMES_DIR.is_dir():
        for name in sorted(os.listdir(_THEMES_DIR)):
            if name.endswith(".json"):
                data = _load_json(_THEMES_DIR / name)
                if "name" in data:
                    themes[data["name"]] = data
    return themes


#: Auto-loaded kits and themes (populated on import)
KITS: Dict[str, dict] = _load_kits()
THEMES: Dict[str, dict] = _load_themes()


# ── field retrieval ─────────────────────────────────────────────────────────
def get_field(receipt: dict, field_name: str) -> Any:
    """Retrieve a field value from a public receipt dict.

    Walks known data paths; returns "n/a" when the data is absent.
    """
    known = {f[0] for f in FIELD_SPECS}
    if field_name not in known:
        return f"UNKNOWN_FIELD:{field_name}"

    # Direct keys that the public serializer may already populate
    direct_map: Dict[str, Any] = {
        "tokens_in":      receipt.get("tokens_in"),
        "tokens_out":     receipt.get("tokens_out"),
        "tokens_total":   receipt.get("tokens_total"),
        "cost":           receipt.get("cost"),
        "tool_call_count":receipt.get("tool_call_count"),
        "top_tools":      receipt.get("top_tools"),
        "duration":       receipt.get("duration"),
        "model":          receipt.get("model"),
        "provider":       receipt.get("provider"),
        "profile":        receipt.get("profile"),
        "turns":          receipt.get("turns"),
        "files_touched":  receipt.get("files_touched"),
        "claims_detected":receipt.get("claims_detected"),
        "verdict_per_claim":receipt.get("verdict_per_claim"),
        "evidence_locators":receipt.get("evidence_locators"),
        "task_session_id":receipt.get("task_session_id"),
        "timestamp":      receipt.get("timestamp"),
    }

    value = direct_map.get(field_name)
    if value is not None:
        return value
    return "n/a"


def render_fields(receipt: dict, selected: List[str] | None = None) -> Dict[str, str]:
    """Render selected fields as a dict of name --> value.

    :param receipt: public receipt dict (from to_public_receipt / to_public_report)
    :param selected: list of field names to include; None = all visible fields
    :returns: dict of field_name --> value (always a string, never 'None')
    """
    all_names = {f[0] for f in FIELD_SPECS}
    if selected is None:
        selected = [name for name in all_names if name in receipt]
    else:
        selected = [s for s in selected if s in all_names]

    result: Dict[str, str] = {}
    for name in selected:
        result[name] = get_field(receipt, name)
    return result


# ── default kits ───────────────────────────────────────────────────────────
# Each kit is a set of field names (subsets of FIELD_SPECS).
# When a field's data is absent it prints "n/a" automatically.

MINIMAL_KIT: List[str] = ["verdict", "task_session_id"]
VERIFY_KIT: List[str] = ["claims_detected", "verdict_per_claim", "evidence_locators"]
COST_KIT: List[str] = ["tokens_in", "tokens_out", "tokens_total", "cost", "tool_call_count", "duration", "model", "provider"]
FULL_KIT: List[str] = [name for name, *_ in FIELD_SPECS]
OPS_KIT: List[str] = ["tool_call_count", "duration", "files_touched", "model", "profile"]


def kit_fields(kit_name: str) -> List[str]:
    """Return the field names for a named kit.

    Looks up the kit in the auto-loaded KITS dict first, then falls back
    to the hard-coded defaults.
    """
    if kit_name in KITS:
        return KITS[kit_name].get("fields", MINIMAL_KIT)
    kits = {
        "minimal":   MINIMAL_KIT,
        "verify":    VERIFY_KIT,
        "cost":      COST_KIT,
        "full":      FULL_KIT,
        "ops":       OPS_KIT,
    }
    return kits.get(kit_name, MINIMAL_KIT)


def auto_fields(categories: dict) -> List[str]:
    """Data-driven auto kit from session classifier categories.

    Starts with mandatory verdict fields. Adds fields based on when_any rules.
    categories maps category names to True/False based on session type.
    """
    fields: List[str] = ["verdict", "task_session_id"]
    rules: List[Dict[str, Any]] = [
        {"when_any": {"tests_run": True},          "fields": ["tokens_in", "tokens_out", "tokens_total", "cost", "tool_call_count", "top_tools", "duration"]},
        {"when_any": {"pushed_or_published": True}, "fields": ["cost", "tool_call_count", "top_tools", "duration"]},
        {"when_any": {"files_changed": True},      "fields": ["files_touched", "model", "provider"]},
        {"when_any": {"external_side_effect": True}, "fields": ["cost", "evidence_locators"]},
        {"when_any": {"unsurfaced_error": True},   "fields": ["cost", "duration"]},
        {"when_any": {"claims_done": True},        "fields": ["claims_detected", "verdict_per_claim", "evidence_locators"]},
        {"when_any": {"tests_passed": True},       "fields": ["tokens_in", "tokens_out", "tokens_total", "cost"]},
        {"when_any": {"files_changed": True},      "fields": ["files_touched", "model", "provider"]},
        {"when_any": {"session_type": "ops",       "jev_session_type": "ops"}, "fields": ["tool_call_count", "top_tools", "duration"]},
        {"when_any": {"session_type": "research",  "jev_session_type": "research"}, "fields": ["sources"]},
        {"when_any": {"session_type": "media",     "jev_session_type": "media"}, "fields": ["artifacts"]},
    ]

    for rule in rules:
        when_any = rule["when_any"]
        if any(categories.get(key) == value for key, value in when_any.items()):
            fields.extend(rule["fields"])
    # Dedup while preserving order
    return list(dict.fromkeys(fields))


# ── themes ─────────────────────────────────────────────────────────────────
def theme(name: str) -> dict:
    """Return the theme dict for the given name, or an empty dict."""
    return THEMES.get(name, {})


# ── kit field availability ─────────────────────────────────────────────────
def kit_has_field(kit_name: str, field_name: str) -> bool:
    """Check whether a kit includes a given field name."""
    fields = kit_fields(kit_name)
    return field_name in fields