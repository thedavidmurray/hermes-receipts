"""Public-only receipt renderer: one text, one layout, the user's paper and theme.

Content and aesthetics are separate:

* :func:`receipt_lines` builds a list of :class:`Line`. A verdict line carries the
  structured verdict enum, never a substring of its own text, so colour comes from
  the enum and always prints a glyph *and* the word (`SUPPORTED \u2713`).
* :func:`_draw` turns those lines into pixels. A theme chooses paper, ink, glyph
  set, font, typography and paper furniture; it can never change the verdict
  mapping, hide a line or make a non-supported verdict look supported. In a
  monochrome theme a contradicted verdict is printed as inverse video.
* A :class:`v2.config.ReceiptConfig` chooses the *paper*: a column count (58 mm
  presets and custom alike) that wrapping, truncation, the ASCII printer and the
  PNG geometry all follow, the section order, the shop's header/footer text, the
  user's own fine-print fields, a logo and a verification barcode.

The rendered representative is identical for the text printer and the PNG
(:func:`public_text` is the same line list the PNG draws), so a USB print and a
screenshot cannot disagree — including the barcode's payload line. Paper
furniture (perforation, logo, dots) exists only in pixels, exactly as before. No
network, no remote fonts, no private metadata.
"""
from __future__ import annotations

import dataclasses
import textwrap
from dataclasses import dataclass, field as dataclass_field
from io import BytesIO
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import branding
from .config import SECTIONS, CustomField
from .fields import (DEFAULT_KIT, MIN_VERDICT_CONTRAST, bar_color, contrast_ratio, luminance,
                     field_block, field_view, kit_experimental_kinds, kit_sections,
                     load_theme, validate_sections, verdict_glyph, verdict_ink, verdict_word,
                     worst_verdict)

#: Legacy geometry: image width is ``WIDTH + 72`` (652 px) as v1 established.
WIDTH = 580
PAPER = '#f4efdf'
INK = '#222620'
GREEN = '#127847'
RED = '#a73532'
MUTED = '#666557'
#: Characters per paper line, for the PNG and the printer alike.
TEXT_WIDTH = 45
LINE_HEIGHT = 29
TOP = 42
LEFT = 58
#: Verdicts in the order their statement counts are printed.
COUNT_ORDER = ('supported', 'contradicted', 'unverified', 'not_evaluable')
#: 9-pin dot pitch and dot size for the ``dot`` font: dotted, still legible.
DOT_PITCH = 4
DOT_SIZE = 3
#: Widest rule that prints; a narrower paper shortens it (never overflows).
RULE_WIDTH = 38
#: Default section order of a task receipt, exactly as v1 printed it.
DEFAULT_ORDER: tuple[str, ...] = SECTIONS
#: Report receipts have no claims/criteria/evidence blocks of their own.
REPORT_SECTIONS: tuple[str, ...] = ('header', 'verdict', 'fine_print', 'footer')
#: Vertical room a logo may take, in pixels, and the gap under it.
LOGO_MAX_HEIGHT = 96
LOGO_GAP = 10


@dataclass(frozen=True)
class Line:
    """One rendered line. ``verdict`` (not ``text``) decides its colour."""

    text: str
    kind: str = 'plain'          # title | head | rule | plain | muted | note | field | brand | barcode
    verdict: str | None = None
    indent: int = 0
    payload: str | None = None   # barcode lines carry the text the bars encode
    count: int | None = None     # None = claim/verdict; 0 must never imply an inverse failure

    def __post_init__(self) -> None:
        if self.kind == 'verdict' and self.verdict is None:
            raise ValueError('a verdict line must carry its structured verdict')
        if self.kind == 'barcode' and not self.payload:
            raise ValueError('a barcode line must carry its payload')


#: 7-bit substitutions so an ASCII theme really is 7-bit (terminals, code blocks, plain printers).
_ASCII_FOLD = {'\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
               '\u2013': '-', '\u2014': '-', '\u00a0': ' ', '\u2026': '...'}


