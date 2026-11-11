"""One intentionally narrow public contract for pixels, text and printer output."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Mapping

from .contracts import SCHEMA_VERSION
from .detect import DETECTOR_VERSION
from .evidence import MATCHER_VERSION
from .fields import render_fields, kit_fields, theme as get_theme, FIELD_SPECS

VERDICTS = ('supported', 'contradicted', 'unverified', 'not_evaluable')
# Deliberately reviewed vocabulary, not arbitrary requirement text from a manifest.
ALIASES = frozenset({'Artifact bytes match', 'Tests pass for this version',
                     'Commit present in remote', 'Required outcome observed',
                     'Deployment observed', 'Message delivered'})
VERSION = re.compile(r'(?:v?[0-9]+(?:\.[0-9]+){0,3}(?:-[a-z0-9]{1,12})?|sha256-[0-9a-f]{12})\Z')


def _date(value):
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime('%Y-%m-%d UTC')
    except (ValueError, TypeError, OverflowError, OSError):
        return 'unknown date'


def _version(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
        return 'sha256-' + value[:12]
    return value if isinstance(value, str) and VERSION.fullmatch(value) else 'version withheld'


def to_public_receipt(private: Mapping, reviewed_labels: Mapping[str, str] | None = None,
                      kit: str | None = None, theme: str | None = None) -> dict:
    """Construct afresh; never copy private keys or arbitrary labels into a public value.

    The caller explicitly reviews aliases; only fixed vocabulary is shareable.
    If no reviewed alias is supplied, a numbered pseudonym replaces the label.

    Keyword arguments:
    kit:     Name of a field kit (minimal, verify, cost, full, ops) — controls
             which fields are rendered; absent fields print "n/a".
    theme:   Name of a theme (thermal-classic, minimal-mono, bold-branded) —
             affects colours/contrast without removing verdict lines.
    """
    reviewed_labels = reviewed_labels or {}
    criteria = []
    requirements = private.get('requirements') or []
    findings = private.get('criterion_findings') or {}
    if not isinstance(requirements, list) or not isinstance(findings, dict):
        raise ValueError('invalid private receipt')
    if len(requirements) > 40:
        raise ValueError('too many criteria for one public page; narrow the task')
    if not isinstance(reviewed_labels, Mapping):
        raise ValueError('reviewed labels must be a mapping')
    for index, requirement in enumerate(requirements, 1):
        if not isinstance(requirement, dict):
            raise ValueError('invalid criterion')
        rid = requirement.get('requirement_id')
        alias = reviewed_labels.get(rid)
        label = alias if isinstance(alias, str) and alias in ALIASES else f'Criterion {index}'
        finding = findings.get(rid) or {}
        verdict = finding.get('verdict')
        criteria.append({'label': label, 'required': requirement.get('required') is True,
                         'verdict': verdict if verdict in VERDICTS else 'not_evaluable'})
    completion = private.get('completion') or {}
    verdict = completion.get('verdict')
    verdict = verdict if verdict in VERDICTS else 'not_evaluable'
    # Refuse a plausible-looking supported completion without a complete, explicitly
    # bound required manifest; the matcher remains the authority for evidence.
    required = [r for r in requirements if r.get('required') is True]
    versions = {r.get('artifact_version') for r in required}
    if (not required or len(versions) != 1 or not all(r.get('target_key') and
            (findings.get(r.get('requirement_id')) or {}).get('verdict') == 'supported' for r in required)):
        verdict = 'unverified' if verdict == 'supported' else verdict
    version = _version(next(iter(versions)) if len(versions) == 1 else None)
    start, end = _date(private.get('observation_start')), _date(private.get('observation_end'))
    return {'schema_version': SCHEMA_VERSION, 'kind': 'task', 'title': 'Your agent says it\u2019s done. Get the receipt.',
            'task': 'Task (pseudonym)', 'completion': verdict, 'criteria': criteria,
            'artifact_version': version, 'window': f'{start} to {end} (UTC, end exclusive)',
            'qualification': ('Declared required criteria supported for this version and window; '
                              'not unrestricted proof of completeness.' if verdict == 'supported' else
                              'Declared required criteria not established for this version and window; '
                              'missing evidence is not a lie.'),
            'detector_version': DETECTOR_VERSION, 'matcher_version': MATCHER_VERSION,
            'warning': 'Experimental viewer; evidence scope and coverage are limited.',
            'cost': 'Tokens/cost: unavailable for this task',
            'kit': kit,
            'theme': theme}


def _counts(raw):
    if not isinstance(raw, dict):
        raw = {}
    counts = {name: max(0, int(raw.get(name, 0))) if str(raw.get(name, 0)).isdigit() else 0
              for name in VERDICTS}
    counts['denominator'] = sum(counts[k] for k in VERDICTS if k != 'not_evaluable')
    n = counts['denominator']
    counts['rate'] = (f"{counts['supported'] / n:.1%}" if n >= 10 else 'counts only (n<10)' if n else 'N/A')
    return counts


def to_public_report(rollup: list[dict], coverage: list[dict], *, start: float, end: float,
                     group_by: list[str], kit: str | None = None, theme: str | None = None) -> dict:
    """Aggregate only allowlisted counts; group values become non-reversible ordinals.

    Keyword arguments:
    kit:     Name of a field kit (minimal, verify, cost, full, ops) — controls
             which fields are rendered; absent fields print "n/a".
    theme:   Name of a theme (thermal-classic, minimal-mono, bold-branded) —
             affects colours/contrast without removing verdict lines.
    """
    safe_groups = {'profile', 'model', 'source', 'project_key', 'day', 'month'}
    if not group_by or any(g not in safe_groups for g in group_by):
        raise ValueError('unsupported public grouping')
    if len(rollup) > 30:
        raise ValueError('too many report groups for one public page; narrow the query')
    groups = []
    for index, row in enumerate(rollup, 1):
        groups.append({'label': f'Group {index} ({", ".join(group_by)})',
                       'action': _counts(row.get('action')),
                       'done': _counts(row.get('task_completion')),
                       'done_claims': _counts(row.get('done')),
                       'tasks': max(0, int(row.get('tasks', 0))),
                       'unknown_model_claims': max(0, int(row.get('unknown_model_claims', 0))),
                       'unknown_source_claims': max(0, int(row.get('unknown_source_claims', 0)))})
    complete = sum(c.get('availability') == 'available' and c.get('coverage_reason') == 'complete' for c in coverage)
    return {'schema_version': SCHEMA_VERSION, 'kind': 'report', 'title': 'Evidence-backed claim statement',
            'window': f'{_date(start)} to {_date(end)} (UTC, end exclusive)',
            'dimensions': list(group_by), 'groups': groups, 'source_count': len(coverage),
            'complete_sources': complete, 'partial_sources': len(coverage) - complete,
            'warning': 'Observed evidence-backed claim rate, not a lie rate. Missing evidence is unverified.',
            'detector_version': DETECTOR_VERSION, 'matcher_version': MATCHER_VERSION,
            'cost': 'Tokens/cost: unavailable (session totals not attributed to replies)',
            'kit': kit,
            'theme': theme}
