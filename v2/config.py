"""One documented ``receipts.yaml``: everything about the printer a user configures.

The user configures *paper and ink*, never the verdict. One file in the plugin
config dir (:func:`v2.fields.config_dir`, ``~/.hermes/receipts`` by default) covers:

``kit`` / ``theme`` / ``fields``
    which content and which look, exactly like the CLI flags.
``paper`` / ``columns``
    a width preset (58 mm, 80 mm, A4, letter, a Discord code block) or a custom
    column count. Wrapping, truncation and the PNG geometry all follow it.
``sections`` / ``order``
    which sections print and in what order. ``verdict`` and ``criteria`` are
    mandatory: asking for them off, or leaving them out of ``order``, is an error.
``header`` / ``footer``
    the shop name, tagline and "thank you" line. User text may not name a verdict,
    so a custom header can never fake a verdict line.
``extra_fields``
    user-defined fine-print fields: a template over declared session data
    (``"{tokens_total:,.0f} tok - ${cost_usd:.4f}"``) or a static key/value pair.
    A field whose data is missing prints ``n/a``.
``logo`` / ``barcode``
    an optional logo dithered to 1-bit for thermal paper, and an optional Code 128
    barcode over the receipt-hash prefix or a verify URL.
``printer``
    USB options: cut, feed lines, density, copies.
``triggers``
    when receipts print by themselves (task completion, goal loops, session ends,
    claim exceptions, a digest) and where each one goes. Defaults are quiet: a fresh
    install writes the local PNG history and says nothing in chat.
``include``
    what a receipt contains: claim kinds, experimental flags, which tools are listed,
    the redaction level, the claim cap, and whether cost/tokens/model print. It can
    hide detail; it can never move the verdict block.

Every validation failure names the key that is wrong, e.g.::

    receipts.yaml: paper: unknown paper preset '99mm' (known: ...)
    receipts.yaml: sections.verdict: section 'verdict' is mandatory ...
"""
from __future__ import annotations

import dataclasses
import math
import os
import re
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .fields import (FIELDS_BY_NAME, KNOWN_FIELDS, METRIC_KEYS, NUMBER_METRICS, KitError,
                     ThemeError, VERDICT_GLYPHS, VERDICT_WORDS, config_dir, kit_names,
                     theme_names, validate_fields)
from .include import (DEFAULT_INCLUDE, EXAMPLE_INCLUDE, IncludeConfig, IncludeError, parse_include)
from .triggers import (DEFAULT_TRIGGER_CONFIG, EXAMPLE_TRIGGERS, TriggerConfig, TriggerError,
                       parse_triggers)

#: The file the user edits, and the JSON fallback when PyYAML is unavailable.
CONFIG_FILENAME = 'receipts.yaml'
CONFIG_JSON_FILENAME = 'receipts.json'
SCHEMA_VERSION = 2

#: Every section a config may switch on or off, in the default print order.
SECTIONS: tuple[str, ...] = ('header', 'verdict', 'claims', 'criteria', 'evidence',
                             'fine_print', 'footer')
#: Sections no config may disable: the verdict block is the receipt.
MANDATORY_SECTIONS: tuple[str, ...] = ('verdict', 'criteria')
#: Sections an ``order`` must list: the verdict block, and the disclaimer footer (a
#: receipt cannot be configured to hide the caveat that it is not proof of completeness).
ORDER_REQUIRED_SECTIONS: tuple[str, ...] = ('verdict', 'criteria', 'footer')
#: Accepted aliases for a section name (the kit vocabulary calls the counts block
#: ``counts``; the printer vocabulary calls it what it prints).
SECTION_ALIASES = {'counts': 'claims'}

#: Paper presets. ``columns`` drives wrapping, truncation, the ASCII printer and the
#: PNG geometry; ``left``/``right`` are the PNG margins in pixels.
PAPERS: dict[str, dict[str, Any]] = {
    '58mm': {'columns': 32, 'left': 46, 'right': 34, 'label': '58 mm roll (32 columns)'},
    '80mm': {'columns': 48, 'left': 54, 'right': 34, 'label': '80 mm roll (48 columns)'},
    'a4': {'columns': 80, 'left': 96, 'right': 72, 'label': 'A4 sheet (80 columns)'},
    'letter': {'columns': 80, 'left': 96, 'right': 72, 'label': 'US letter sheet (80 columns)'},
    'discord': {'columns': 60, 'left': 58, 'right': 40, 'label': 'Discord code block (60 columns)'},
    'roll': {'columns': 45, 'left': 58, 'right': 34, 'label': 'legacy roll (45 columns)'},
    'custom': {'columns': None, 'left': 54, 'right': 34, 'label': 'custom column count'},
}
#: Default paper when a config selects none (the geometry the v1 receipts shipped).
DEFAULT_PAPER = 'roll'
MIN_COLUMNS, MAX_COLUMNS = 20, 200