def fold_ascii(text: str) -> str:
    """Transliterate to 7-bit ASCII, replacing anything else with ``?``."""
    folded = ''.join(_ASCII_FOLD.get(char, char) for char in text)
    return folded.encode('ascii', 'replace').decode('ascii')


def wrap(text: str, width: int = TEXT_WIDTH, indent: int = 0) -> list[str]:
    """Wrap a line to paper width, hanging-indenting continuations."""
    if not text:
        return ['']
    body = textwrap.wrap(text, width=max(1, width - indent), break_long_words=True,
                         break_on_hyphens=False) or ['']
    if indent:
        return [body[0], *[' ' * indent + part for part in body[1:]]]
    return body


def _rule(width: int = RULE_WIDTH) -> Line:
    return Line('-' * width, kind='rule')


def _verdict_line(prefix: str, verdict: str, glyph_set: str, suffix: str = '') -> Line:
    text = f'{prefix}{verdict_word(verdict)} {verdict_glyph(verdict, glyph_set)}{suffix}'
    return Line(text, kind='verdict', verdict=verdict)


def _header(public: Mapping, theme: Mapping, config=None, rule_width: int = RULE_WIDTH,
            width: int = TEXT_WIDTH) -> list[Line]:
    custom = getattr(config, 'header', None) or {}
    brand = custom.get('shop_name') or theme.get('brand_text') or 'HERMES / RECEIPTS'
    if public.get('kind') == 'report':
        title, subtitle = public.get('title') or 'Evidence-backed claim statement', 'NOT A LIE RATE'
    else:
        title, subtitle = public.get('title') or 'Receipt', 'GET THE RECEIPT'
    if custom.get('tagline'):
        subtitle = custom['tagline']
    # The header respects the paper width like every other section: a long shop name
    # wraps rather than running off the roll.
    lines = [Line(part, kind='brand') for part in wrap(str(brand), width)]
    lines += [Line(part, kind='title') for part in wrap(str(title), width)]
    lines += [Line(part, kind='head') for part in wrap(str(subtitle), width)]
    return [*lines, _rule(rule_width)]


def _verdict_section(public: Mapping, theme: Mapping) -> list[Line]:
    """The completion line. Immutable: no kit, config or theme can remove or restyle it."""
    glyph_set = theme['glyph_set']
    criteria = [c for c in public.get('criteria') or [] if isinstance(c, Mapping)]
    required = [c for c in criteria if c.get('required') is True]
    # The completion claim can only be downgraded: the worst of its own verdict and
    # every required criterion. Green therefore means *all* required criteria pass.
    display = worst_verdict([public.get('completion'), *[c.get('verdict') for c in required]])
    return [_verdict_line('COMPLETION: ', display, glyph_set)]


def _counts_section(public: Mapping, theme: Mapping) -> list[Line]:
    """DECLARED CLAIMS statement counts, one line per verdict category."""
    glyph_set = theme['glyph_set']
    criteria = [c for c in public.get('criteria') or [] if isinstance(c, Mapping)]
    if not criteria:
        return []
    lines = [Line('DECLARED CLAIMS', kind='head')]
    for verdict in COUNT_ORDER:
        count = sum(1 for c in criteria if c.get('verdict') == verdict)
        # A statement count is coloured per category, never by its text.
        lines.append(Line(f'{verdict_word(verdict)} {verdict_glyph(verdict, glyph_set)}: {count}',
                          kind='verdict', verdict=verdict, count=count))
    return lines


def _criteria_section(public: Mapping, theme: Mapping, kit_name: str,
                      width: int = TEXT_WIDTH, rule_width: int = RULE_WIDTH) -> list[Line]:
    glyph_set = theme['glyph_set']
    criteria = [c for c in public.get('criteria') or [] if isinstance(c, Mapping)]
    experimental = set(kit_experimental_kinds(kit_name))
    lines = [_rule(rule_width), Line('REQUIRED CRITERIA', kind='head')]
    if not criteria:
        return [*lines, Line('  none declared', kind='muted')]
    for index, criterion in enumerate(criteria, 1):
        marker = 'REQ' if criterion.get('required') is True else 'OPT'
        label = str(criterion.get('label') or f'Criterion {index}')
        suffix = ' (experimental)' if criterion.get('kind') in experimental else ''
        for part in wrap(f'{marker} {index}: {label}{suffix}', width):
            lines.append(Line(part, kind='field'))
        verdict = criterion.get('verdict')
        lines.append(_verdict_line('  ', verdict if isinstance(verdict, str) else 'not_evaluable',
                                   glyph_set))
    return lines


