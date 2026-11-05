"""Lane C: observed outcomes, same-turn matching and explicit task scope."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from v2.contracts import claim_key, evidence_key
from v2.detect import extract_claims
from v2.evidence import normalize_evidence, judge_claims, observe_artifact, run_bound_test, observe_remote_commit
from v2.tasks import judge_task

SCOPE = dict(source_id='source', profile='alpha', session_id='s', user_message_id='u', final_message_id='f')


def claim(kind, scope=SCOPE, **links):
    return dict(claim_id=claim_key(scope, kind, 0, 4, 'd'), scope=scope, kind=kind,
                span_start=0, span_end=4, target_key=links.get('target_key'),
                task_key=links.get('task_key'), project_key=None, detector_version='d')


def ev(action, *, scope=SCOPE, call='c', target=None, version=None, level='outcome_readback', status='ok', at=15):
    return dict(evidence_id=evidence_key(scope, call, action, level), scope=scope, call_id=call,
                action=action, target_key=target, artifact_version=version,
                observed_at=at, level=level, status=status)


def req(name, predicate='artifact_digest', required=True, target='target', version='v1'):
    return dict(requirement_id=name, public_label=name, required=required,
                evidence_predicate=predicate, target_key=target, artifact_version=version)


def test_normalize_tool_results_never_promotes_wrappers_or_compound_shell():
    calls = [dict(id='nested', function=dict(name='execute_code', arguments=json.dumps({'code': 'terminal("git push")'}))),
             dict(id='mixed', function=dict(name='terminal', arguments=json.dumps({'command': 'git push; true'}))),
             dict(id='direct', function=dict(name='terminal', arguments=json.dumps({'command': 'pytest -q'})))]
    messages = [dict(role='assistant', tool_calls=json.dumps(calls), timestamp=10)] + [
        dict(role='tool', tool_call_id=cid, content=json.dumps(result), timestamp=11)
        for cid, result in [('nested', {'exit_code': 0}), ('mixed', {'exit_code': 0}),
                            ('direct', {'exit_code': 0, 'output': '2 passed'})]]
    rows = normalize_evidence(dict(scope=SCOPE, messages=messages))
    assert all(x['level'] != 'outcome_readback' for x in rows)
    assert not any(x['action'] == 'push' and x['status'] == 'ok' for x in rows)
    assert [x['status'] for x in rows if x['call_id'] == 'direct' and x['level'] == 'process_result'] == ['ok']


def test_no_call_wrong_target_wrong_profile_or_wrong_version_never_contradict():
    c = claim('push', target_key='target')
    assert judge_claims([c], [])[0]['verdict'] == 'unverified'
    other = dict(SCOPE, profile='beta')
    for e in (ev('push', target='other'), ev('push', scope=other, target='target'),
              ev('push', target='target', version='v0')):
        assert judge_claims([c], [e])[0]['verdict'] != 'supported'
        assert judge_claims([c], [e])[0]['verdict'] != 'contradicted'
    # A claim without an explicit target must not inherit an arbitrary push.
    assert judge_claims([claim('push')], [ev('push', target='target')])[0]['verdict'] != 'supported'
    assert judge_claims([claim('push')], [ev('push', target=None)])[0]['verdict'] == 'unverified'


def test_failure_then_retry_late_and_missing_results():
    c = claim('tests', target_key='target')
    failed = ev('tests', target='target', level='process_result', status='error', call='first')
    good = ev('tests', target='target', level='process_result', status='ok', call='second')
    assert judge_claims([c], [failed])[0]['verdict'] == 'contradicted'
    assert judge_claims([c], [failed, good])[0]['verdict'] == 'supported'
    assert judge_claims([c], [ev('tests', target='target', level='invocation', status='unknown')])[0]['verdict'] == 'unverified'
    assert judge_claims([c], [ev('tests', target='target', at=40)], window=(0, 30))[0]['verdict'] == 'unverified'


def test_cross_turn_requires_explicit_task_target_version_and_same_profile():
    old = dict(SCOPE, user_message_id='earlier', final_message_id='earlier-final')
    c = claim('write', target_key='target', task_key='task')
    e = ev('write', scope=old, target='target', version='v1')
    assert judge_claims([c], [e])[0]['verdict'] == 'unverified'
    assert judge_claims([c], [e], links={'task_key': 'task', 'target_key': 'target', 'artifact_version': 'v1'})[0]['verdict'] == 'supported'
    assert judge_claims([c], [e], links={'task_key': 'task', 'target_key': 'target'})[0]['verdict'] == 'unverified'
    assert judge_claims([c], [ev('write', scope=dict(old, profile='beta'), target='target', version='v1')],
                        links={'task_key': 'task', 'target_key': 'target', 'artifact_version': 'v1'})[0]['verdict'] != 'supported'


def test_done_requires_explicit_nonempty_manifest_all_required_scope_and_window():
    c = claim('done', target_key='target', task_key='task')
    records = [ev('artifact_digest', target='target', version='v1', call='file'),
               ev('tests_pass', target='target', version='v1', call='test'),
               ev('remote_commit', target='target', version='v1', call='remote')]
    requirements = [req('bytes'), req('tests', 'tests_pass'), req('push', 'remote_commit')]
    for subset in ([], records[:1], records[:2]):
        receipt = judge_task(requirements, subset, (0, 30), claim=c)
        assert receipt['completion']['verdict'] == 'unverified'
    supported = judge_task(requirements + [req('optional', 'artifact_digest', False, 'other')], records, (0, 30), claim=c)
    assert supported['completion']['verdict'] == 'supported'
    assert supported['unresolved_requirement_ids'] == ['optional']
    assert supported['declared_scope_label'].startswith('Declared required criteria supported for v1 in [0, 30)')
    assert judge_task([], records, (0, 30), claim=c)['completion']['verdict'] == 'unverified'
    assert judge_task(requirements, records, (0, 30), claim=claim('done'))['completion']['verdict'] == 'unverified'
    assert judge_task(requirements, records, (0, 30), claim=claim('done', target_key='wrong', task_key='task'))['completion']['verdict'] == 'unverified'
    assert judge_task(requirements + [req('unknown', 'evil()')], records, (0, 30), claim=c)['completion']['verdict'] == 'unverified'
    assert judge_task(requirements, records, (20, 30), claim=c)['completion']['verdict'] == 'unverified'
    assert judge_task(requirements, records[:2] + [ev('remote_commit', target='target', version='v0')],
                      (0, 30), claim=c)['completion']['verdict'] == 'unverified'
    # A later explicit negative readback supersedes an earlier positive observation.
    removed = ev('artifact_digest', target='target', version='v1', status='error', call='removed', at=20)
    assert judge_task(requirements, records + [removed], (0, 30), claim=c)['completion']['verdict'] != 'supported'
    old = dict(SCOPE, user_message_id='old', final_message_id='old')
    prior = [ev(x['action'], scope=old, target='target', version='v1', call=x['call_id'], at=x['observed_at']) for x in records]
    assert judge_task(requirements, prior, (0, 30), claim=c)['completion']['verdict'] != 'supported'
    for required_link in ('task_key', 'target_key', 'artifact_version'):
        links = dict(task_key='task', target_key='target', artifact_version='v1')
        del links[required_link]
        assert judge_task(requirements, prior, (0, 30), claim=c, links=links)['completion']['verdict'] != 'supported'
    assert judge_task(requirements, prior, (0, 30), claim=c,
                      links=dict(task_key='task', target_key='target', artifact_version='v1'))['completion']['verdict'] == 'supported'


def test_controlled_real_artifact_test_bound_and_local_bare_remote(tmp_path):
    artifact = tmp_path / 'artifact.bin'
    artifact.write_bytes(b'exact bytes\x00')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    work = tmp_path / 'work'
    remote = tmp_path / 'bare.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
    subprocess.run(['git', 'init', '-q', str(work)], check=True)
    (work / 'artifact.bin').write_bytes(artifact.read_bytes())
    for cmd in (['git', '-C', str(work), 'add', '.'],
                ['git', '-C', str(work), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture']):
        subprocess.run(cmd, check=True)
    sha = subprocess.check_output(['git', '-C', str(work), 'rev-parse', 'HEAD'], text=True).strip()
    c = claim('done', target_key='target', task_key='task')
    required = [req('bytes'), req('tests', 'tests_pass'), req('remote', 'remote_commit')]
    file_e = observe_artifact(artifact, digest, SCOPE, 'target', 'v1', observed_at=15)
    # This test reads the artifact and checks its digest, not merely process exit.
    test_e = run_bound_test(['python3', '-c',
                             'import hashlib,sys; assert hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()==sys.argv[2]',
                             str(artifact), digest], artifact, digest, SCOPE, 'target', 'v1', observed_at=16)
    remote_e = observe_remote_commit(remote, sha, SCOPE, 'target', 'v1', observed_at=17)
    assert remote_e['status'] != 'ok'
    assert judge_task(required, [file_e, test_e, remote_e], (0, 30), claim=c)['completion']['verdict'] != 'supported'
    subprocess.run(['git', '-C', str(work), 'push', '-q', str(remote), 'HEAD:refs/heads/main'], check=True)
    remote_e = observe_remote_commit(remote, sha, SCOPE, 'target', 'v1', observed_at=17)
    assert remote_e['status'] == 'ok'
    assert judge_task(required, [file_e, test_e, remote_e], (0, 30), claim=c)['completion']['verdict'] == 'supported'
    artifact.unlink()
    missing = observe_artifact(artifact, digest, SCOPE, 'target', 'v1', observed_at=18)
    assert missing['status'] == 'error'
    assert judge_task(required, [missing, test_e, remote_e], (0, 30), claim=c)['completion']['verdict'] != 'supported'
    artifact.write_bytes(b'wrong bytes')
    stale = run_bound_test(['python3', '-c', 'pass'], artifact, digest, SCOPE,
                           'target', 'v1', observed_at=19)
    assert stale['status'] == 'error'
    assert judge_task(required, [file_e, stale, remote_e], (0, 30), claim=c)['completion']['verdict'] != 'supported'
    artifact.write_bytes(b'exact bytes\x00')
    repaired = run_bound_test(['python3', '-c',
                               'import hashlib,sys; assert hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()==sys.argv[2]',
                               str(artifact), digest], artifact, digest, SCOPE, 'target', 'v1', observed_at=20)
    assert judge_task(required, [file_e, stale, repaired, remote_e], (0, 30), claim=c)['completion']['verdict'] == 'supported'