#: Barcode payload choices. ``qr`` needs a pure-python QR dependency; see
#: :func:`v2.branding.qr_provider`.
BARCODE_MODES = ('none', 'hash', 'url', 'qr')
#: Longest user-visible strings a config may inject.
MAX_SHOP_NAME, MAX_TAGLINE, MAX_THANKS, MAX_LABEL, MAX_STATIC = 40, 60, 60, 32, 120

_HEX6 = re.compile(r'#[0-9a-fA-F]{6}\Z')
_KEY = re.compile(r'[A-Za-z0-9_]{1,32}\Z')
_PLACEHOLDER = re.compile(r'\{([A-Za-z0-9_]+)(?::([^{}]*))?\}')
#: Fields whose value is a raw number, so a template may give them a format spec.
NUMERIC_FIELDS: tuple[str, ...] = ('tokens_in', 'tokens_out', 'tokens_total', 'cost',
                                   'tool_call_count', 'duration', 'files_touched', 'turns')
#: A field name that resolves to a raw metric: the placeholder `{cost:.4f}` reads
#: ``metrics.cost_usd`` exactly as the COST field does, so a template's format spec works.
FIELD_METRIC: dict[str, str] = {
    'tokens_in': 'tokens_in', 'tokens_out': 'tokens_out', 'tokens_total': 'tokens_total',
    'cost': 'cost_usd', 'tool_call_count': 'tool_calls', 'duration': 'duration_seconds',
    'files_touched': 'files_touched', 'turns': 'turns',
}
#: Every placeholder that carries a raw number, mapped to the metric it reads.
NUMERIC_PLACEHOLDERS: dict[str, str] = {**{key: key for key in NUMBER_METRICS}, **FIELD_METRIC}
_PLACEHOLDER_NAMES = frozenset(KNOWN_FIELDS) | frozenset(METRIC_KEYS)
_FORMATTABLE = frozenset(NUMERIC_PLACEHOLDERS)


class ConfigError(ValueError):
    """A ``receipts.yaml`` value broke the schema or the verdict invariant."""


# ── small validators that always name the key ──────────────────────────────
def _fail(source: str, key: str, message: str) -> ConfigError:
    return ConfigError(f'{source}: {key}: {message}')


