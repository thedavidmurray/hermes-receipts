"""One intentionally narrow public contract for pixels, text and printer output.

Everything a receipt can print is built here from an allowlist, never copied:

* criterion labels come from a reviewed vocabulary (:data:`ALIASES`),
* criterion *reasons* come from a reviewed phrase table keyed by the matcher's
  fixed reason codes,
* evidence becomes a ``sha256-<12>`` locator prefix,
* telemetry passes through :func:`v2.fields.public_metrics`, which pseudonymises
  every run identifier,
* the receipt carries its own content hash so a reader can re-verify it.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Mapping

from .contracts import SCHEMA_VERSION
from .detect import DETECTOR_VERSION
from .evidence import MATCHER_VERSION
from .fields import plugin_provenance, pseudonymize, public_metrics

VERDICTS = ('supported', 'contradicted', 'unverified', 'not_evaluable')
# Deliberately reviewed vocabulary, not arbitrary requirement text from a manifest.
ALIASES = frozenset({'Artifact bytes match', 'Tests pass for this version',
                     'Commit present in remote', 'Required outcome observed',
                     'Deployment observed', 'Message delivered'})
VERSION = re.compile(r'(?:v?[0-9]+(?:\.[0-9]+){0,3}(?:-[a-z0-9]{1,12})?|sha256-[0-9a-f]{12,64})\Z')
#: Fixed criterion predicates (matcher vocabulary); anything else is 'unknown'.
CHECK_KINDS = frozenset({'artifact_digest', 'tests_pass', 'remote_commit'})
#: What was checked, per predicate: one short line for a reader.
CHECK_PHRASES = {
    'artifact_digest': 'artifact bytes compared with the declared digest',
    'tests_pass': 'a recorded test run bound to this version',
    'remote_commit': 'the declared commit present in the declared remote',
    'unknown': 'an unrecognised criterion predicate',
}
#: Why this criterion is not supported, per matcher reason code.
REASON_PHRASES = {
    'required_outcome_readback': 'an observed outcome readback',
    'explicit_outcome_mismatch': 'an observed outcome contradicts this criterion',
    'no_applicable_readback': 'no applicable readback in this version and window',
    'unknown_or_unbound_criterion': 'the criterion is not bound to a version and target',
    'ambiguous_or_missing_manifest': 'the manifest is incomplete or ambiguous',
    'required_criteria_unresolved': 'required criteria are unresolved',
    'declared_required_scope_supported': 'every required criterion is supported',
    'no_evidence': 'no evidence was observed for this criterion',
    'unknown': 'no reason was recorded',
}
#: Sharper, predicate-specific wording for the common "nothing observed" case.
UNVERIFIED_REASONS = {
    ('remote_commit', 'no_applicable_readback'): 'remote ref not found in this window',
    ('tests_pass', 'no_applicable_readback'): 'no test run after this version',
    ('artifact_digest', 'no_applicable_readback'): 'artifact bytes were not read in this window',
}
MAX_EVIDENCE_LOCATORS = 3


def _date(value):
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime('%Y-%m-%d UTC')
    except (ValueError, TypeError, OverflowError, OSError):
        return 'unknown date'


def _timestamp(value):
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (ValueError, TypeError, OverflowError, OSError):
        return 'unknown time'


def _version(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
        return 'sha256-' + value[:12]
    return value if isinstance(value, str) and VERSION.fullmatch(value) else 'version withheld'


def _locators(finding: Mapping) -> list[str]:
    """Non-reversible evidence locators: the same prefix rule as artifact versions."""
    ids = finding.get('evidence_ids')
    if not isinstance(ids, list):
        return []
    return ['sha256-' + str(item)[:12] for item in sorted(str(x) for x in ids)[:MAX_EVIDENCE_LOCATORS]
            if isinstance(item, str) and re.fullmatch(r'[0-9a-f]{8,64}', item)]


def _criterion(requirement: Mapping, finding: Mapping) -> dict:
    predicate = requirement.get('evidence_predicate')
    kind = predicate if predicate in CHECK_KINDS else 'unknown'
    reason_code = finding.get('reason_code')
    verdict = finding.get('verdict')
    verdict = verdict if verdict in VERDICTS else 'not_evaluable'
    reason = (UNVERIFIED_REASONS.get((kind, str(reason_code)))
              or REASON_PHRASES.get(str(reason_code), REASON_PHRASES['unknown']))
    return {'kind': kind, 'check': CHECK_PHRASES[kind], 'reason': reason,
            'evidence': _locators(finding), 'verdict': verdict}


def receipt_content_hash(public: Mapping) -> str:
    """SHA256 over this receipt's own content (the hash field itself excluded)."""
    content = {key: value for key, value in public.items() if key != 'receipt_hash'}
    canonical = json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return 'sha256-' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def verify_receipt_hash(public: Mapping) -> bool:
    """True when the stored ``receipt_hash`` matches this receipt's content."""
    stored = public.get('receipt_hash')
    return isinstance(stored, str) and stored == receipt_content_hash(public)


