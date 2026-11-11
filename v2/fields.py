"""Receipt content registry (fields + kits) and the separate visual-theme layer.

Two deliberately independent layers:

* **content** — :data:`FIELDS` is an ordered registry. Every field declares the
  data source it reads. A *kit* (built-in JSON under ``v2/kits``, or a user JSON
  under the plugin config dir) or an explicit ``--fields`` list chooses which
  fields print. A field whose declared source has no data prints ``n/a`` and
  never a guess.
* **aesthetics** — a *theme* only chooses paper, ink, glyph set, font and paper
  furniture. It can never remove a line, reword a verdict, invent a glyph, or
  drop a verdict below :data:`MIN_VERDICT_CONTRAST`. A theme file carrying a key
  that tries to hide or soften a verdict is **rejected**, not silently patched.

The completion verdict and the per-claim verdicts are mandatory content: the
verdict section is in :data:`MANDATORY_SECTIONS`, so no kit and no field list can
remove it, and :func:`worst_verdict` can only ever *downgrade* a completion claim.

The verdict → (glyph, word, colour role) mapping lives in code here, never in a
theme file.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA_VERSION = 2

# ── verdict semantics (code-owned; themes and kits cannot override) ─────────
#: Every verdict prints a glyph *and* the word, in every kit and every theme.
VERDICT_GLYPHS = {'supported': '\u2713', 'contradicted': '\u2717',
                  'unverified': '?', 'not_evaluable': '\u2013'}
#: 7-bit fallback for the ``ascii`` glyph set (terminals, code blocks, plain printers).
ASCII_GLYPHS = {'supported': '[OK]', 'contradicted': '[X]',
                'unverified': '[?]', 'not_evaluable': '[-]'}
VERDICT_WORDS = {'supported': 'SUPPORTED', 'contradicted': 'CONTRADICTED',
                 'unverified': 'UNVERIFIED', 'not_evaluable': 'NOT EVALUABLE'}
#: Severity ranking. ``worst_verdict`` takes the maximum, so a completion claim
#: can be downgraded by a required criterion but never upgraded by one.
VERDICT_SEVERITY = {'supported': 0, 'not_evaluable': 1, 'unverified': 2, 'contradicted': 3}


def worst_verdict(verdicts: Iterable[str]) -> str:
    """Return the most severe known verdict in *verdicts* (``unverified`` if empty).

    Only downgrades: a set containing nothing worse than ``supported`` returns
    ``supported``; one weakly-supported criterion can never lift a
    ``contradicted`` completion back to green.
    """
    known = [v for v in verdicts if v in VERDICT_SEVERITY]
    if not known:
        return 'unverified'
    return max(known, key=lambda v: VERDICT_SEVERITY[v])


def verdict_glyph(verdict: str, glyph_set: str = 'unicode') -> str:
    """Fixed glyph for a verdict; unknown verdicts get the not-evaluable glyph."""
    table = ASCII_GLYPHS if glyph_set == 'ascii' else VERDICT_GLYPHS
    return table.get(verdict, table['not_evaluable'])


def verdict_word(verdict: str) -> str:
    return VERDICT_WORDS.get(verdict, VERDICT_WORDS['not_evaluable'])


# ── structure ──────────────────────────────────────────────────────────────
#: Sections every receipt prints, whatever the kit says.
MANDATORY_SECTIONS = ('verdict', 'criteria', 'footer')
#: Sections a kit may switch on (the field block prints whenever a kit lists fields).
OPTIONAL_SECTIONS = ('counts', 'evidence')
#: Words a kit section name may never contain: a kit cannot ask to drop a verdict.
_FORBIDDEN_SECTION_TOKENS = ('hide', 'soften', 'suppress', 'skip', 'drop', 'verdict')
#: Theme keys that try to change verdict visibility or typography; rejected on load.
THEME_FORBIDDEN_KEYS = frozenset({
    'hide_unsupported', 'hide_contradicted', 'hide_unverified', 'hide_verdict', 'hide_verdicts',
    'soften_contradicted', 'soften_unsupported', 'soften_verdict', 'suppress_verdict',
    'verdict_opacity', 'mute_verdict', 'mute_verdicts', 'verdict_glyphs', 'glyphs',
    'glyph_overrides', 'verdict_words', 'relabel_verdicts',
})
#: Minimum WCAG contrast ratio for verdict ink against its own paper.
MIN_VERDICT_CONTRAST = 4.5
#: Default kit when the caller selects none.
DEFAULT_KIT = 'verify'
#: Glyph sets, fonts and paper furniture a theme may choose from.
GLYPH_SETS = ('unicode', 'ascii')
FONTS = ('standard', 'dot')
PERFORATIONS = ('torn', 'tractor', 'none')

#: Text-valued metric keys (all other accepted metrics are non-negative numbers).
TEXT_METRICS = ('model', 'provider', 'profile', 'session', 'agent', 'cost_scope')
#: Numeric metric keys.
NUMBER_METRICS = ('tokens_in', 'tokens_out', 'tokens_total', 'cost_usd', 'tool_calls',
                  'duration_seconds', 'files_touched', 'turns')
#: Reviewed allowlist of metric keys that may enter a public receipt.
METRIC_KEYS = (*TEXT_METRICS, *NUMBER_METRICS, 'top_tools')
#: Metrics that identify one run. Public receipts never print these raw: each is
#: replaced by a non-reversible ``sha256-<12>`` pseudonym (the same rule the
#: public serializer already applies to artifact versions).
CONFIDENTIAL_METRICS = {'session': 'pseudonym', 'task_session_id': 'pseudonym',
                        'profile': 'pseudonym', 'agent': 'pseudonym'}

_HEX = re.compile(r'#[0-9a-fA-F]{6}\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')


class ThemeError(ValueError):
    """A theme file broke the verdict-visibility invariant."""


class KitError(ValueError):
    """A kit file asked for something a kit may not do."""


# ── colour maths ───────────────────────────────────────────────────────────
def _srgb(channel: float) -> float:
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    """Relative luminance of an ``#rrggbb`` colour (WCAG 2.1)."""
    if not isinstance(color, str) or not _HEX.match(color):
        raise ValueError('colour must be #rrggbb')
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b)


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colours (>= 1.0)."""
    a, b = luminance(first), luminance(second)
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


def _safe_ink(paper: str) -> str:
    return '#111111' if luminance(paper) > 0.5 else '#f5f5f5'


# ── paths ──────────────────────────────────────────────────────────────────
_BUILTIN_KITS_DIR = Path(__file__).resolve().parent / 'kits'
_BUILTIN_THEMES_DIR = Path(__file__).resolve().parent / 'themes'
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    """Directory users drop their own ``kits/`` and ``themes/`` files into."""
    configured = os.environ.get('HERMES_RECEIPTS_CONFIG_DIR')
    return (Path(configured).expanduser() if configured
            else Path.home() / '.hermes' / 'receipts')


def _load_json_dir(directory: Path) -> dict[str, dict]:
    loaded: dict[str, dict] = {}
    if not directory.is_dir():
        return loaded
    for path in sorted(directory.glob('*.json')):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get('name'), str) and data['name']:
            loaded[data['name']] = data
    return loaded


# ── field registry ─────────────────────────────────────────────────────────
def _as_text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return str(int(value)) if float(value).is_integer() else f'{value:g}'
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(metrics: Mapping, key: str, formatter: Callable[[float], str]) -> str | None:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return formatter(value)


class Field:
    """One selectable receipt field and the data source it declares."""

    __slots__ = ('name', 'label', 'section', 'source', 'resolve')

    def __init__(self, name: str, label: str, section: str, source: str,
                 resolve: Callable[[Mapping], Any]) -> None:
        self.name, self.label, self.section = name, label, section
        self.source, self.resolve = source, resolve

    def value(self, view: Mapping) -> str | None:
        """Resolve the field, returning ``None`` (never a guess) when absent."""
        try:
            resolved = self.resolve(view)
        except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError):
            return None
        return _as_text(resolved)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f'Field({self.name!r}, source={self.source!r})'


def _metrics(view: Mapping) -> Mapping:
    metrics = view.get('metrics')
    return metrics if isinstance(metrics, Mapping) else {}


def _criteria(view: Mapping) -> list:
    criteria = view.get('criteria')
    return [c for c in criteria if isinstance(c, Mapping)] if isinstance(criteria, (list, tuple)) else []


def _verdict_text(value: Any) -> str | None:
    if not isinstance(value, str) or value not in VERDICT_WORDS:
        return None
    return f'{verdict_word(value)} {VERDICT_GLYPHS[value]}'


FIELDS: tuple[Field, ...] = (
    # verdict (mandatory content, resolved by the verdict section) ------------
    Field('verdict', 'COMPLETION', 'verdict', 'public.completion',
          lambda v: _verdict_text(v.get('completion'))),
    # identity ---------------------------------------------------------------
    Field('task_session_id', 'TASK/SESSION ID', 'identity', 'public.receipt_id',
          lambda v: v.get('receipt_id')),
    Field('artifact_version', 'VERSION', 'identity', 'public.artifact_version',
          lambda v: v.get('artifact_version')),
    Field('detector_version', 'DETECTOR', 'identity', 'public.detector_version',
          lambda v: v.get('detector_version')),
    Field('matcher_version', 'MATCHER', 'identity', 'public.matcher_version',
          lambda v: v.get('matcher_version')),
    Field('timestamp', 'GENERATED', 'identity', 'public.timestamp',
          lambda v: v.get('timestamp')),
    # claims and evidence ----------------------------------------------------
    Field('claims_detected', 'CLAIMS DETECTED', 'claims', 'public.criteria',
          lambda v: f'{len(_criteria(v))} declared' if _criteria(v) else None),
    Field('verdict_per_claim', 'CLAIM VERDICTS', 'claims', 'public.criteria[].verdict',
          lambda v: '; '.join(f"{c.get('label')}: {verdict_word(c.get('verdict'))} "
                              f"{verdict_glyph(c.get('verdict'), v.get('glyph_set', 'unicode'))}"
                              for c in _criteria(v)) or None),
    Field('evidence_locators', 'EVIDENCE', 'claims', 'public.criteria[].evidence',
          lambda v: '; '.join(f"{c.get('label')}: {', '.join(c.get('evidence') or [])}"
                              for c in _criteria(v) if c.get('evidence')) or None),
    # cost -------------------------------------------------------------------
    Field('tokens_in', 'TOKENS IN', 'cost', 'metrics.tokens_in',
          lambda v: _number(_metrics(v), 'tokens_in', lambda n: f'{n:,.0f}')),
    Field('tokens_out', 'TOKENS OUT', 'cost', 'metrics.tokens_out',
          lambda v: _number(_metrics(v), 'tokens_out', lambda n: f'{n:,.0f}')),
    Field('tokens_total', 'TOKENS TOTAL', 'cost', 'metrics.tokens_total',
          lambda v: _number(_metrics(v), 'tokens_total', lambda n: f'{n:,.0f}')),
    Field('cost', 'COST (EST.)', 'cost', 'metrics.cost_usd',
          lambda v: _number(_metrics(v), 'cost_usd', lambda n: f'${n:.4f}')),
    Field('cost_scope', 'COST SCOPE', 'cost', 'metrics.cost_scope',
          lambda v: _as_text(_metrics(v).get('cost_scope'))),
    # ops --------------------------------------------------------------------
    Field('tool_call_count', 'TOOL CALLS', 'ops', 'metrics.tool_calls',
          lambda v: _number(_metrics(v), 'tool_calls', lambda n: f'{n:,.0f}')),
    Field('top_tools', 'TOP TOOLS', 'ops', 'metrics.top_tools',
          lambda v: ', '.join(f'{name} x{int(count)}'
                              for name, count in (_metrics(v).get('top_tools') or [])) or None),
    Field('duration', 'DURATION', 'ops', 'metrics.duration_seconds',
          lambda v: _number(_metrics(v), 'duration_seconds', lambda n: f'{n:,.1f}s')),
    Field('files_touched', 'FILES TOUCHED', 'ops', 'metrics.files_touched',
          lambda v: _number(_metrics(v), 'files_touched', lambda n: f'{n:,.0f}')),
    Field('turns', 'TURNS', 'ops', 'metrics.turns',
          lambda v: _number(_metrics(v), 'turns', lambda n: f'{n:,.0f}')),
    # context ----------------------------------------------------------------
    Field('model', 'MODEL', 'context', 'metrics.model',
          lambda v: _as_text(_metrics(v).get('model'))),
    Field('provider', 'PROVIDER', 'context', 'metrics.provider',
          lambda v: _as_text(_metrics(v).get('provider'))),
    Field('profile', 'PROFILE', 'context', 'metrics.profile',
          lambda v: _as_text(_metrics(v).get('profile'))),
    # fine print -------------------------------------------------------------
    Field('session', 'SESSION ID', 'fine', 'metrics.session',
          lambda v: _as_text(_metrics(v).get('session'))),
    Field('agent', 'AGENT', 'fine', 'metrics.agent',
          lambda v: _as_text(_metrics(v).get('agent'))),
    Field('plugin_version', 'PLUGIN', 'fine', 'public.plugin_version',
          lambda v: v.get('plugin_version')),
    Field('git_sha', 'PLUGIN SHA', 'fine', 'public.git_sha',
          lambda v: v.get('git_sha')),
    Field('receipt_hash', 'RECEIPT HASH', 'fine', 'public.receipt_hash',
          lambda v: v.get('receipt_hash')),
)
FIELDS_BY_NAME: dict[str, Field] = {field.name: field for field in FIELDS}
#: Fields available to a kit's ``fields`` list.
KNOWN_FIELDS: tuple[str, ...] = tuple(FIELDS_BY_NAME)


def field_view(public: Mapping, glyph_set: str = 'unicode') -> dict:
    """Normalize a public receipt into the mapping field resolvers read."""
    metrics = public.get('metrics')
    return {'completion': public.get('completion'), 'criteria': public.get('criteria') or [],
            'receipt_id': public.get('receipt_id'), 'window': public.get('window'),
            'artifact_version': public.get('artifact_version'),
            'detector_version': public.get('detector_version'),
            'matcher_version': public.get('matcher_version'),
            'plugin_version': public.get('plugin_version'), 'git_sha': public.get('git_sha'),
            'receipt_hash': public.get('receipt_hash'), 'timestamp': public.get('timestamp'),
            'glyph_set': glyph_set,
            'metrics': metrics if isinstance(metrics, Mapping) else {}}


def validate_fields(names: Iterable[str]) -> list[str]:
    """Return the ordered, de-duplicated field list; raise on unknown names."""
    if isinstance(names, str):
        names = [part.strip() for part in names.split(',') if part.strip()]
    ordered: list[str] = []
    unknown: list[str] = []
    for name in names:
        if name not in FIELDS_BY_NAME:
            unknown.append(str(name))
        elif name not in ordered:
            ordered.append(name)
    if unknown:
        raise KitError('unknown receipt field(s): ' + ', '.join(sorted(unknown))
                       + ' (known: ' + ', '.join(KNOWN_FIELDS) + ')')
    return ordered


def render_field(name: str, view: Mapping) -> str:
    """Render one field; absent data prints ``n/a`` (never a guess)."""
    field = FIELDS_BY_NAME.get(name)
    if field is None:
        raise KitError(f'unknown receipt field: {name}')
    return field.value(view) or 'n/a'


# ── kits ───────────────────────────────────────────────────────────────────
KITS: dict[str, dict] = {}


def validate_sections(names: Iterable[str] | str) -> tuple[str, ...]:
    """Ordered optional sections; the mandatory ones always print on their own."""
    raw: Iterable[str] = names
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(',') if part.strip()]
    if not isinstance(raw, (list, tuple)):
        raise KitError('sections must be a comma-separated list of section names')
    chosen: list[str] = []
    for name in (str(item) for item in raw):
        lowered = name.lower()
        if name in MANDATORY_SECTIONS:
            raise KitError(f'section {name!r} is mandatory: it prints in every receipt and '
                           f'cannot be listed or removed')
        if any(token in lowered for token in _FORBIDDEN_SECTION_TOKENS):
            raise KitError(f'section {name!r} may not hide or rename a verdict')
        if name not in OPTIONAL_SECTIONS:
            raise KitError(f'unknown section: {name} (optional: {", ".join(OPTIONAL_SECTIONS)})')
        if name not in chosen:
            chosen.append(name)
    return tuple(chosen)


def _kit_sections(data: Mapping) -> tuple[str, ...]:
    return validate_sections(data.get('sections') or [])


def _kit_field_names(data: Mapping) -> list[str]:
    names: list[str] = []
    for key in ('fields', 'required_fields', 'optional_fields'):
        value = data.get(key)
        if isinstance(value, list):
            names.extend(str(item) for item in value)
    return validate_fields(names)


def kit_sections(name: str) -> tuple[str, ...]:
    """Mandatory sections plus whatever optional sections the kit asks for."""
    if name == 'auto':
        # The auto kit adds *fields* from measured categories; it never adds sections.
        return tuple(MANDATORY_SECTIONS)
    data = KITS.get(name)
    if data is None:
        raise KitError(f'unknown receipt kit: {name} (available: {", ".join(kit_names())})')
    return tuple(MANDATORY_SECTIONS) + _kit_sections(data)


def kit_fields(name: str, categories: Mapping | None = None) -> list[str]:
    """Field list for a named kit, in print order; raises for an unknown kit."""
    if name == 'auto':
        return auto_kit_fields(categories or {})
    data = KITS.get(name)
    if data is None:
        raise KitError(f'unknown receipt kit: {name} (available: {", ".join(kit_names())})')
    return _kit_field_names(data)


def kit_experimental_kinds(name: str) -> tuple[str, ...]:
    """Claim kinds this kit prints with an ``(experimental)`` marker."""
    data = KITS.get(name) or {}
    kinds = data.get('experimental_kinds') or []
    return tuple(str(k) for k in kinds) if isinstance(kinds, (list, tuple)) else ()


def kit_names() -> list[str]:
    return sorted([*KITS, 'auto'])


def kit_descriptions() -> dict[str, str]:
    return {name: str(data.get('description') or '') for name, data in sorted(KITS.items())}


def kit_path(name: str) -> Path | None:
    """On-disk location of a built-in kit (``None`` for the computed ``auto`` kit)."""
    path = _BUILTIN_KITS_DIR / f'{name}.json'
    return path if path.is_file() else None


def reload(config: Path | str | None = None) -> None:
    """(Re)load built-in and user kits/themes.

    A user file with the same ``name`` as a built-in overrides it, so a
    user-supplied ``kits/mine.json`` is selectable with ``--kit mine`` without a
    host restart. Loading is best-effort for *unrelated* files; a malformed user
    file is skipped rather than breaking the plugin.
    """
    global KITS, THEMES
    user_dir = Path(config).expanduser() if config else config_dir()
    KITS = {**_load_json_dir(_BUILTIN_KITS_DIR), **_load_json_dir(user_dir / 'kits')}
    THEMES = {**_load_json_dir(_BUILTIN_THEMES_DIR), **_load_json_dir(user_dir / 'themes')}


def select_fields(public: Mapping, kit: str | None = None) -> list[str]:
    """Resolve the field selection for a public receipt.

    Precedence: explicit ``fields`` list, then the caller's ``kit``, then the
    receipt's own ``kit``, then :data:`DEFAULT_KIT`.
    """
    explicit = public.get('fields')
    if explicit:
        return validate_fields(explicit)
    return kit_fields(kit or public.get('kit') or DEFAULT_KIT, public.get('categories'))


def field_block(public: Mapping, glyph_set: str = 'unicode',
                kit: str | None = None) -> list[tuple[str, str]]:
    """``(label, rendered value)`` for every field the receipt's kit selects."""
    view = field_view(public, glyph_set)
    block: list[tuple[str, str]] = []
    for name in select_fields(public, kit):
        field = FIELDS_BY_NAME[name]
        if field.section == 'verdict':
            continue  # rendered by the mandatory verdict section
        block.append((field.label, field.value(view) or 'n/a'))
    return block