def _evidence_section(public: Mapping, theme: Mapping,
                      width: int = TEXT_WIDTH, rule_width: int = RULE_WIDTH) -> list[Line]:
    criteria = [c for c in public.get('criteria') or [] if isinstance(c, Mapping)]
    lines: list[Line] = [_rule(rule_width), Line('EVIDENCE CHECKED', kind='head')]
    if not criteria:
        return [*lines, Line('  no claim was declared', kind='muted')]
    for index, criterion in enumerate(criteria, 1):
        for part in wrap(f'{index}. checked: {criterion.get("check") or "n/a"}', width, indent=3):
            lines.append(Line(part, kind='note'))
        if criterion.get('verdict') != 'supported':
            for part in wrap(f'{index}. not supported: {criterion.get("reason") or "n/a"}',
                             width, indent=3):
                lines.append(Line(part, kind='note'))
    return lines


def _fine_print(public: Mapping, theme: Mapping, kit_name: str, config=None,
                width: int = TEXT_WIDTH, rule_width: int = RULE_WIDTH) -> list[Line]:
    """The field block: the kit's fields plus the user's own fine-print fields."""
    block = field_block(public, theme['glyph_set'], kit_name)
    extra: Sequence[CustomField] = tuple(getattr(config, 'extra_fields', ()) or ())
    if not block and not extra:
        return []
    view = field_view(public, theme['glyph_set'])
    lines = [_rule(rule_width), Line('FINE PRINT', kind='head')]
    for label, value in block:
        for part in wrap(f'{label}: {value}', width, indent=2):
            lines.append(Line(part, kind='field'))
    for field in extra:
        # A custom field whose data is missing prints n/a, never a partial guess.
        for part in wrap(f'{field.label}: {field.render(view)}', width, indent=2):
            lines.append(Line(part, kind='field'))
    return lines


def _report_lines(public: Mapping, sections: Sequence[str], theme: Mapping) -> list[Line]:
    glyph_set = theme['glyph_set']
    dimensions = public.get('dimensions') or []
    lines = [
        Line('WINDOW UTC: ' + str(public.get('window')).split(' (UTC, end exclusive)')[0], kind='field'),
        Line('Dates only; exact UTC bounds are private.', kind='muted'),
        Line('Actual observation window: [start, end).', kind='muted'),
        Line(f"COVERAGE: {int(public.get('complete_sources') or 0)}/{int(public.get('source_count') or 0)} complete", kind='field'),
        Line(f"PARTIAL/UNAVAILABLE: {int(public.get('partial_sources') or 0)}", kind='field'),
        Line('Missing evidence is unverified.', kind='muted'), _rule(),
    ]
    for index, group in enumerate(public.get('groups') or [], 1):
        lines.append(Line(f"GROUP {index} ({', '.join(dimensions)})", kind='head'))
        for label, key in [('TASK COMPLETION', 'done'), ('ACTION CLAIMS', 'action'),
                           ('DONE ASSERTIONS', 'done_claims')]:
            counts = group.get(key) or {}
            supported = int(counts.get('supported', 0) or 0)
            # A statement count line is coloured per category, never by its text.
            classified = sum(int(counts.get(name, 0) or 0) for name in COUNT_ORDER)
            for verdict in COUNT_ORDER:
                count = int(counts.get(verdict, 0) or 0)
                lines.append(Line(f'{label}: {verdict_word(verdict)} '
                                  f'{verdict_glyph(verdict, glyph_set)}  ({count})',
                                  kind='verdict', verdict=verdict, count=count))
            lines.append(Line(f'  rate: {supported / classified:.1%}' if classified >= 10 else
                              '  rate: counts only (n<10)' if classified else '  rate: N/A',
                              kind='field'))
        lines.append(Line(f"  unknown model/source: {int(group.get('unknown_model_claims') or 0)}/"
                          f"{int(group.get('unknown_source_claims') or 0)}", kind='field'))
        lines.append(_rule())
    return lines