def to_public_receipt(private: Mapping, reviewed_labels: Mapping[str, str] | None = None, *,
                      kit: str | None = None, theme: str | None = None,
                      fields: list[str] | str | None = None,
                      metrics: Mapping | None = None,
                      categories: Mapping | None = None) -> dict:
    """Construct afresh; never copy private keys or arbitrary labels into a public value.

    The caller explicitly reviews aliases; only fixed vocabulary is shareable.
    If no reviewed alias is supplied, a numbered pseudonym replaces the label.

    Keyword arguments:
    kit:        named field kit (``minimal``/``verify``/``cost``/``ops``/``full``/``auto``,
                or a user kit dropped under the plugin config dir)
    theme:      named theme; aesthetics only, never verdict visibility
    fields:     explicit field list, overrides ``kit``
    metrics:    reviewed telemetry (see :func:`v2.fields.sanitize_metrics`); run
                identifiers are pseudonymised before they enter the receipt
    categories: measured session categories, consumed by the ``auto`` kit
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
        entry = {'label': label, 'required': requirement.get('required') is True}
        entry.update(_criterion(requirement, findings.get(rid) or {}))
        criteria.append(entry)
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
    provenance = plugin_provenance()
    task_key = private.get('task_key')
    generated = private.get('generated_at')
    receipt = {
        'schema_version': SCHEMA_VERSION, 'kind': 'task',
        'title': 'Your agent says it\u2019s done. Get the receipt.',
        'task': 'Task (pseudonym)', 'completion': verdict, 'criteria': criteria,
        'artifact_version': version, 'window': f'{start} to {end} (UTC, end exclusive)',
        'qualification': ('Declared required criteria supported for this version and window; '
                          'not unrestricted proof of completeness.' if verdict == 'supported' else
                          'Declared required criteria not established for this version and window; '
                          'missing evidence is not a lie.'),
        'detector_version': DETECTOR_VERSION, 'matcher_version': MATCHER_VERSION,
        'plugin_version': provenance['plugin_version'], 'git_sha': provenance['git_sha'],
        'timestamp': _timestamp(generated if generated is not None
                                else datetime.now(timezone.utc).timestamp()),
        'warning': 'Experimental viewer; evidence scope and coverage are limited.',
        'metrics': public_metrics(metrics or {}),
        'kit': kit, 'theme': theme, 'fields': fields, 'categories': categories,
        'receipt_id': pseudonymize(task_key) if task_key else None,
    }
    receipt['receipt_hash'] = receipt_content_hash(receipt)
    return receipt


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
                     group_by: list[str], kit: str | None = None, theme: str | None = None,
                     metrics: Mapping | None = None) -> dict:
    """Aggregate only allowlisted counts; group values become non-reversible ordinals.

    Keyword arguments:
    kit:     named field kit — controls which fields print; absent fields print ``n/a``
    theme:   named theme — aesthetics only, never verdict visibility
    metrics: reviewed telemetry for the scan itself (sessions, tools, wall clock)
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
    provenance = plugin_provenance()
    receipt = {
        'schema_version': SCHEMA_VERSION, 'kind': 'report',
        'title': 'Evidence-backed claim statement',
        'window': f'{_date(start)} to {_date(end)} (UTC, end exclusive)',
        'dimensions': list(group_by), 'groups': groups, 'source_count': len(coverage),
        'complete_sources': complete, 'partial_sources': len(coverage) - complete,
        'warning': 'Observed evidence-backed claim rate, not a lie rate. Missing evidence is unverified.',
        'detector_version': DETECTOR_VERSION, 'matcher_version': MATCHER_VERSION,
        'plugin_version': provenance['plugin_version'], 'git_sha': provenance['git_sha'],
        'timestamp': _timestamp(datetime.now(timezone.utc).timestamp()),
        'metrics': public_metrics(metrics or {}),
        'kit': kit, 'theme': theme,
    }
    receipt['receipt_hash'] = receipt_content_hash(receipt)
    return receipt