def auto_kit_fields(categories: Mapping) -> list[str]:
    """Data-driven kit from measured session categories.

    Consumes the reviewed ``kits/auto.json`` decision rules (never a hardcoded
    rule table) and keeps only fields that exist in the v2 registry. Rules whose
    v1 field has no v2 equivalent are dropped rather than invented.
    """
    rules_path = _PLUGIN_ROOT / 'kits' / 'auto.json'
    try:
        rules = json.loads(rules_path.read_text())
    except (OSError, ValueError):
        return ['task_session_id']
    chosen: list[str] = []
    for rule in rules.get('rules') or []:
        when_any = rule.get('when_any') or {}
        if not isinstance(when_any, Mapping):
            continue
        if any(categories.get(key) == value for key, value in when_any.items()):
            chosen.extend(str(name) for name in rule.get('fields') or [])
    kept = [name for name in chosen if name in FIELDS_BY_NAME and name != 'verdict']
    return list(dict.fromkeys(['task_session_id', *kept]))


# ── themes ─────────────────────────────────────────────────────────────────
THEMES: dict[str, dict] = {}

_FALLBACK_THEME = {
    'name': 'thermal-classic', 'frame': '#272727', 'paper': '#f4efdf', 'ink': '#222620',
    'ink_supported': '#127847', 'ink_contradicted': '#a73532', 'ink_unverified': '#8a6d1a',
    'ink_not_evaluable': '#666557', 'ink_muted': '#666557', 'mono': False,
    'glyph_set': 'unicode', 'font': 'standard', 'perforation': 'torn', 'green_bar': False,
}