def _barcode_line(public: Mapping, config) -> list[Line]:
    """The verification line, when the config asks for one and the receipt has a hash."""
    if config is None or config.barcode in (None, 'none'):
        return []
    payload = config.barcode_payload(public)
    if not payload:
        return []
    return [Line(f'VERIFY: {payload}', kind='barcode', payload=payload)]


def _footer(public: Mapping, theme: Mapping, config=None,
            width: int = TEXT_WIDTH, rule_width: int = RULE_WIDTH) -> list[Line]:
    """Version/window/thanks furniture, then the disclaimer that always prints."""
    furniture = config is None or config.enabled('footer')
    lines = [_rule(rule_width)]
    if furniture:
        version = public.get('artifact_version')
        if version:
            lines.append(Line(f'VERSION: {version}', kind='field'))
        window = public.get('window')
        if window:
            lines.append(Line('WINDOW UTC: ' + str(window).split(' (UTC, end exclusive)')[0],
                              kind='field'))
        thanks = (getattr(config, 'footer', None) or {}).get('thanks')
        if thanks:
            for part in wrap(str(thanks), width):
                lines.append(Line(part, kind='brand'))
        lines += _barcode_line(public, config)
    # Not configurable: the receipt always states what it does *not* prove.
    for part in wrap(str(public.get('qualification') or ''), width):
        lines.append(Line(part, kind='plain'))
    for part in wrap(str(public.get('warning') or ''), width):
        lines.append(Line(part, kind='muted'))
    return lines


def section_plan(kind: str, kit_name: str, sections: Iterable[str] | str | None, config=None):
    """Resolve which sections print and in what order.

    Precedence is explicit argument > ``receipts.yaml`` > kit. ``verdict`` and
    ``criteria`` are always enabled, and the footer always prints its disclaimer.
    """
    chosen = (set(validate_sections(sections)) if sections is not None
              else set(kit_sections(kit_name)))
    enabled = {'header': True, 'verdict': True, 'claims': 'counts' in chosen, 'criteria': True,
               'evidence': 'evidence' in chosen, 'fine_print': True, 'footer': True}
    if config is not None:
        enabled = {name: config.enabled(name, enabled[name]) for name in SECTIONS}
    present = DEFAULT_ORDER if kind == 'task' else REPORT_SECTIONS
    enabled = {name: enabled[name] for name in present}
    enabled['verdict'] = enabled['criteria'] = enabled['footer'] = True
    order = config.printed_order(present) if config is not None else present
    return enabled, order


