"""Public-only receipt renderer: one text, one layout, five themes.

Content and aesthetics are separate:

* :func:`receipt_lines` builds a list of :class:`Line`. A verdict line carries the
  structured verdict enum, never a substring of its own text, so colour comes from
  the enum and always prints a glyph *and* the word (`SUPPORTED \u2713`).
* :func:`_draw` turns those lines into pixels. A theme chooses paper, ink, glyph
  set, font and paper furniture; it can never change the verdict mapping, hide a
  line or make a non-supported verdict look supported. In a monochrome theme a
  contradicted verdict is printed as inverse video.

The rendered representative is identical for the text printer and the PNG
(:func:`public_text` is the same line list the PNG draws), so a USB print and a
screenshot cannot disagree. No network, no remote fonts, no private metadata.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass, field as dataclass_field
from io import BytesIO
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .fields import (DEFAULT_KIT, field_block, kit_experimental_kinds, kit_sections,
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


@dataclass(frozen=True)
class Line:
    """One rendered line. ``verdict`` (not ``text``) decides its colour."""

    text: str
    kind: str = 'plain'          # title | head | rule | plain | muted | note | field | brand
    verdict: str | None = None
    indent: int = 0

    def __post_init__(self) -> None:
        if self.kind == 'verdict' and self.verdict is None:
            raise ValueError('a verdict line must carry its structured verdict')


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
    body = textwrap.wrap(text, width=width - indent, break_long_words=True,
                         break_on_hyphens=False) or ['']
    if indent:
        return [body[0], *[' ' * indent + part for part in body[1:]]]
    return body


def _rule(width: int = 38) -> Line:
    return Line('-' * width, kind='rule')


def _verdict_line(prefix: str, verdict: str, glyph_set: str, suffix: str = '') -> Line:
    text = f'{prefix}{verdict_word(verdict)} {verdict_glyph(verdict, glyph_set)}{suffix}'
    return Line(text, kind='verdict', verdict=verdict)


def _header(public: Mapping, theme: Mapping) -> list[Line]:
    brand = theme.get('brand_text') or 'HERMES / RECEIPTS'
    if public.get('kind') == 'report':
        title, subtitle = public.get('title') or 'Evidence-backed claim statement', 'NOT A LIE RATE'
    else:
        title, subtitle = public.get('title') or 'Receipt', 'GET THE RECEIPT'
    return [Line(brand, kind='brand'), Line(title, kind='title'), Line(subtitle, kind='head'), _rule()]


def _verdict_section(public: Mapping, sections: Sequence[str], theme: Mapping) -> list[Line]:
    glyph_set = theme['glyph_set']
    criteria = [c for c in public.get('criteria') or [] if isinstance(c, Mapping)]
    required = [c for c in criteria if c.get('required') is True]
    # The completion claim can only be downgraded: the worst of its own verdict and
    # every required criterion. Green therefore means *all* required criteria pass.
    display = worst_verdict([public.get('completion'), *[c.get('verdict') for c in required]])
    lines = [_verdict_line('COMPLETION: ', display, glyph_set)]
    if 'counts' in sections:
        if criteria:
            lines += [Line('DECLARED CLAIMS', kind='head')]
            for verdict in COUNT_ORDER:
                count = sum(1 for c in criteria if c.get('verdict') == verdict)
                # A statement count is coloured per category, never by its text.
                lines.append(Line(f'{verdict_word(verdict)} {verdict_glyph(verdict, glyph_set)}: {count}',
                                  kind='verdict', verdict=verdict))
    return lines


def _criteria_section(public: Mapping, theme: Mapping, kit_name: str) -> list[Line]:
    glyph_set = theme['glyph_set']
    criteria = [c for c in public.get('criteria') or [] if isinstance(c, Mapping)]
    experimental = set(kit_experimental_kinds(kit_name))
    lines = [_rule(), Line('REQUIRED CRITERIA', kind='head')]
    if not criteria:
        return [*lines, Line('  none declared', kind='muted')]
    for index, criterion in enumerate(criteria, 1):
        marker = 'REQ' if criterion.get('required') is True else 'OPT'
        label = str(criterion.get('label') or f'Criterion {index}')
        suffix = ' (experimental)' if criterion.get('kind') in experimental else ''
        for part in wrap(f'{marker} {index}: {label}{suffix}'):
            lines.append(Line(part, kind='field'))
        verdict = criterion.get('verdict')
        lines.append(_verdict_line('  ', verdict if isinstance(verdict, str) else 'not_evaluable',
                                   glyph_set))
    return lines


def _evidence_section(public: Mapping, theme: Mapping) -> list[Line]:
    criteria = [c for c in public.get('criteria') or [] if isinstance(c, Mapping)]
    lines: list[Line] = [_rule(), Line('EVIDENCE CHECKED', kind='head')]
    if not criteria:
        return [*lines, Line('  no claim was declared', kind='muted')]
    for index, criterion in enumerate(criteria, 1):
        for part in wrap(f'{index}. checked: {criterion.get("check") or "n/a"}', indent=3):
            lines.append(Line(part, kind='note'))
        if criterion.get('verdict') != 'supported':
            for part in wrap(f'{index}. not supported: {criterion.get("reason") or "n/a"}', indent=3):
                lines.append(Line(part, kind='note'))
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
                                  kind='verdict', verdict=verdict))
            lines.append(Line(f'  rate: {supported / classified:.1%}' if classified >= 10 else
                              '  rate: counts only (n<10)' if classified else '  rate: N/A',
                              kind='field'))
        lines.append(Line(f"  unknown model/source: {int(group.get('unknown_model_claims') or 0)}/"
                          f"{int(group.get('unknown_source_claims') or 0)}", kind='field'))
        lines.append(_rule())
    return lines


def _footer(public: Mapping, theme: Mapping) -> list[Line]:
    lines = [_rule()]
    version = public.get('artifact_version')
    if version:
        lines.append(Line(f'VERSION: {version}', kind='field'))
    window = public.get('window')
    if window:
        lines.append(Line('WINDOW UTC: ' + str(window).split(' (UTC, end exclusive)')[0], kind='field'))
    for part in wrap(str(public.get('qualification') or '')):
        lines.append(Line(part, kind='plain'))
    for part in wrap(str(public.get('warning') or '')):
        lines.append(Line(part, kind='muted'))
    return lines


def receipt_lines(public: Mapping, *, kit: str | None = None, theme: str | None = None,
                  sections: Iterable[str] | str | None = None) -> list[Line]:
    """Build the ordered lines for a public receipt under a kit and a theme.

    Raises ``KitError`` for an unknown kit or field and ``ThemeError`` for a theme
    that breaks the verdict-visibility invariant.
    """
    kind = public.get('kind')
    if kind not in ('task', 'report'):
        raise ValueError('task or report public receipt required')
    kit_name = kit or public.get('kit') or DEFAULT_KIT
    chosen = (validate_sections(sections) if sections is not None
              else kit_sections(kit_name))
    theme_obj = load_theme(theme if theme is not None else public.get('theme'))
    lines = _header(public, theme_obj)
    if kind == 'task':
        lines += _verdict_section(public, chosen, theme_obj)
        lines += _criteria_section(public, theme_obj, kit_name)
        if 'evidence' in chosen:
            lines += _evidence_section(public, theme_obj)
    else:
        lines += _report_lines(public, chosen, theme_obj)
    block = field_block(public, theme_obj['glyph_set'], kit_name)
    if block:
        lines += [_rule(), Line('FINE PRINT', kind='head')]
        for label, value in block:
            for part in wrap(f'{label}: {value}', indent=2):
                lines.append(Line(part, kind='field'))
    lines += _footer(public, theme_obj)
    if len(lines) > 550:
        raise ValueError('receipt too tall; narrow the report')
    if theme_obj['glyph_set'] == 'ascii':
        lines = [Line(fold_ascii(line.text), kind=line.kind, verdict=line.verdict,
                      indent=line.indent) for line in lines]
    return lines


def public_text(public: Mapping, *, kit: str | None = None, theme: str | None = None,
                sections: Iterable[str] | str | None = None) -> str:
    """Identical public representation for the printer and the PNG."""
    return '\n'.join(line.text for line in receipt_lines(public, kit=kit, theme=theme,
                                                         sections=sections))


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


def _paper_furniture(draw: ImageDraw.ImageDraw, theme: Mapping, width: int, height: int) -> None:
    if theme['perforation'] == 'torn':
        for x in range(36, width - 36, 20):
            draw.polygon(((x, height), (x + 10, height + 13), (x + 20, height)), fill=theme['paper'])
    elif theme['perforation'] == 'tractor':
        for y in range(24, height, LINE_HEIGHT):
            for x in (18, width - 54):
                draw.ellipse((x, y, x + 8, y + 8), fill=theme['frame'], outline=theme['ink_muted'])


def _line_color(line: Line, theme: Mapping) -> str:
    if line.kind == 'verdict':
        return verdict_ink(theme, str(line.verdict))
    if line.kind in ('muted', 'note', 'rule'):
        return theme['ink_muted']
    return theme['ink']


def _draw(lines: Sequence[Line], theme: Mapping, output: str | Path | BytesIO):
    width = WIDTH + 72
    height = max(520, 70 + len(lines) * LINE_HEIGHT)
    canvas = Image.new('RGB', (width, height + 40), theme['frame'])
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((36, 14, WIDTH + 35, height), fill=theme['paper'])
    if theme['green_bar']:
        for index in range(len(lines)):
            if index % 2:
                top = TOP - 8 + index * LINE_HEIGHT
                draw.rectangle((36, top, WIDTH + 35, top + LINE_HEIGHT - 2), fill='#e8f0e2')
    _paper_furniture(draw, theme, width, height)
    normal, bold, title = _font(18), _font(19, True), _font(31, True)
    head_font = _font(21, True) if theme['font'] == 'standard' else _font(19, True)
    dot_grid = _dot_grid((width, LINE_HEIGHT)) if theme['font'] == 'dot' else None
    for index, line in enumerate(lines):
        top = TOP + index * LINE_HEIGHT
        font = title if line.kind == 'brand' and theme['font'] == 'standard' else (
            head_font if index < 3 or line.kind in ('title', 'head') else
            (bold if line.kind == 'brand' else normal))
        color = _line_color(line, theme)
        inverse = theme['mono'] and line.verdict == 'contradicted'
        if inverse:
            draw.rectangle((50, top - 4, WIDTH + 35, top + LINE_HEIGHT - 6), fill=color)
        ink = theme['paper'] if inverse else color
        text = line.text[:TEXT_WIDTH] if line.kind != 'rule' else line.text
        if theme['font'] == 'standard':
            draw.text((LEFT, top), text, font=font, fill=ink)
            continue
        mask = Image.new('L', (width, LINE_HEIGHT), 0)
        ImageDraw.Draw(mask).text((LEFT, 0), text, font=font, fill=255)
        mask = ImageChops.multiply(mask, dot_grid)
        canvas.paste(Image.new('RGB', (width, LINE_HEIGHT), ink), (0, top), mask)
    if not isinstance(output, BytesIO):
        output = Path(output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
    # Pillow does not add EXIF/text/iTXt by default. Never pass private metadata.
    canvas.save(output, format='PNG', optimize=True)
    return output


def render_task_png(public: Mapping, output: str | Path | BytesIO, *,
                    kit: str | None = None, theme: str | None = None,
                    sections: Iterable[str] | str | None = None):
    return _draw(receipt_lines(public, kit=kit, theme=theme, sections=sections),
                 load_theme(theme if theme is not None else public.get('theme')), output)


def render_report_png(public: Mapping, output: str | Path | BytesIO, *,
                      kit: str | None = None, theme: str | None = None,
                      sections: Iterable[str] | str | None = None):
    return _draw(receipt_lines(public, kit=kit, theme=theme, sections=sections),
                 load_theme(theme if theme is not None else public.get('theme')), output)