def load_theme(name: str | None = None) -> dict:
    """Normalized theme for *name*; validates the verdict-visibility invariant.

    Raises :class:`ThemeError` for an unknown theme, a forbidden key, a missing or
    malformed ink, an unknown glyph set/font/perforation, or verdict inks that are
    illegible or (outside a declared monochrome theme) indistinguishable.
    """
    if not name:
        return dict(_FALLBACK_THEME)
    raw = THEMES.get(name)
    if raw is None:
        raise ThemeError(f'unknown receipt theme: {name} (available: {", ".join(theme_names())})')
    if not isinstance(raw, Mapping):
        raise ThemeError(f'theme {name!r} must be an object')
    forbidden = sorted(set(raw) & THEME_FORBIDDEN_KEYS)
    if forbidden:
        raise ThemeError(f'theme {name!r} may not set {", ".join(forbidden)}: '
                         'a theme can never hide or soften a verdict')
    theme = dict(_FALLBACK_THEME)
    theme['name'] = name
    for key in ('frame', 'paper', 'ink', 'ink_muted', 'ink_supported', 'ink_contradicted',
                'ink_unverified', 'ink_not_evaluable'):
        if key in raw:
            value = raw[key]
            if not isinstance(value, str) or not _HEX.match(value):
                raise ThemeError(f'theme {name!r} key {key!r} must be #rrggbb')
            theme[key] = value.lower()
    if isinstance(raw.get('brand_text'), str) and raw['brand_text'].strip():
        theme['brand_text'] = raw['brand_text'].strip()[:48]
    mono = raw.get('mono', False)
    if not isinstance(mono, bool):
        raise ThemeError(f'theme {name!r} key "mono" must be a boolean')
    theme['mono'] = mono
    glyph_set = raw.get('glyph_set', 'unicode')
    if glyph_set not in GLYPH_SETS:
        raise ThemeError(f'theme {name!r} glyph_set must be one of {", ".join(GLYPH_SETS)}')
    theme['glyph_set'] = glyph_set
    font = raw.get('font', 'standard')
    if font not in FONTS:
        raise ThemeError(f'theme {name!r} font must be one of {", ".join(FONTS)}')
    theme['font'] = font
    perforation = raw.get('perforation', 'torn')
    if perforation not in PERFORATIONS:
        raise ThemeError(f'theme {name!r} perforation must be one of {", ".join(PERFORATIONS)}')
    theme['perforation'] = perforation
    green_bar = raw.get('green_bar', False)
    if not isinstance(green_bar, bool):
        raise ThemeError(f'theme {name!r} key "green_bar" must be a boolean')
    theme['green_bar'] = green_bar
    inks = {verdict: theme['ink_' + verdict] for verdict in VERDICT_WORDS}
    if mono:
        # One ink only. A mono theme may not smuggle a green in for `supported`.
        if set(inks.values()) - {theme['ink']}:
            raise ThemeError(f'theme {name!r} declares mono but sets a second ink')
    else:
        if len(set(inks.values())) != len(inks):
            raise ThemeError(f'theme {name!r} must give every verdict its own ink')
    for verdict, ink in inks.items():
        if contrast_ratio(ink, theme['paper']) < MIN_VERDICT_CONTRAST:
            raise ThemeError(f'theme {name!r} {verdict} ink is below '
                             f'{MIN_VERDICT_CONTRAST:g}:1 against its paper')
    return theme


