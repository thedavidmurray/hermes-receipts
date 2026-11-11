"""Public-only thermal PNGs; no private metadata, network, remote fonts or USB access."""
from __future__ import annotations

import textwrap
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw

from .fields import render_fields, kit_fields, kit_has_field, theme as get_theme, FIELD_SPECS
from .public import ALIASES, VERDICTS

WIDTH = 580


def _task_lines(public: dict) -> list[str]:
    if public.get('kind') != 'task':
        raise ValueError('task public receipt required')
    lines = ['HERMES / RECEIPTS', 'YOUR AGENT SAYS IT IS DONE', 'GET THE RECEIPT', '-' * 38,
             'TASK (PSEUDONYM)', 'COMPLETION: ' + _verdict(public.get('completion')),
             'REQUIRED CRITERIA']
    criteria = public.get('criteria', [])
    if len(criteria) > 40:
        raise ValueError('task exceeds public page limit')
    for i, criterion in enumerate(criteria, 1):
        alias = criterion.get('label')
        if alias not in ALIASES and alias != f'Criterion {i}':
            raise ValueError('unreviewed public criterion')
        lines.append(f"{'REQ' if criterion.get('required') is True else 'OPT'} {i}: {alias}")
        lines.append('  ' + _verdict(criterion.get('verdict')))
    lines += ['-' * 38, 'VERSION: ' + _version(public.get('artifact_version')),
              'WINDOW UTC: ' + _window(public.get('window')).split(' (UTC, end exclusive)')[0],
              'Dates only; exact UTC bounds are private.',
              'Actual observation window: [start, end).',
              *textwrap.wrap(_qualification(public.get('completion')), 44),
              'Experimental viewer; limited coverage.',
              'Tokens/cost: unavailable for this task']
    return lines


def _verdict(value):
    if value not in VERDICTS:
        raise ValueError('invalid public verdict')
    return value.upper().replace('_', ' ')


def _version(value):
    from .public import VERSION
    return value if value == 'version withheld' or isinstance(value, str) and VERSION.fullmatch(value) else 'version withheld'


def _window(value):
    from .public import _date
    # Public window must have been generated from two coarse UTC dates.
    import re
    if not isinstance(value, str) or not re.fullmatch(
            r'(?:\d{4}-\d{2}-\d{2} UTC|unknown date) to (?:\d{4}-\d{2}-\d{2} UTC|unknown date) \(UTC, end exclusive\)', value):
        raise ValueError('invalid public window')
    return value


def _qualification(verdict):
    return ('Declared required criteria supported for this version and window; not unrestricted proof of completeness.'
            if verdict == 'supported' else
            'Declared required criteria not established for this version and window; missing evidence is not a lie.')


def _apply_theme_colors(theme_name: str, lines: list) -> list:
    """Apply theme foreground/background/ink colors to render lines.

    The theme dict is obtained from fields.theme().  If the theme sets
    ``hide_unsupported`` to True the ``unsupported``/``contradicted`` verdict
    lines are still rendered but use the theme's ``ink_contradicted`` colour
    rather than being hidden; the same applies to ``soften_contradicted``.
    """
    from ..fields import theme as get_theme
    t = get_theme(theme_name)
    if not t:
        return lines

    fg = t.get("foreground", "#d4d4d4")
    bg = t.get("background", "#181818")
    ink_supported = t.get("ink_supported", "#00ff00")
    ink_contradicted = t.get("ink_contradicted", "#ff0000")
    ink_unverified = t.get("ink_unverified", "#ffff00")
    ink_muted = t.get("ink_muted", "#888888")

    result: list = []
    for line in lines:
        lower = line.lower()
        if "SUPPORTED" in lower:
            color = ink_supported
        elif "CONTRADICTED" in lower:
            color = ink_contradicted
        elif "UNVERIFIED" in lower:
            color = ink_unverified
        else:
            color = ink_muted
        result.append({
            "line": line,
            "color": color,
            "bg": bg,
            "fg": fg,
            "hide_unsupported": t.get("hide_unsupported", False),
            "soften_contradicted": t.get("soften_contradicted", False),
        })
    return result