def receipt_lines(public: Mapping, *, kit: str | None = None, theme: str | None = None,
                  sections: Iterable[str] | str | None = None, config=None) -> list[Line]:
    """Build the ordered lines for a public receipt under a kit, a theme and a config.

    Raises ``KitError`` for an unknown kit or field, ``ThemeError`` for a theme that
    breaks the verdict-visibility invariant and ``ConfigError`` for a config that
    breaks the schema.
    """
    kind = public.get('kind')
    if kind not in ('task', 'report'):
        raise ValueError('task or report public receipt required')
    kit_name = (kit or getattr(config, 'kit', None) or public.get('kit') or DEFAULT_KIT)
    if theme is not None:
        theme_name = theme
    else:
        theme_name = getattr(config, 'theme', None) or public.get('theme')
    theme_obj = load_theme(theme_name)
    width = int(getattr(config, 'columns', TEXT_WIDTH) or TEXT_WIDTH)
    rule_width = min(RULE_WIDTH, width)
    enabled, order = section_plan(kind, kit_name, sections, config)
    blocks: dict[str, list[Line]] = {}
    if enabled['header']:
        blocks['header'] = _header(public, theme_obj, config, rule_width, width)
    if kind == 'task':
        if enabled['verdict']:
            blocks['verdict'] = _verdict_section(public, theme_obj)
        if enabled['claims']:
            blocks['claims'] = _counts_section(public, theme_obj)
        if enabled['criteria']:
            blocks['criteria'] = _criteria_section(public, theme_obj, kit_name, width, rule_width)
        if enabled['evidence']:
            blocks['evidence'] = _evidence_section(public, theme_obj, width, rule_width)
    else:
        blocks['verdict'] = _report_lines(public, chosen_sections(sections), theme_obj)
    if enabled['fine_print']:
        blocks['fine_print'] = _fine_print(public, theme_obj, kit_name, config, width, rule_width)
    blocks['footer'] = _footer(public, theme_obj, config, width, rule_width)
    lines: list[Line] = []
    for name in order:
        lines += blocks.get(name, [])
    lines = _fit(lines, width)
    if len(lines) > 550:
        raise ValueError('receipt too tall; narrow the report')
    if theme_obj['glyph_set'] == 'ascii':
        lines = [Line(fold_ascii(line.text), kind=line.kind, verdict=line.verdict,
                      indent=line.indent,
                      payload=fold_ascii(line.payload) if line.payload else None, count=line.count)
                 for line in lines]
    return lines


def _fit(lines: Sequence[Line], width: int) -> list[Line]:
    """Wrap any line wider than the paper, so no section can run off the roll.

    Rules stay full-width and barcode lines are drawn from their payload, so both pass
    through untouched; every other kind is wrapped and keeps its kind and verdict.
    """
    fitted: list[Line] = []
    for line in lines:
        if line.kind in ('rule', 'barcode') or len(line.text) <= width:
            fitted.append(line)
            continue
        fitted += [dataclasses.replace(line, text=part) for part in wrap(line.text, width, line.indent)]
    return fitted


def chosen_sections(sections: Iterable[str] | str | None) -> tuple[str, ...]:
    """Validated optional sections, for the parts of a report that still use the vocabulary."""
    return () if sections is None else validate_sections(sections)


def public_text(public: Mapping, *, kit: str | None = None, theme: str | None = None,
                sections: Iterable[str] | str | None = None, config=None) -> str:
    """Identical public representation for the printer and the PNG."""
    return '\n'.join(line.text for line in receipt_lines(public, kit=kit, theme=theme,
                                                         sections=sections, config=config))


# ── pixels ─────────────────────────────────────────────────────────────────
def _font(size: int, bold: bool = False):
    candidates = ('/System/Library/Fonts/Menlo.ttc',
                  '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf')
    for name in candidates:
        if Path(name).is_file():
            return ImageFont.truetype(name, size=size, index=1 if bold and name.endswith('.ttc') else 0)
    return ImageFont.load_default(size=size)


def _dot_grid(size: tuple[int, int]):
    """A tiled dot mask: ink only where the 9-pin head would have struck."""
    pitch = DOT_PITCH * 4
    tile = Image.new('L', (pitch, pitch), 0)
    dot = ImageDraw.Draw(tile)
    for y in range(0, pitch, DOT_PITCH):
        for x in range(0, pitch, DOT_PITCH):
            dot.rectangle((x, y, x + DOT_SIZE - 1, y + DOT_SIZE - 1), fill=255)
    grid = Image.new('L', size, 0)
    for y in range(0, size[1], pitch):
        for x in range(0, size[0], pitch):
            grid.paste(tile, (x, y))
    return grid


def _paper_furniture(draw: ImageDraw.ImageDraw, theme: Mapping, width: int, height: int,
                     line_height: int = LINE_HEIGHT) -> None:
    edge = theme['perforation']
    if edge == 'torn':
        for x in range(36, width - 36, 20):
            draw.polygon(((x, height), (x + 10, height + 13), (x + 20, height)), fill=theme['paper'])
    elif edge == 'perforated':
        # A punched tear line: small holes along the bottom edge and both side margins.
        for x in range(30, width - 24, 16):
            draw.ellipse((x, height - 2, x + 4, height + 2), fill=theme['frame'])
        for y in range(28, height - 12, 18):
            for x in (40, width - 44):
                draw.ellipse((x, y, x + 3, y + 3), fill=theme['frame'])
    elif edge == 'tractor':
        for y in range(24, height, line_height):
            for x in (18, width - 54):
                draw.ellipse((x, y, x + 8, y + 8), fill=theme['frame'], outline=theme['ink_muted'])