def verdict_ink(theme: Mapping, verdict: str) -> str:
    """Ink for a verdict line, contrast-enforced; unknown verdicts use muted ink."""
    proposed = theme.get('ink_' + verdict)
    ink = proposed if isinstance(proposed, str) and _HEX.match(proposed) else ''
    fallback_ink = str(theme.get('ink') or '')
    if not ink:
        ink = fallback_ink if _HEX.match(fallback_ink) else str(_FALLBACK_THEME['ink'])
    proposed_paper = str(theme.get('paper') or '')
    paper = proposed_paper if _HEX.match(proposed_paper) else str(_FALLBACK_THEME['paper'])
    if contrast_ratio(ink, paper) < MIN_VERDICT_CONTRAST:
        return _safe_ink(paper)
    return ink


def theme_names() -> list[str]:
    return sorted(THEMES) or ['thermal-classic']


def theme_descriptions() -> dict[str, str]:
    return {name: str(data.get('description') or '') for name, data in sorted(THEMES.items())}


# ── reviewed metrics ───────────────────────────────────────────────────────
def _clean_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if value >= 0 else None


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    # 120 chars fits a pseudonymous id (`sha256-` + 12) or a raw `local-<64 hex>`.
    return text if text and len(text) <= 120 and '\n' not in text else None


