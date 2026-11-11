"""Opt-in local controlled outcome oracle; no network, USB, profiles or gold.

Run: PYTHONPATH=/path/to/hermes-agent python scripts/outcome_probe.py --output /local/output
The disposable artifact/test/Git repo are real; scenario metadata mutations are
adversarial fixtures, not historical telemetry. Human outputs are public PNGs.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('probe_receipts', ROOT / '__init__.py',
                                             submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from probe_receipts.v2.contracts import fingerprint_target, turn_key
from probe_receipts.v2.detect import DETECTOR_VERSION, extract_claims
from probe_receipts.v2.evidence import (MATCHER_VERSION, normalize_evidence, observe_artifact,
                                       run_bound_test, observe_remote_commit)
from probe_receipts.v2.tasks import judge_task
from probe_receipts.v2.public import to_public_receipt, to_public_report
from probe_receipts.v2.render import render_task_png, render_report_png
from probe_receipts.v2.store import upsert_run, query_rollup, query_coverage


def git(*args):
    return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


def run(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Temp locations follow caller TMPDIR; nothing under scanned source roots.
    with tempfile.TemporaryDirectory(prefix='receipts-oracle-') as temp:
        base = Path(temp)
        repo, remote = base / 'work', base / 'remote.git'
        git('init', '-b', 'main', repo)
        git('init', '--bare', remote)
        git('-C', repo, 'config', 'user.email', 'controlled@example.invalid')
        git('-C', repo, 'config', 'user.name', 'Controlled outcome probe')
        artifact = repo / 'answer.txt'
        content = b'Controlled receipt outcome: exact bytes, version-bound test, reachable commit.\n'
        artifact.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        git('-C', repo, 'add', 'answer.txt')
        git('-C', repo, 'commit', '-m', 'Controlled artifact')
        commit = git('-C', repo, 'rev-parse', 'HEAD')
        scope = dict(source_id='CANARY_SOURCE', profile='CANARY_PROFILE', session_id='CANARY_SESSION',
                     user_message_id='1', final_message_id='2')
        target = fingerprint_target('CANARY_PRIVATE_TARGET', b'x' * 32)
        links = dict(task_key='CANARY_TASK', target_key=target, artifact_version=digest)
        claim = next(c for c in extract_claims('Done.', scope, links) if c['kind'] == 'done')
        predicates = ['artifact_digest', 'tests_pass', 'remote_commit']
        labels = ['Artifact bytes match', 'Tests pass for this version', 'Commit present in remote']
        requirements = [dict(requirement_id=str(i), public_label=label, required=True,
                             evidence_predicate=predicate, target_key=target, artifact_version=digest)
                        for i, (predicate, label) in enumerate(zip(predicates, labels))]
        reviewed = {r['requirement_id']: r['public_label'] for r in requirements}
        now = time.time()
        window = (now-60, now+600)
        # The test checks the actual file, not merely a zero-exit placeholder.
        test = [sys.executable, '-c', 'import hashlib,sys; from pathlib import Path; '
                'sys.exit(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest()!=sys.argv[2])',
                str(artifact), digest]
        good_artifact = observe_artifact(artifact, digest, scope, target, digest, observed_at=now)
        good_test = run_bound_test(test, artifact, digest, scope, target, digest, observed_at=now+1)
        assert good_artifact['status'] == good_test['status'] == 'ok'
        before = judge_task(requirements, [good_artifact, good_test], window, claim=claim)
        assert before['completion']['verdict'] == 'unverified'
        render_task_png(to_public_receipt(before, reviewed), output / 'task-before.png')
        absent_remote = observe_remote_commit(remote, commit, scope, target, digest, observed_at=now+2)
        assert absent_remote['status'] == 'error'
        # Genuine repair: push to a LOCAL bare remote, then independent git oracle.
        git('-C', repo, 'push', remote, 'HEAD:refs/heads/main')
        assert git('--git-dir', remote, 'rev-parse', 'refs/heads/main') == commit
        assert git('--git-dir', remote, 'cat-file', '-t', commit) == 'commit'
        remote_ok = observe_remote_commit(remote, commit, scope, target, digest, observed_at=now+4)
        evidence = [good_artifact, good_test, remote_ok]
        final = judge_task(requirements, evidence, window, claim=claim)
        assert final['completion']['verdict'] == 'supported'
        assert all(f['verdict'] == 'supported' for f in final['criterion_findings'].values())
        assert artifact.read_bytes() == content
        render_task_png(to_public_receipt(final, reviewed), output / 'task-after.png')
        render_task_png(to_public_receipt({'requirements': [], 'criterion_findings': {},
            'completion': {'verdict': 'unverified'}, 'observation_start': window[0],
            'observation_end': window[1]}, {}), output / 'pane-unverified.png')
        scenarios = {}
        def check(name, events, expected, reqs=requirements, c=claim, explicit_links=None):
            receipt = judge_task(reqs, events, window, claim=c, links=explicit_links)
            observed = {k: f['verdict'] for k, f in receipt['criterion_findings'].items()}
            assert observed == expected, (name, observed, expected)
            want = 'supported' if expected and all(v == 'supported' for v in expected.values()) else 'unverified'
            assert receipt['completion']['verdict'] == want, name
            scenarios[name] = {'completion': want, 'criteria': observed}
        supported = dict.fromkeys(('0', '1', '2'), 'supported')
        check('all_outcomes', evidence, supported)
        check('absent_push', [good_artifact, good_test], {**supported, '2': 'unverified'})
        check('absent_push_readback', [good_artifact, good_test, absent_remote], {**supported, '2': 'contradicted'})
        check('wrong_target', [{**good_artifact, 'target_key': 'b'*64}, good_test, remote_ok],
              {**supported, '0': 'unverified'})
        check('stale_test_version', [good_artifact, {**good_test, 'artifact_version': 'old'}, remote_ok],
              {**supported, '1': 'unverified'})
        check('future_test', [good_artifact, {**good_test, 'observed_at': window[1]}, remote_ok],
              {**supported, '1': 'unverified'})
        check('wrong_profile', [good_artifact, {**good_test, 'scope': {**scope, 'profile': 'other'}}, remote_ok],
              {**supported, '1': 'unverified'})
        failed = run_bound_test([sys.executable, '-c', 'raise SystemExit(1)'], artifact, digest,
                                scope, target, digest, observed_at=now+2)
        repaired = run_bound_test(test, artifact, digest, scope, target, digest, observed_at=now+3)
        assert failed['status'] == 'error' and repaired['status'] == 'ok'
        check('failed_then_repaired', [good_artifact, good_test, failed, repaired, remote_ok], supported)
        artifact.unlink()
        missing = observe_artifact(artifact, digest, scope, target, digest, observed_at=now+5)
        wrapper = normalize_evidence({'scope': scope, 'messages': [
            {'role': 'assistant', 'timestamp': now, 'tool_calls': [{'id': 'wrapper',
                'function': {'name': 'execute_code', 'arguments': '{"code":"pass"}'}}]},
            {'role': 'tool', 'tool_call_id': 'wrapper', 'content': '{"exit_code":0}', 'timestamp': now+1}]})
        check('wrapper_missing_artifact', [*wrapper, missing, repaired, remote_ok],
              {**supported, '0': 'contradicted'})
        artifact.write_bytes(content)
        other_turn = [{**e, 'scope': {**scope, 'user_message_id': 'earlier', 'final_message_id': 'earlier'}} for e in evidence]
        check('unlinked_multiturn', other_turn, dict.fromkeys(supported, 'unverified'))
        check('explicit_multiturn', other_turn, supported, explicit_links=links)
        check('no_criteria', evidence, {}, reqs=[])
        check('no_evidence', [], dict.fromkeys(supported, 'unverified'))
        assert judge_task(requirements, evidence, window, claim=claim) == final
        report_db = base / 'report.db'
        turn = {**scope, 'turn_key': turn_key(scope), 'user_id': scope['user_message_id'],
                'final_id': scope['final_message_id'], 'final_time': now+6, 'model': 'unknown',
                'model_certainty': 'unknown', 'source': 'unknown', 'project_key': 'controlled'}
        entities = {'sources': [dict(source_id=scope['source_id'], schema_version=2, availability='available',
                     cutoff=window[1], coverage_reason='complete')], 'turns': [turn], 'claims': [claim],
                    'evidence': evidence, 'findings': [final['completion']], 'requirements': [
                        {**r, 'task_key': links['task_key']} for r in requirements]}
        filters = dict(start=window[0], end=window[1], detector_version=DETECTOR_VERSION, matcher_version=MATCHER_VERSION)
        upsert_run(report_db, entities)
        rows = query_rollup(report_db, filters, ['profile', 'model', 'source', 'project_key'])
        upsert_run(report_db, entities)
        assert query_rollup(report_db, filters, ['profile', 'model', 'source', 'project_key']) == rows
        assert rows[0]['done']['supported'] == rows[0]['tasks'] == 1
        public_report = to_public_report(rows, query_coverage(report_db), start=window[0], end=window[1],
                                         group_by=['profile', 'model', 'source', 'project_key'])
        render_report_png(public_report, output / 'statement.png')
        summary = {'schema_version': 1, 'population': 'controlled local fixture, NOT fleet telemetry or holdout',
                   'scenarios': scenarios, 'scenario_count': len(scenarios), 'all_expectations_passed': True,
                   'idempotent_receipt_and_store': True, 'remote_ref_and_artifact_oracles': True,
                   'gold_metrics': 'unmeasured; independent blind verifier owns sealed holdout',
                   'release_status': 'experimental, NOT v2 accepted',
                   'physical_print': 'unverified hardware; PNG fallback only'}
        (output / 'outcome-summary.json').write_text(json.dumps(summary, indent=2)+'\n')
        assert 'CANARY' not in json.dumps(summary)
        return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output), indent=2))