def _line_color(line: Line, theme: Mapping) -> str:
    if line.kind == 'verdict':
        return verdict_ink(theme, str(line.verdict))
    if line.kind in ('muted', 'note', 'rule'):
        return theme['ink_muted']
    return theme['ink']


def geometry(theme: Mapping, config=None) -> dict:
    """Pixel geometry: the legacy constants without a config, the paper's with one."""
    if config is None:
        return {'columns': TEXT_WIDTH, 'frame': 36, 'paper_left': 36, 'paper_right': WIDTH + 35,
                'left': LEFT, 'top': TOP, 'line_height': LINE_HEIGHT, 'image_width': WIDTH + 72,
                'text_px': WIDTH + 35 - LEFT}
    columns = int(config.columns)
    margins = config.margins
    normal = _font(int(theme['font_size']))
    text_px = max(40, int(round(normal.getlength('M') * columns)))
    left = 36 + int(margins['left'])
    paper_right = left + text_px + int(margins['right'])
    return {
        'columns': columns, 'frame': 36, 'paper_left': 36, 'paper_right': paper_right,
        'left': left, 'top': TOP, 'text_px': text_px,
        'line_height': max(12, int(round(theme['font_size'] * 1.61 * theme['line_spacing']))),
        'image_width': paper_right + 38,
    }