def _clean_top_tools(value: Any) -> list[list] | None:
    if not isinstance(value, (list, tuple)):
        return None
    cleaned: list[list] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            name, count = _clean_text(item[0]), _clean_number(item[1])
        else:
            name, count = _clean_text(item), None
        if name is not None:
            cleaned.append([name, int(count) if isinstance(count, (int, float)) else 1])
    return cleaned[:5] or None


def sanitize_metrics(raw: Any) -> dict:
    """Copy only reviewed metric keys, with bounded, non-guessing values."""
    if not isinstance(raw, Mapping):
        return {}
    cleaned: dict[str, Any] = {}
    for key in METRIC_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        result = (_clean_top_tools(value) if key == 'top_tools'
                  else _clean_text(value) if key in TEXT_METRICS
                  else _clean_number(value))
        if result is not None:
            cleaned[key] = result
    return cleaned


def pseudonymize(value: Any) -> str | None:
    """Non-reversible ``sha256-<12>`` label for a raw identifier.

    The same rule the public receipt already uses for artifact versions: the
    prefix is published, the identifier itself never is.
    """
    text = _as_text(value)
    if text is None:
        return None
    if _SHA256.fullmatch(text):
        digest = text
    else:
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return 'sha256-' + digest[:12]


def public_metrics(raw: Any) -> dict:
    """Sanitized metrics with the confidential identifiers removed.

    ``session``/``task_session_id``/``profile``/``agent`` become non-reversible
    ``sha256-<12>`` pseudonyms, so two receipts can be recognised as the same run
    without publishing any raw identifier. Nothing else about a public receipt
    changes, and a missing value stays missing (printed ``n/a``).
    """
    metrics = sanitize_metrics(raw)
    for key, rule in CONFIDENTIAL_METRICS.items():
        if key not in metrics:
            continue
        metrics[key] = pseudonymize(metrics[key]) if rule == 'pseudonym' else 'withheld'
    return metrics


# ── provenance ─────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def plugin_provenance() -> dict:
    """Plugin version from ``plugin.yaml`` and the checkout sha when available."""
    version, sha = 'n/a', 'n/a'
    try:
        for line in (_PLUGIN_ROOT / 'plugin.yaml').read_text().splitlines():
            if line.startswith('version:'):
                candidate = line.split(':', 1)[1].strip().strip('"\'')
                if candidate:
                    version = candidate
                break
    except OSError:
        pass
    try:
        result = subprocess.run(['git', '-C', str(_PLUGIN_ROOT), 'rev-parse', '--short=12', 'HEAD'],
                                capture_output=True, text=True, timeout=3, check=False)
        candidate = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{7,40}', candidate):
            sha = candidate
    except (OSError, subprocess.SubprocessError):
        pass
    return {'plugin_version': version, 'git_sha': sha}


def metrics_from_state(db_path: Path | str, session_id: str | None = None,
                       turn_id: int | None = None) -> dict:
    """Read-only reviewed metrics for one real Hermes session."""
    from .telemetry import facts_from_state
    return sanitize_metrics(facts_from_state(db_path, session_id=session_id, turn_id=turn_id))


reload()
