"""Explicit manifest-only completed-task gate; never run manifest predicates as code."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .contracts import Claim, Evidence, Finding, Requirement, TaskReceipt, Verdict
from .evidence import MATCHER_VERSION

# Fixed IDs. Manifest fields are data, never executable Python/shell/SQL.
_PREDICATES = frozenset({'artifact_digest', 'tests_pass', 'remote_commit'})


def _finding(cid: str, verdict: Verdict, reason: str, evidence: Sequence[Evidence] = ()) -> Finding:
    return Finding(claim_id=cid, verdict=verdict, reason_code=reason,
                   evidence_ids=sorted({item['evidence_id'] for item in evidence}), matcher_version=MATCHER_VERSION)


def judge_task(requirements: Sequence[Requirement], findings: Sequence[Evidence],
               window: tuple[float, float], *, claim: Claim | None = None,
               links: Mapping | None = None) -> TaskReceipt:
    """Judge declared required criteria against *observed* readbacks only.

    `findings` is normalized private evidence, not arbitrary caller verdicts; this
    prevents a forged supported Finding from certifying a required outcome.
    Cross-turn evidence needs an explicit task+target+version linkage in `links`.
    A receipt's supported text is scoped to required criteria, version and window.
    """
    start, end = window
    if start >= end:
        raise ValueError('invalid half-open observation window')
    task_key = claim.get('task_key') if claim else None
    cid = claim['claim_id'] if claim else ''
    claim_ok = bool(claim and claim['kind'] == 'done' and task_key and claim.get('target_key')
                    and claim['target_key'] in {r.get('target_key') for r in requirements if r.get('required')})
    ids = [r.get('requirement_id') for r in requirements]
    malformed = len(ids) != len(set(ids)) or not ids or any(not isinstance(i, str) or not i for i in ids)
    unknown = any(r.get('required') and r.get('evidence_predicate') not in _PREDICATES for r in requirements)
    required = [r for r in requirements if r.get('required')]
    malformed |= not required or any(not r.get('target_key') or not r.get('artifact_version') for r in required)
    versions = {r['artifact_version'] for r in required if r.get('artifact_version')}
    malformed |= len(versions) != 1
    details: dict[str, Finding] = {}
    for r in requirements:
        rid = r.get('requirement_id')
        if not isinstance(rid, str) or not rid or rid in details:
            continue
        predicate, target, version = r.get('evidence_predicate'), r.get('target_key'), r.get('artifact_version')
        if predicate not in _PREDICATES or not target or not version:
            details[rid] = _finding(rid, 'unverified', 'unknown_or_unbound_criterion')
            continue
        eligible = []
        for item in findings:
            if (item.get('action') != predicate or item.get('target_key') != target
                    or item.get('artifact_version') != version
                    or item.get('level') != 'outcome_readback'
                    or not start <= item.get('observed_at', float('-inf')) < end):
                continue
            if claim is None:
                continue
            a, b = claim['scope'], item['scope']
            if any(a[k] != b[k] for k in ('source_id', 'profile', 'session_id')):
                continue
            same_turn = (a['user_message_id'] == b['user_message_id']
                         and a['final_message_id'] == b['final_message_id'])
            linked = bool(links and task_key and links.get('task_key') == task_key
                          and links.get('target_key') == target
                          and links.get('artifact_version') == version)
            if same_turn or linked:
                eligible.append(item)
        # Latest explicit observation controls stateful outcomes. Earlier errors
        # cannot override repaired success, nor can stale success hide removal.
        eligible = sorted(eligible, key=lambda e: (e['observed_at'], e['evidence_id']))
        latest = eligible[-1:] if eligible else []
        good = [e for e in latest if e['status'] == 'ok']
        bad = [e for e in latest if e['status'] == 'error']
        if good:
            details[rid] = _finding(rid, 'supported', 'required_outcome_readback', good)
        elif bad:
            details[rid] = _finding(rid, 'contradicted', 'explicit_outcome_mismatch', bad)
        else:
            details[rid] = _finding(rid, 'unverified', 'no_applicable_readback')
    unresolved = [r['requirement_id'] for r in requirements if r.get('requirement_id') not in details
                  or details[r['requirement_id']]['verdict'] != 'supported']
    supported = claim_ok and not malformed and not unknown and all(
        details[r['requirement_id']]['verdict'] == 'supported' for r in required)
    version_label = next(iter(versions)) if len(versions) == 1 else 'unresolved'
    if supported:
        label = (f'Declared required criteria supported for {version_label} in '
                 f'[{start:g}, {end:g}); not unrestricted proof of completeness.')
        completion = _finding(cid, 'supported', 'declared_required_scope_supported',
                              [e for e in findings if e['evidence_id'] in {
                                  eid for r in required for eid in details[r['requirement_id']]['evidence_ids']}])
    else:
        label = (f'Declared required criteria not established for {version_label} in '
                 f'[{start:g}, {end:g}); not unrestricted proof of completeness.')
        completion = _finding(cid, 'unverified',
                              'ambiguous_or_missing_manifest' if malformed or unknown or not claim_ok
                              else 'required_criteria_unresolved')
    return TaskReceipt(task_key=task_key or '', declared_scope_label=label,
                       observation_start=start, observation_end=end,
                       requirements=list(requirements), criterion_findings=details,
                       completion=completion, unresolved_requirement_ids=unresolved)
