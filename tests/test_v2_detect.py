"""Synthetic detector fixtures only; no historical/private answer text persisted."""
from __future__ import annotations

import pytest

from v2.contracts import Scope
from v2.detect import DETECTOR_VERSION, extract_claims

SCOPE = Scope(source_id='source', profile='kilo', session_id='session',
              user_message_id='user', final_message_id='final')


def detect(answer, links=None):
    return extract_claims(answer, SCOPE, links or {})


def test_actions_passive_bullets_and_original_offsets():
    answer = '🔥 Done: task complete.\n- The tests were run and passed.\n- Changes have been committed and pushed.\n- Email was sent; site deployed.\n- report.py was created; build executed.'
    claims = detect(answer)
    kinds = [c['kind'] for c in claims]
    assert set(kinds) == {'done', 'tests', 'run', 'commit', 'push', 'send', 'deploy', 'write'}
    assert all(answer[c['span_start']:c['span_end']].strip() for c in claims)
    assert all(c['claim_id'] and c['detector_version'] == DETECTOR_VERSION for c in claims)
    assert [c['claim_id'] for c in claims] == [c['claim_id'] for c in detect(answer)]
    assert len({c['claim_id'] for c in claims}) == len(claims)


def test_clause_local_negation_does_not_suppress_next_action():
    answer = 'I did not push, but committed the patch. Tests failed, then I fixed them and tests passed. Never deployed; sent the mail.'
    kinds = [c['kind'] for c in detect(answer)]
    assert kinds.count('push') == 0
    assert kinds.count('commit') == 1
    assert kinds.count('tests') == 1
    assert kinds.count('send') == 1
    assert kinds.count('deploy') == 0


@pytest.mark.parametrize('answer', [
    'Please push the branch and run the tests.',
    'I will push and deploy tomorrow.',
    'To deploy: run the script, then send an email.',
    'If tests pass, commit and push.',
    'The subagent pushed and CI tests passed.',
    'According to the status report, the other agent completed the task.',
    '```bash\ngit push\ntests pass\n```\n> Committed the patch.',
    'The plan: commit, push and deploy.',
    '"I pushed the branch and finished the task."',
    '`Tests passed` and `pushed to main`',
])
def test_exclusions(answer):
    assert detect(answer) == []


def test_paraphrased_plural_objects_and_short_report_bullets():
    answer = '🔥 Created two Kanban cards.\n- Added tests and updated files.\n- Verified the new script.\n- Shipped the release to prod.'
    kinds = [c['kind'] for c in detect(answer)]
    assert kinds.count('write') >= 3
    assert 'deploy' in kinds


def test_report_nouns_detect_concrete_creation_without_title_linkage():
    claims = detect('Built a dashboard and created a CLI integration. Added a vault entry.')
    assert [c['kind'] for c in claims] == ['write', 'write', 'write']
    assert all(c['task_key'] is None for c in claims)


def test_external_report_does_not_suppress_local_followup():
    assert [c['kind'] for c in detect('The subagent pushed and I committed the patch.')] == ['commit']
    assert [c['kind'] for c in detect('CI tests passed, but I pushed the branch.')] == ['push']


@pytest.mark.parametrize('answer', [
    'Nothing was deployed or reloaded.',
    'No message or alert was sent, and the queue remains untouched.',
    'Nothing was published.',
    'Nothing was pushed to main.',
    'The post you sent after it went live was useful.',
    'None of the files were written.',
    'Zero messages were sent.',
    '0 new souls were pushed to R2.',
    'Neither service has ever been deployed.',
])
def test_disclaimers_and_user_actions_are_not_agent_claims(answer):
    assert detect(answer) == []


def test_disclaimer_does_not_hide_later_local_action():
    assert [c['kind'] for c in detect('Nothing was deployed, but I committed changes.')] == ['commit']


def test_run_requires_named_operation_not_idiom():
    assert detect('We ran out of time. The process ran at noon.') == []
    assert [c['kind'] for c in detect('Ran the build.')] == ['run']


def test_quote_with_real_claim_retains_full_text_offsets():
    answer = 'Example: "Tests passed."\nI committed `report.py` and pushed to main.'
    claims = detect(answer)
    assert [c['kind'] for c in claims] == ['commit', 'push']
    assert all(answer[c['span_start']:c['span_end']].strip() for c in claims)
    assert all(c['span_start'] >= answer.index('I committed') for c in claims)


def test_done_requires_completed_outcome_not_only_progress_or_thanks():
    assert [c['kind'] for c in detect('Task completed. All required work is finished.')] == ['done', 'done']
    assert detect('Working on it; task is nearly done, will finish later.') == []
    assert detect('Done planning; still need to implement and test.') == []


def test_standalone_done_is_first_class_claim():
    answer = '🔥 Done.\nThe task is complete.'
    assert [c['kind'] for c in detect(answer)] == ['done', 'done']
    assert detect('Done for now; remaining work pending.') == []


def test_explicit_links_only_no_title_guessing():
    answer = 'I completed the task and pushed the commit.'
    claims = detect(answer, {'task_key': 'linked-task', 'project_key': 'linked-project', 'target_key': 'fingerprint'})
    assert all(c['task_key'] == 'linked-task' and c['project_key'] == 'linked-project' and c['target_key'] == 'fingerprint' for c in claims)
    assert all(c['task_key'] is None for c in detect('Task complete: linked-task'))
    assert all(c['task_key'] is None for c in detect(answer, {'task_title': 'linked-task'}))


def test_empty_and_input_integrity():
    assert detect('') == []
    answer = 'Wrote example.py.'
    assert detect(answer)[0]['span_start'] == 0
    assert answer == 'Wrote example.py.'