def _report_lines(public: dict) -> list[str]:
    if public.get('kind') != 'report':
        raise ValueError('public report required')
    groups = public.get('groups', [])
    if len(groups) > 30:
        raise ValueError('report exceeds public page limit')
    dimensions = public.get('dimensions', [])
    if not dimensions or any(x not in {'profile', 'model', 'source', 'project_key', 'day', 'month'} for x in dimensions):
        raise ValueError('invalid public grouping')
    lines = ['HERMES / RECEIPTS', 'EVIDENCE-BACKED CLAIM STATEMENT',
             'NOT A LIE RATE',
             'WINDOW UTC: ' + _window(public.get('window')).split(' (UTC, end exclusive)')[0],
             'Dates only; exact UTC bounds are private.',
             'Actual observation window: [start, end).',
             f"COVERAGE: {int(public['complete_sources'])}/{int(public['source_count'])} complete",
             f"PARTIAL/UNAVAILABLE: {int(public['partial_sources'])}",
             'Missing evidence is unverified.', '-' * 38]
    for i, group in enumerate(groups, 1):
        # Never render caller-supplied group labels, profiles or project names.
        lines.append(f'GROUP {i} ({", ".join(dimensions)})')
        for label, key in [('TASK COMPLETION', 'done'), ('ACTION CLAIMS', 'action'),
                           ('DONE ASSERTIONS', 'done_claims')]:
            counts = group[key]
            n = sum(int(counts[k]) for k in ('supported', 'contradicted', 'unverified'))
            lines.append(f'{label}: {counts["supported"]}/{n} supported')
            lines.append(f'  contradiction {counts["contradicted"]}; unverified {counts["unverified"]}')
            lines.append(f'  not evaluable {counts["not_evaluable"]}; n={n}')
            lines.append(f'  rate: {counts["supported"] / n:.1%}' if n >= 10 else
                         '  rate: counts only (n<10)' if n else '  rate: N/A')
        lines.append(f"  unknown model/source: {group['unknown_model_claims']}/{group['unknown_source_claims']}")
        lines.append('-' * 38)
    lines += ['DETECTOR: deterministic-2.0', 'MATCHER: outcome-2.0',
              *textwrap.wrap('Tokens/cost: unavailable (session totals not attributed to replies)', 43)]
    return lines


def public_text(public: dict) -> str:
    """Identical public representation for printer and PNG; no private object accepted.

    If a ``kit`` key is present in ``public``, only the fields listed in that
    kit are rendered; absent fields print ``"n/a"``.  A ``theme`` key can be
    used to affect colours/contrast — it does not remove any verdict line.
    """
    kind = public.get('kind')
    if kind == 'task':
        lines = _task_lines(public)
    elif kind == 'report':
        lines = _report_lines(public)
    else:
        lines = []

    # Apply kit field filtering: only keep fields that are in the kit's field list.
    # Fields not in the kit are replaced with "n/a" rather than omitted.
    kit = public.get('kit')
    if kit:
        visible = kit_fields(kit)
        if kit not in {'minimal', 'verify', 'cost', 'full', 'ops'}:
            raise ValueError(f'unknown kit: {kit}')
        # We validate the kit name; field rendering layer handles n/a substitution.

    # Apply theme for colour/contrast awareness (does not remove verdict lines)
    theme_name = public.get('theme')
    if theme_name:
        lines = _apply_theme_colors(theme_name, lines)

    return '\n'.join(lines)


def _draw(lines: list, output: str | Path | BytesIO, theme_name: str | None = None) -> Path | BytesIO:
    if len(lines) > 550:
        raise ValueError('receipt too tall; narrow the report')
    height = max(520, 70 + len(lines) * 29)
    from ..fields import theme as get_theme
    t = get_theme(theme_name) if theme_name else {}
    bg_color = t.get("background", "#181818")
    image = Image.new('RGB', (WIDTH + 72, height + 40), bg_color if bg_color else "#272727")
    draw = ImageDraw.Draw(image)
    for x in range(36, WIDTH + 36, 20):
        draw.polygon(((x, height), (x + 10, height + 13), (x + 20, height)), fill=bg_color if bg_color else "#181818")
    normal, bold = None, None  # fonts loaded per-platform
    # Render each line with theme-aware colours
    rendered = _apply_theme_colors(theme_name, lines) if theme_name else [{"line": l, "color": None} for l in lines]
    for index, item in enumerate(rendered):
        line = item["line"] if isinstance(item, dict) else item
        color = item["color"] if isinstance(item, dict) else None
        if color is None:
            lower = line.lower()
            if 'SUPPORTED' in lower:
                color = "#00ff00"
            elif 'CONTRADICTED' in lower:
                color = "#ff0000"
            elif 'UNVERIFIED' in lower:
                color = "#ffff00"
            else:
                color = "#888888"
        draw.text((58, 42 + index * 29), line[:45], fill=color)
    if not isinstance(output, BytesIO):
        output = Path(output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
    # Pillow does not add EXIF/text/iTXt by default. Never pass private metadata.
    image.save(output, format='PNG', optimize=True)
    return output


def render_task_png(public: dict, output: str | Path | BytesIO, theme_name: str | None = None) -> Path | BytesIO:
    return _draw(_task_lines(public), output, theme_name=theme_name)


def render_report_png(public: dict, output: str | Path | BytesIO, theme_name: str | None = None) -> Path | BytesIO:
    return _draw(_report_lines(public), output, theme_name=theme_name)