def barcode_bar_height(text_px: int, line_height: int) -> int:
    """Bar height for a barcode over *text_px* of paper: a quarter of the width, bounded."""
    return max(24, min(line_height * 4, text_px // 4))


def _barcode_pixels(draw: ImageDraw.ImageDraw, line: Line, theme: Mapping, x: int, y: int,
                    text_px: int, line_height: int) -> tuple[int, bool]:
    """Draw the bars for a barcode line; return (height used, whether bars were drawn)."""
    payload = line.payload or ''
    try:
        fit = branding.barcode_geometry(payload, text_px)
    except ValueError:  # pragma: no cover - payloads are validated when they are built
        return 0, False
    if not fit['fits']:
        # The barcode cannot be drawn legibly at this paper width; the line still prints.
        return 0, False
    module_px = int(fit['module_px'])
    height = barcode_bar_height(text_px, line_height)
    for start, run in fit['bars']:
        bar_x = x + start * module_px
        draw.rectangle((bar_x, y, bar_x + run * module_px - 1, y + height), fill=theme['ink'])
    return height, True


def _readable_bar(lines: Sequence[Line], theme: Mapping) -> str | None:
    """Paper furniture is optional: never band dark paper or obscure any text ink."""
    if not theme['green_bar'] or luminance(theme['paper']) <= 0.5:
        return None
    bar = bar_color(theme)
    # Barcodes can fall back to muted text after layout; include that ink too.
    inks = {theme['ink'], theme['ink_muted'], *(_line_color(line, theme) for line in lines)}
    if any(contrast_ratio(ink, bar) < MIN_VERDICT_CONTRAST for ink in inks):
        return None
    return bar


def _draw(lines: Sequence[Line], theme: Mapping, output: str | Path | BytesIO, config=None):
    shape = geometry(theme, config)
    line_height, columns = shape['line_height'], shape['columns']
    logo_mask = None
    logo_box = (0, 0)
    if config is not None and config.logo:
        logo_mask = branding.logo_mask(config.logo, shape['text_px'], LOGO_MAX_HEIGHT)
        logo_box = logo_mask.size
    text_height = 70 + len(lines) * line_height + (logo_box[1] + LOGO_GAP if logo_mask else 0)
    # Room for the barcode: bars plus the payload text under them.
    barcode_lines = [line for line in lines if line.kind == 'barcode']
    text_height += len(barcode_lines) * (barcode_bar_height(shape['text_px'], line_height)
                                        + line_height + 8)
    height = max(520, text_height)
    width = shape['image_width']
    canvas = Image.new('RGB', (width, height + 40), theme['frame'])
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((shape['paper_left'], 14, shape['paper_right'], height), fill=theme['paper'])
    bar = _readable_bar(lines, theme)
    if bar is not None:
        for index in range(len(lines)):
            if index % 2:
                top = shape['top'] - 8 + index * line_height
                draw.rectangle((shape['paper_left'], top, shape['paper_right'], top + line_height - 2),
                               fill=bar)
    _paper_furniture(draw, theme, width, height, line_height)
    size = int(theme['font_size'])
    normal, bold = _font(size), _font(size + 1, True)
    title = _font(max(size, int(round(size * 1.722))), True)
    head_font = _font(size + 1, True) if theme['font'] == 'dot' else _font(size + 3, True)
    dot_grid = _dot_grid((width, line_height)) if theme['font'] == 'dot' else None
    top = shape['top']
    if logo_mask is not None:
        # 1-bit ink, exactly the dots the logo will print.
        canvas.paste(Image.new('RGB', logo_mask.size, theme['ink']),
                     (shape['left'], top), logo_mask)
        top += logo_mask.size[1] + LOGO_GAP
    for index, line in enumerate(lines):
        if line.kind == 'barcode':
            bars, drawn = _barcode_pixels(draw, line, theme, shape['left'], top, shape['text_px'],
                                          line_height)
            if not drawn:
                # Never a silent gap: say why, in the same line the printer prints.
                draw.text((shape['left'], top), f'{line.text} (no room for bars)'[:columns],
                          font=normal, fill=theme['ink_muted'])
                top += line_height
                continue
            top += bars + 4
            draw.text((shape['left'], top), line.text[:columns], font=normal,
                      fill=theme['ink'])
            top += line_height
            continue
        font = title if line.kind == 'brand' and theme['font'] == 'standard' else (
            head_font if index < 3 or line.kind in ('title', 'head') else
            (bold if line.kind == 'brand' else normal))
        color = _line_color(line, theme)
        inverse = (theme['mono'] and line.verdict == 'contradicted'
                   and (line.count is None or line.count > 0))
        if inverse:
            draw.rectangle((shape['left'] - 8, top - 4, shape['paper_right'], top + line_height - 6),
                           fill=color)
        ink = theme['paper'] if inverse else color
        text = line.text[:columns] if line.kind != 'rule' else line.text
        if theme['font'] == 'standard':
            draw.text((shape['left'], top), text, font=font, fill=ink)
            top += line_height
            continue
        mask = Image.new('L', (width, line_height), 0)
        ImageDraw.Draw(mask).text((shape['left'], 0), text, font=font, fill=255)
        mask = ImageChops.multiply(mask, dot_grid)
        canvas.paste(Image.new('RGB', (width, line_height), ink), (0, top), mask)
        top += line_height
    if not isinstance(output, BytesIO):
        output = Path(output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
    # Pillow does not add EXIF/text/iTXt by default. Never pass private metadata.
    canvas.save(output, format='PNG', optimize=True)
    return output


def render_task_png(public: Mapping, output: str | Path | BytesIO, *,
                    kit: str | None = None, theme: str | None = None,
                    sections: Iterable[str] | str | None = None, config=None):
    return _draw(receipt_lines(public, kit=kit, theme=theme, sections=sections, config=config),
                 load_theme(theme if theme is not None else
                            (getattr(config, 'theme', None) or public.get('theme'))),
                 output, config)


def render_report_png(public: Mapping, output: str | Path | BytesIO, *,
                      kit: str | None = None, theme: str | None = None,
                      sections: Iterable[str] | str | None = None, config=None):
    return _draw(receipt_lines(public, kit=kit, theme=theme, sections=sections, config=config),
                 load_theme(theme if theme is not None else
                            (getattr(config, 'theme', None) or public.get('theme'))),
                 output, config)
