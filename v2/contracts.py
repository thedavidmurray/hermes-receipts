"""Frozen v2 private contracts. Public serializers must use a separate allowlist."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Literal, NotRequired, TypedDict

SCHEMA_VERSION = 2
Verdict = Literal['supported', 'contradicted', 'unverified', 'not_evaluable']
Kind = Literal['tests', 'commit', 'push', 'send', 'deploy', 'write', 'run', 'done']


class Scope(TypedDict):
    source_id: str
    profile: str
    session_id: str
    user_message_id: str
    final_message_id: str


class Attribution(TypedDict):
    model: str
    source: str
    certainty: Literal['exact', 'session_only', 'unknown']


class Claim(TypedDict):
    claim_id: str
    scope: Scope
    kind: Kind
    span_start: int
    span_end: int
    target_key: str | None
    task_key: str | None
    project_key: str | None
    detector_version: str


class Evidence(TypedDict):
    evidence_id: str
    scope: Scope
    call_id: str
    action: str
    target_key: str | None
    artifact_version: str | None
    observed_at: float
    level: Literal['invocation', 'process_result', 'outcome_readback']
    status: Literal['ok', 'error', 'unknown']


class Finding(TypedDict):
    claim_id: str
    verdict: Verdict
    reason_code: str
    evidence_ids: list[str]
    matcher_version: str


class Requirement(TypedDict):
    requirement_id: str
    public_label: str
    required: bool
    evidence_predicate: str
    target_key: str | None
    artifact_version: str | None


class TaskReceipt(TypedDict):
    task_key: str
    declared_scope_label: str
    observation_start: float
    observation_end: float
    requirements: list[Requirement]
    criterion_findings: dict[str, Finding]
    completion: Finding
    unresolved_requirement_ids: list[str]


class FleetLimits(TypedDict, total=False):
    page_size: int
    max_finals: int
    max_seconds: float
    all_time: bool
    cancelled: object  # threading.Event with is_set(); never exported


class SourceCoverage(TypedDict):
    record_type: Literal['source']
    source_id: str
    schema_version: int
    availability: Literal['available', 'unavailable']
    cutoff: float | None
    coverage_reason: str
    cursor: NotRequired[str]  # private resumption hint, not a public identifier


class TurnRecord(TypedDict):
    record_type: Literal['turn']
    turn_key: str
    source_id: str
    profile: str
    session_id: str
    user_id: str
    final_id: str
    final_time: float
    model: str
    model_certainty: str
    source: str
    project_key: str | None
    scope: Scope
    answer: str  # memory-only, never written to store
    messages: list[dict]  # memory-only; C normalizes evidence before storage


def fingerprint_target(target: str, key: bytes) -> str:
    """HMAC target metadata with a caller-held local key, never the raw target.

    Caller stores/protects its key outside source/report DB and never exports it;
    no config/credential file is read by the fleet scanner.
    """
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError('target fingerprint requires a 32-byte local key')
    return hmac.new(key, target.encode('utf-8'), hashlib.sha256).hexdigest()


def identity(*parts: object) -> str:
    """SHA256 of a domain-separated, canonical composite; never a public raw identifier."""
    return hashlib.sha256(json.dumps(['receipts.v2', *parts], separators=(',', ':'),
                                      ensure_ascii=False).encode('utf-8')).hexdigest()


def turn_key(scope: Scope) -> str:
    return identity('turn', scope['source_id'], scope['profile'], scope['session_id'],
                    scope['user_message_id'], scope['final_message_id'])


def claim_key(scope: Scope, kind: Kind, start: int, end: int, detector_version: str) -> str:
    return identity('claim', turn_key(scope), kind, start, end, detector_version)


def evidence_key(scope: Scope, call_id: str, action: str, level: str) -> str:
    return identity('evidence', turn_key(scope), call_id, action, level)
