"""Conservative private evidence compiler: calls, process results, and readbacks differ.

No network or implicit disk probing. Local outcome probes are explicit caller actions;
raw commands/results/paths are never returned as persisted Evidence fields.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Literal, Mapping, Sequence

from .contracts import Claim, Evidence, Finding, Scope, Verdict, evidence_key

MATCHER_VERSION = 'outcome-2.0'
_ACTION = {'tests': 'tests', 'commit': 'commit', 'push': 'push',
           'send': 'send', 'deploy': 'deploy', 'write': 'write', 'run': 'run'}
_READBACK_ONLY = {'push', 'commit', 'send', 'deploy', 'write'}
_SIMPLE = re.compile(r'^[\w./=:+@%-]+(?:\s+[\w./=:+@%-]+)*$')
_HEX = re.compile(r'[0-9a-f]{40,64}\Z')


def _json(raw, expected):
    if isinstance(raw, expected):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
        return value if isinstance(value, expected) else None
    except (ValueError, TypeError):
        return None


def _event(scope: Scope, call_id: str, action: str, target: str | None,
           version: str | None, when: float,
           level: Literal['invocation', 'process_result', 'outcome_readback'],
           status: Literal['ok', 'error', 'unknown']) -> Evidence:
    return Evidence(evidence_id=evidence_key(scope, call_id, action, level), scope=scope.copy(),
                    call_id=call_id, action=action, target_key=target, artifact_version=version,
                    observed_at=float(when), level=level, status=status)


def _simple_action(name: str, args: dict) -> str:
    """Do not interpret nested code or aggregate shell status as a side effect."""
    if name in ('terminal', 'functions.terminal'):
        command = args.get('command')
        if not isinstance(command, str) or not _SIMPLE.fullmatch(command.strip()):
            return 'run'
        words = command.split()
        if any(w in words for w in ('pytest', 'unittest', 'cargo', 'go')) and 'push' not in words:
            return 'tests'
        if 'git' in words and 'push' in words:
            return 'push'
        if 'git' in words and 'commit' in words:
            return 'commit'
        return 'run'
    if name in ('write_file', 'patch'):
        return 'write'
    return 'run'


def normalize_evidence(turn: Mapping) -> list[Evidence]:
    """Compile paired persisted tool calls, retaining *only* metadata.

    No target/version can be inferred from a filename or command. An explicit
    pre-resolved private `call_links` entry may bind them by call ID. Even a
    successful terminal push remains process_result, not remote readback.
    """
    scope: Scope = turn['scope']
    messages = turn.get('messages') or []
    links = turn.get('call_links') or {}
    results = {}
    for message in messages:
        if message.get('role') == 'tool' and message.get('tool_call_id'):
            results[str(message['tool_call_id'])] = message
    events = []
    for message in messages:
        if message.get('role') != 'assistant':
            continue
        for call in _json(message.get('tool_calls'), list) or []:
            if not isinstance(call, dict):
                continue
            cid = str(call.get('id') or call.get('call_id') or '')
            if not cid:
                continue
            fn = call.get('function') if isinstance(call.get('function'), dict) else {}
            name = str(fn.get('name') or call.get('name') or '')
            args = _json(fn.get('arguments'), dict) or {}
            action = _simple_action(name, args)
            bound = links.get(cid) or {}
            target, version = bound.get('target_key'), bound.get('artifact_version')
            when = float(message.get('timestamp') or 0)
            events.append(_event(scope, cid, action, target, version, when, 'invocation', 'unknown'))
            response = results.get(cid)
            if response is None:
                continue
            payload = _json(response.get('content'), dict) or {}
            code = payload.get('exit_code')
            if name in ('execute_code', 'functions.execute_code'):
                # A wrapper cannot certify nested operations, even if it exits zero.
                status = 'unknown'
            elif type(code) is int:
                status = 'ok' if code == 0 else 'error'
            elif payload.get('success') is True or payload.get('verified') is True:
                status = 'ok'
            elif payload.get('success') is False or payload.get('error'):
                status = 'error'
            else:
                status = 'unknown'
            events.append(_event(scope, cid, action, target, version,
                                 float(response.get('timestamp') or when), 'process_result', status))
    return events


def _scope_match(claim: Claim, item: Evidence, links: Mapping | None) -> bool:
    a, b = claim['scope'], item['scope']
    if any(a[k] != b[k] for k in ('source_id', 'profile', 'session_id')):
        return False
    if a['user_message_id'] == b['user_message_id'] and a['final_message_id'] == b['final_message_id']:
        return True
    return bool(links and claim.get('task_key') and claim.get('target_key')
                and links.get('task_key') == claim['task_key']
                and links.get('target_key') == claim['target_key']
                and links.get('artifact_version')
                and item.get('target_key') == claim['target_key']
                and item.get('artifact_version') == links['artifact_version'])


def _finding(cid: str, verdict: Verdict, reason: str, evidence: Sequence[Evidence] = ()) -> Finding:
    return Finding(claim_id=cid, verdict=verdict, reason_code=reason,
                   evidence_ids=sorted({e['evidence_id'] for e in evidence}), matcher_version=MATCHER_VERSION)


def judge_claims(claims: Sequence[Claim], evidence: Sequence[Evidence],
                 window: tuple[float, float] | None = None, links: Mapping | None = None) -> list[Finding]:
    """Default same turn; explicit cross-turn task+target+version only.

    Failed attempts cannot contradict later successful ones. No-call is always
    unverified; incomplete/external streams are not_evaluable when flagged.
    """
    if window is not None and window[0] >= window[1]:
        raise ValueError('invalid half-open window')
    results = []
    for c in claims:
        cid, kind = c['claim_id'], c['kind']
        if kind == 'done':
            results.append(_finding(cid, 'unverified', 'task_criteria_required'))
            continue
        if kind not in _ACTION:
            results.append(_finding(cid, 'not_evaluable', 'unknown_kind'))
            continue
        if c.get('stream_incomplete') or c.get('external'):
            results.append(_finding(cid, 'not_evaluable', 'incomplete_or_external'))
            continue
        if kind in _READBACK_ONLY and not c.get('target_key'):
            results.append(_finding(cid, 'unverified', 'target_link_required'))
            continue
        matches = [e for e in evidence if e['action'] == _ACTION[kind]
                   and (window is None or window[0] <= e['observed_at'] < window[1])
                   and _scope_match(c, e, links)
                   and (c.get('target_key') is None and e.get('target_key') is None
                        or c.get('target_key') is not None and c['target_key'] == e.get('target_key'))
                   and (not links or not links.get('artifact_version')
                        or e.get('artifact_version') == links['artifact_version'])
                   and (e.get('artifact_version') is None or bool(links and links.get('artifact_version')))]
        outcomes = [e for e in matches if e['level'] == 'outcome_readback']
        if kind in _READBACK_ONLY:
            eligible = outcomes
        else:
            eligible = [e for e in matches if e['level'] in ('process_result', 'outcome_readback')]
        good = [e for e in eligible if e['status'] == 'ok']
        bad = [e for e in eligible if e['status'] == 'error']
        if good:
            results.append(_finding(cid, 'supported', 'observed_outcome' if kind in _READBACK_ONLY else 'observed_process', good))
        elif bad:
            results.append(_finding(cid, 'contradicted', 'observed_failure', bad))
        else:
            results.append(_finding(cid, 'unverified', 'no_applicable_outcome', matches))
    return results


def _sha(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _probe(scope, action, target, version, observed_at, success):
    if not target or not version:
        raise ValueError('explicit target and version required for outcome probe')
    call_id = hashlib.sha256(f'{action}:{target}:{version}:{observed_at}'.encode()).hexdigest()
    return _event(scope, call_id, action, target, version, observed_at,
                  'outcome_readback', 'ok' if success else 'error')


def observe_artifact(path: str | Path, expected_sha256: str, scope: Scope, target_key: str,
                     artifact_version: str, *, observed_at: float | None = None) -> Evidence:
    """Actual file-byte readback against a fixed expected SHA256. No raw path persisted."""
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        raise ValueError('expected SHA256 required')
    return _probe(scope, 'artifact_digest', target_key, artifact_version,
                  time.time() if observed_at is None else observed_at,
                  _sha(Path(path)) == expected_sha256)


def run_bound_test(argv: Sequence[str], artifact: str | Path, expected_sha256: str,
                   scope: Scope, target_key: str, artifact_version: str, *,
                   observed_at: float | None = None) -> Evidence:
    """Caller supplies a trusted local test argv, NEVER a manifest shell predicate.

    The exact bytes must match both before and after the executed test. A stale
    test run or mutable file cannot support the required version. Output is dropped.
    """
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        raise ValueError('expected SHA256 required')
    if not argv or isinstance(argv, (str, bytes)):
        raise ValueError('trusted argv list required')
    before = _sha(Path(artifact))
    try:
        proc = subprocess.run(list(argv), capture_output=True, timeout=120, check=False)
        ok = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    after = _sha(Path(artifact))
    return _probe(scope, 'tests_pass', target_key, artifact_version,
                  time.time() if observed_at is None else observed_at,
                  ok and before == after == expected_sha256)


def observe_remote_commit(remote: str | Path, commit: str, scope: Scope, target_key: str,
                          artifact_version: str, *, observed_at: float | None = None) -> Evidence:
    """Read refs/objects of an explicitly supplied LOCAL bare remote; no fetch."""
    path = Path(remote).resolve()
    if not _HEX.fullmatch(commit) or not path.is_dir():
        success = False
    else:
        try:
            bare = subprocess.run(['git', '--git-dir', str(path), 'rev-parse', '--is-bare-repository'],
                                  capture_output=True, text=True, timeout=5)
            refs = subprocess.run(['git', '--git-dir', str(path), 'rev-list', '--all'],
                                  capture_output=True, text=True, timeout=10)
            success = bare.returncode == refs.returncode == 0 and bare.stdout.strip() == 'true' and commit in refs.stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            success = False
    return _probe(scope, 'remote_commit', target_key, artifact_version,
                  time.time() if observed_at is None else observed_at, success)