def _as_mapping(value: Any, source: str, key: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise _fail(source, key, 'must be a mapping of keys to values')
    return value


def _as_text(value: Any, source: str, key: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(source, key, 'must be a non-empty string')
    text = value.strip()
    if '\n' in text or '\r' in text:
        raise _fail(source, key, 'must be a single line')
    if len(text) > limit:
        raise _fail(source, key, f'must be at most {limit} characters')
    return text


def _as_int(value: Any, source: str, key: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _fail(source, key, 'must be a number')
    if float(value) != int(value):
        raise _fail(source, key, 'must be a whole number')
    number = int(value)
    if not low <= number <= high:
        raise _fail(source, key, f'must be between {low} and {high}')
    return number


def _as_bool(value: Any, source: str, key: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(source, key, 'must be true or false')
    return value


def _as_str_list(value: Any, source: str, key: str) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(',') if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise _fail(source, key, 'must be a list of names')
    return [_as_text(item, source, f'{key}[{index}]', 64) for index, item in enumerate(value)]


def _reject_verdict_text(source: str, key: str, text: str) -> str:
    """User text may not name a verdict: a header can never fake the verdict block."""
    upper = text.upper()
    for verdict, word in VERDICT_WORDS.items():
        if word in upper or text.count(VERDICT_GLYPHS[verdict]):
            raise _fail(source, key, f'may not contain the verdict word or glyph {word!r}: '
                                      'the verdict block is not configurable')
    return text


# ── custom fine-print fields ───────────────────────────────────────────────
@dataclass(frozen=True)
class CustomField:
    """One user-defined fine-print line: a template over session data, or static text."""

    key: str
    label: str
    template: str | None = None
    value: str | None = None

    def render(self, view: Mapping) -> str:
        """The line's value; ``n/a`` when referenced data is missing (never a guess)."""
        if self.value is not None:
            return self.value
        assert self.template is not None  # guaranteed by validation
        missing = False

        def substitute(match: re.Match) -> str:
            nonlocal missing
            resolved = resolve_placeholder(match.group(1), match.group(2), view)
            if resolved is None:
                missing = True
                return ''
            return resolved

        filled = _PLACEHOLDER.sub(substitute, self.template)
        return 'n/a' if missing else filled


def resolve_placeholder(name: str, spec: str | None, view: Mapping) -> str | None:
    """Resolve one ``{name}`` / ``{name:fmt}`` placeholder against a receipt view.

    A bare metric name (``{tokens_total}``) or any name carrying a format spec
    (``{cost:.4f}``) reads the raw number and formats it. Everything else falls to
    the field registry, then to a text metric. Missing data is ``None`` (-> ``n/a``).
    """
    metrics = view.get('metrics') if isinstance(view.get('metrics'), Mapping) else {}
    source = NUMERIC_PLACEHOLDERS.get(name)
    if source is not None and (spec is not None or name in NUMBER_METRICS):
        raw = metrics.get(source)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return None
        try:
            return format(raw, spec) if spec else f'{raw:,.0f}'
        except (ValueError, TypeError) as exc:
            raise ConfigError(f'bad format spec {{{name}:{spec}}}: {exc}') from exc
    if spec:
        raise ConfigError(f'{{{name}:{spec}}} is not a number; only numeric data takes a format spec')
    field = FIELDS_BY_NAME.get(name)
    if field is not None:
        return field.value(view)
    raw = metrics.get(name)
    return raw if isinstance(raw, str) and raw.strip() else None


def _parse_custom_field(raw: Any, source: str, index: int) -> CustomField:
    key_path = f'extra_fields[{index}]'
    item = _as_mapping(raw, source, key_path)
    unknown = sorted(set(item) - {'key', 'label', 'template', 'value'})
    if unknown:
        raise _fail(source, f'{key_path}.{unknown[0]}',
                    f'unknown key (known: key, label, template, value)')
    key = _as_text(item.get('key'), source, f'{key_path}.key', 32)
    if not _KEY.match(key):
        raise _fail(source, f'{key_path}.key', 'must be letters, digits or underscore')
    label = _reject_verdict_text(source, f'{key_path}.label',
                                 _as_text(item.get('label'), source, f'{key_path}.label', MAX_LABEL))
    has_template, has_value = 'template' in item, 'value' in item
    if has_template == has_value:
        raise _fail(source, key_path, 'needs exactly one of "template" or "value"')
    if has_value:
        value = _reject_verdict_text(source, f'{key_path}.value',
                                     _as_text(item['value'], source, f'{key_path}.value', MAX_STATIC))
        return CustomField(key=key, label=label, value=value)
    template = _as_text(item['template'], source, f'{key_path}.template', 240)
    _reject_verdict_text(source, f'{key_path}.template', template)
    if not _PLACEHOLDER.search(template):
        raise _fail(source, f'{key_path}.template', 'must reference at least one {placeholder}')
    for match in _PLACEHOLDER.finditer(template):
        name, spec = match.group(1), match.group(2)
        if name not in _PLACEHOLDER_NAMES:
            raise _fail(source, f'{key_path}.template',
                        f'unknown placeholder {{{name}}} (known data: '
                        f'{", ".join(sorted(_PLACEHOLDER_NAMES))})')
        if spec is not None and name not in _FORMATTABLE:
            raise _fail(source, f'{key_path}.template',
                        f'{{{name}}} is not numeric, so it takes no format spec')
    return CustomField(key=key, label=label, template=template)


# ── sections, order, paper, printer ────────────────────────────────────────
def _canonical_section(name: str) -> str:
    return SECTION_ALIASES.get(name, name)


def _parse_sections(raw: Any, source: str) -> dict[str, bool]:
    if raw is None:
        return {}
    mapping = _as_mapping(raw, source, 'sections')
    chosen: dict[str, bool] = {}
    for name, value in mapping.items():
        canonical = _canonical_section(str(name))
        if canonical not in SECTIONS:
            raise _fail(source, f'sections.{name}',
                        f'unknown section (known: {", ".join(SECTIONS)})')
        enabled = _as_bool(value, source, f'sections.{name}')
        if canonical in MANDATORY_SECTIONS and not enabled:
            raise _fail(source, f'sections.{name}',
                        f'section {canonical!r} is mandatory: the verdict block prints '
                        'on every receipt and cannot be switched off')
        chosen[canonical] = enabled
    return chosen


def _parse_order(raw: Any, source: str, sections: Mapping[str, bool]) -> tuple[str, ...]:
    if raw is None:
        return ()
    order: list[str] = []
    for index, name in enumerate(_as_str_list(raw, source, 'order')):
        canonical = _canonical_section(name)
        if canonical not in SECTIONS:
            raise _fail(source, f'order[{index}]', f'unknown section (known: {", ".join(SECTIONS)})')
        if canonical in order:
            raise _fail(source, f'order[{index}]', f'section {canonical!r} listed twice')
        order.append(canonical)
    for name in ORDER_REQUIRED_SECTIONS:
        if name not in order:
            raise _fail(source, 'order', f'section {name!r} must appear: the verdict block and '
                                         'its disclaimer are not configurable')
    # An ``order`` is a complete statement: every section that will print must be listed,
    # so a section is never dropped from the paper by simply being left out.
    for name in SECTIONS:
        if name in order or sections.get(name) is False:
            continue
        raise _fail(source, 'order', f'section {name!r} is missing from the order; list it, or '
                                     f'switch it off with "sections: {{{name}: false}}"')
    return tuple(order)


def _parse_paper(raw_paper: Any, raw_columns: Any, source: str) -> tuple[str, int]:
    if raw_paper is None and raw_columns is None:
        return DEFAULT_PAPER, PAPERS[DEFAULT_PAPER]['columns']
    if raw_paper is None:
        columns = _as_int(raw_columns, source, 'columns', MIN_COLUMNS, MAX_COLUMNS)
        return 'custom', columns
    paper = _as_text(raw_paper, source, 'paper', 32)
    if paper not in PAPERS:
        raise _fail(source, 'paper', f'unknown paper preset {paper!r} '
                                     f'(known: {", ".join(PAPERS)})')
    columns = PAPERS[paper]['columns']
    if raw_columns is not None:
        custom = _as_int(raw_columns, source, 'columns', MIN_COLUMNS, MAX_COLUMNS)
        if paper != 'custom' and custom != columns:
            raise _fail(source, 'columns',
                        f'set "paper: custom" to use a column count other than {paper}\'s {columns}')
        columns = custom
    if columns is None:
        raise _fail(source, 'columns', 'required when paper is "custom"')
    return paper, columns


def _parse_printer(raw: Any, source: str) -> dict[str, Any]:
    options: dict[str, Any] = {'cut': True, 'feed_lines': 0, 'density': None, 'copies': 1}
    if raw is None:
        return options
    mapping = _as_mapping(raw, source, 'printer')
    for key in sorted(mapping):
        if key not in options:
            raise _fail(source, f'printer.{key}',
                        'unknown key (known: cut, feed_lines, density, copies)')
    if 'cut' in mapping:
        options['cut'] = _as_bool(mapping['cut'], source, 'printer.cut')
    if 'feed_lines' in mapping:
        options['feed_lines'] = _as_int(mapping['feed_lines'], source, 'printer.feed_lines', 0, 10)
    if 'density' in mapping:
        options['density'] = _as_int(mapping['density'], source, 'printer.density', 0, 15)
    if 'copies' in mapping:
        options['copies'] = _as_int(mapping['copies'], source, 'printer.copies', 1, 5)
    return options


def _parse_branding(raw: Mapping, source: str, base_dir: Path | None = None) -> tuple[str | None, str, str | None]:
    logo = raw.get('logo')
    logo_path = None
    if logo is not None:
        text = _as_text(logo, source, 'logo', 400)
        candidate = Path(text).expanduser()
        # A relative logo path is read from the config file's own directory, so one
        # documented receipts.yaml plus its image can live together in the config dir.
        if not candidate.is_absolute():
            candidate = (base_dir or Path.cwd()) / candidate
        logo_path = str(candidate)
        if not candidate.is_file():
            raise _fail(source, 'logo', f'file not found: {candidate.name}')
    barcode = raw.get('barcode', 'none')
    if barcode is None:
        barcode = 'none'
    barcode = _as_text(barcode, source, 'barcode', 16).lower()
    if barcode == 'qr':
        from .branding import qr_provider
        provider = qr_provider()
        if provider is None:
            raise _fail(source, 'barcode', 'the QR dependency (qrcode or segno) is not installed; '
                                          'use "hash" or "url" for the built-in Code 128 barcode')
    if barcode not in BARCODE_MODES:
        raise _fail(source, 'barcode', f'unknown mode {barcode!r} (known: {", ".join(BARCODE_MODES)})')
    url = raw.get('barcode_url')
    barcode_url = None
    if url is not None:
        barcode_url = _reject_verdict_text(source, 'barcode_url',
                                           _as_text(url, source, 'barcode_url', 400))
    if barcode == 'url' and not barcode_url:
        raise _fail(source, 'barcode_url', 'required when barcode is "url"')
    return logo_path, barcode, barcode_url


def _parse_header(raw: Any, source: str) -> dict[str, str]:
    if raw is None:
        return {}
    mapping = _as_mapping(raw, source, 'header')
    limits = {'shop_name': MAX_SHOP_NAME, 'tagline': MAX_TAGLINE}
    for key in sorted(mapping):
        if key not in limits:
            raise _fail(source, f'header.{key}', 'unknown key (known: shop_name, tagline)')
    return {key: _reject_verdict_text(source, f'header.{key}',
                                      _as_text(mapping[key], source, f'header.{key}', limits[key]))
            for key in limits if key in mapping}


def _parse_footer(raw: Any, source: str) -> dict[str, str]:
    if raw is None:
        return {}
    mapping = _as_mapping(raw, source, 'footer')
    for key in sorted(mapping):
        if key != 'thanks':
            raise _fail(source, f'footer.{key}', 'unknown key (known: thanks)')
    return ({'thanks': _reject_verdict_text(source, 'footer.thanks',
                                            _as_text(mapping['thanks'], source, 'footer.thanks',
                                                     MAX_THANKS))}
            if 'thanks' in mapping else {})


# ── the config object ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class ReceiptConfig:
    """A validated, immutable printer configuration."""

    kit: str | None = None
    theme: str | None = None
    fields: tuple[str, ...] = ()
    paper: str = DEFAULT_PAPER
    columns: int = PAPERS[DEFAULT_PAPER]['columns']
    sections: Mapping[str, bool] = dataclass_field(default_factory=dict)
    order: tuple[str, ...] = ()
    header: Mapping[str, str] = dataclass_field(default_factory=dict)
    footer: Mapping[str, str] = dataclass_field(default_factory=dict)
    extra_fields: tuple[CustomField, ...] = ()
    logo: str | None = None
    barcode: str = 'none'
    barcode_url: str | None = None
    printer: Mapping[str, Any] = dataclass_field(default_factory=dict)
    triggers: TriggerConfig = DEFAULT_TRIGGER_CONFIG
    include: IncludeConfig = DEFAULT_INCLUDE
    source: str | None = None

    # -- geometry ----------------------------------------------------------
    @property
    def margins(self) -> dict[str, int]:
        preset = PAPERS.get(self.paper, PAPERS['custom'])
        margin = {'left': preset['left'], 'right': preset['right']}
        return margin

    def printed_order(self, present: Iterable[str]) -> tuple[str, ...]:
        """The section order to print, restricted to the sections that exist."""
        available = set(present)
        order = self.order or tuple(name for name in SECTIONS if self.enabled(name))
        return tuple(name for name in order if name in available)

    def enabled(self, section: str, default: bool = True) -> bool:
        """Whether *section* prints: a config wins over the kit.

        *default* is the kit's own answer for a section the file does not mention.
        ``verdict`` and ``criteria`` are mandatory and always return ``True``.
        """
        canonical = _canonical_section(section)
        if canonical in MANDATORY_SECTIONS:
            return True
        return bool(self.sections.get(canonical, default))

    # -- overrides ---------------------------------------------------------
    def with_paper(self, paper: str | None = None, columns: int | None = None) -> 'ReceiptConfig':
        """A copy forced onto another paper preset or column count.

        A CLI ``--paper``/``--columns`` wins over the file. With no override the
        original is returned unchanged, so precedence is explicit > file > default.
        """
        if not paper and columns is None:
            return self
        preset, count = _parse_paper(paper, columns, 'command line')
        return dataclasses.replace(self, paper=preset, columns=count)

    def with_style(self, *, kit: str | None = None, theme: str | None = None,
                   fields: Sequence[str] | None = None) -> 'ReceiptConfig':
        return dataclasses.replace(self, kit=kit or self.kit, theme=theme or self.theme,
                                   fields=tuple(fields) if fields else self.fields)

    def barcode_payload(self, public: Mapping) -> str | None:
        """The text the barcode/QR encodes, or ``None`` when it prints nothing."""
        if self.barcode in ('none', None):
            return None
        digest = str(public.get('receipt_hash') or '')
        if not digest:
            return None
        if self.barcode == 'hash':
            return digest[:19]  # 'sha256-' + 12 hex, the same prefix the receipt prints
        if self.barcode == 'url':
            template = self.barcode_url or ''
            target = template.replace('{receipt_hash}', digest)
            return target.replace('{hash_prefix}', digest[:19])
        return None  # qr is drawn by v2.branding.qr_render


def parse_config(raw: Mapping, source: str = CONFIG_FILENAME,
                 base_dir: Path | None = None) -> ReceiptConfig:
    """Validate a config mapping; every failure names the offending key.

    ``base_dir`` is the directory of the config file, used to resolve a relative
    ``logo`` path next to the file.
    """
    mapping = _as_mapping(raw, source, CONFIG_FILENAME)
    known = {'schema_version', 'kit', 'theme', 'fields', 'paper', 'columns', 'sections', 'order',
             'header', 'footer', 'extra_fields', 'logo', 'barcode', 'barcode_url', 'printer',
             'triggers', 'include'}
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise _fail(source, unknown[0], f'unknown key (known: {", ".join(sorted(known))})')
    version = mapping.get('schema_version', SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise _fail(source, 'schema_version', f'must be {SCHEMA_VERSION}')
    kit = None
    if mapping.get('kit') is not None:
        kit = _as_text(mapping['kit'], source, 'kit', 64)
        if kit not in kit_names():
            raise _fail(source, 'kit', f'unknown kit {kit!r} (available: {", ".join(kit_names())})')
    theme = None
    if mapping.get('theme') is not None:
        theme = _as_text(mapping['theme'], source, 'theme', 64)
        try:
            available = theme_names()
        except ThemeError as exc:  # pragma: no cover - defensive
            raise _fail(source, 'theme', str(exc)) from exc
        if theme not in available:
            raise _fail(source, 'theme', f'unknown theme {theme!r} (available: {", ".join(available)})')
    fields: tuple[str, ...] = ()
    if mapping.get('fields') is not None:
        try:
            fields = tuple(validate_fields(_as_str_list(mapping['fields'], source, 'fields')))
        except KitError as exc:
            raise _fail(source, 'fields', str(exc)) from exc
    sections = _parse_sections(mapping.get('sections'), source)
    order = _parse_order(mapping.get('order'), source, sections)
    paper, columns = _parse_paper(mapping.get('paper'), mapping.get('columns'), source)
    extra_raw = mapping.get('extra_fields') or []
    if not isinstance(extra_raw, (list, tuple)):
        raise _fail(source, 'extra_fields', 'must be a list of fields')
    extra_fields = tuple(_parse_custom_field(item, source, index)
                         for index, item in enumerate(extra_raw))
    keys = [item.key for item in extra_fields]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise _fail(source, 'extra_fields', f'duplicate field key(s): {", ".join(duplicates)}')
    logo, barcode, barcode_url = _parse_branding(mapping, source, base_dir)
    # One config file, one error type: a bad `triggers:`/`include:` key is reported as
    # a ConfigError naming the same key, so `hermes receipt config --show` and the
    # loader tell the user the same thing.
    try:
        triggers = parse_triggers(mapping.get('triggers'), source)
        include = parse_include(mapping.get('include'), source)
    except (TriggerError, IncludeError) as exc:
        raise ConfigError(str(exc)) from exc
    return ReceiptConfig(
        kit=kit, theme=theme, fields=fields, paper=paper, columns=int(columns),
        sections=sections, order=order, header=_parse_header(mapping.get('header'), source),
        footer=_parse_footer(mapping.get('footer'), source), extra_fields=extra_fields,
        logo=logo, barcode=barcode, barcode_url=barcode_url,
        printer=_parse_printer(mapping.get('printer'), source), triggers=triggers,
        include=include, source=source)


def config_file() -> Path:
    """The file :func:`load_config` reads by default (yaml preferred, json beside it)."""
    directory = config_dir()
    yaml_path = directory / CONFIG_FILENAME
    return yaml_path if yaml_path.is_file() else directory / CONFIG_JSON_FILENAME


def _read_mapping(path: Path) -> Mapping:
    text = path.read_text(encoding='utf-8')
    if path.suffix == '.json':
        import json
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ConfigError(f'{path.name}: invalid JSON: {exc}') from exc
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML ships with Hermes
        raise ConfigError(f'{path.name}: reading YAML needs PyYAML, or use '
                          f'{CONFIG_JSON_FILENAME}') from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f'{path.name}: invalid YAML: {getattr(exc, "problem", exc)}') from exc


def load_config(path: str | Path | None = None, *, paper: str | None = None,
                columns: int | None = None, use_default: bool = True) -> ReceiptConfig | None:
    """Load and validate a config file, or ``None`` when the user has none.

    ``path``    explicit file; ``None`` means "the file in the config dir, if any".
    ``use_default``  when false, a missing default file is not an error either.
    ``paper``/``columns``  CLI overrides that win over the file.
    """
    if path is not None:
        target = Path(path).expanduser()
        if not target.is_file():
            raise ConfigError(f'{target.name}: config file not found')
    else:
        if not use_default:
            target = None
        else:
            candidate = config_file()
            target = candidate if candidate.is_file() else None
    if target is None:
        if paper is None and columns is None:
            return None
        return parse_config({}, 'command line').with_paper(paper, columns)
    raw = _read_mapping(target)
    if raw is None:
        raw = {}
    config = parse_config(raw, target.name, target.parent)
    return config.with_paper(paper, columns)


# ── the documented example, written by `hermes receipt config --init` ──────
EXAMPLE_CONFIG = '''\
# Hermes receipts - printer configuration.
# Every key is optional: delete a line to fall back to the built-in default.
# This file is validated on load and any bad key is reported by name.

schema_version: 2

# -- content ---------------------------------------------------------------
# kit: full            # minimal | verify | cost | ops | full | auto | your kit
# theme: thermal-classic  # any theme in the config dir, + the five built-ins
# fields: [verdict, model, provider, tokens_total, cost, duration]

# -- paper -----------------------------------------------------------------
# 58mm (32 columns) | 80mm (48 columns) | a4 | letter | discord | roll
# paper: 80mm
# columns: 40          # custom column count: set "paper: custom" to use one

# -- which sections print, and in what order -------------------------------
# verdict and criteria are mandatory: this file cannot switch them off.
# sections:
#   header: true
#   claims: true         # DECLARED CLAIMS counts
#   evidence: true       # EVIDENCE CHECKED lines
#   fine_print: true     # the field block
#   footer: true         # version / window lines; the disclaimer always prints
# order: [header, verdict, claims, criteria, evidence, fine_print, footer]

# -- your shop -------------------------------------------------------------
# header:
#   shop_name: "MURRAY & SONS AUTOMATION"
#   tagline: "agent work, itemised"
# footer:
#   thanks: "THANK YOU FOR YOUR PATRONAGE"

# -- your own fine-print fields -------------------------------------------
# A template reads declared session data; missing data prints n/a.
# A "value" is static text. Exactly one of template/value per field.
# extra_fields:
#   - key: tokens
#     label: TOKENS
#     template: "{tokens_total:,.0f} tok - ${cost_usd:.4f}"
#   - key: terms
#     label: TERMS
#     value: "No refunds on agent promises."

# -- branding --------------------------------------------------------------
# logo: ~/.hermes/receipts/logo.png     # dithered to 1-bit for thermal paper
# barcode: hash                         # none | hash | url | qr (qr needs qrcode/segno)
# barcode_url: "https://example.invalid/r/{hash_prefix}"

# -- USB printer -----------------------------------------------------------
# printer:
#   cut: true
#   feed_lines: 3
#   density: 8        # 0..15, ESC/POS DC2 #
#   copies: 1
''' + '\n' + EXAMPLE_TRIGGERS + '\n' + EXAMPLE_INCLUDE


def init_config(path: str | Path | None = None, *, force: bool = False) -> Path:
    """Write the commented example config; refuse to overwrite one that exists."""
    target = Path(path).expanduser() if path else config_dir() / CONFIG_FILENAME
    if target.exists() and not force:
        raise ConfigError(f'{target.name} already exists; pass --force to overwrite')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG, encoding='utf-8')
    return target
