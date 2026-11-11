"""Synthetic plumbing tests; these do not measure model effectiveness."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from session_classifier import (BOOL_QUESTIONS, TYPES, auto_fields, build_digest,
                                classify_session, combine_verdict, deterministic)


def messages(command='pytest -q', output='2 passed', code=0):
    return [dict(role='assistant', tool_calls=json.dumps([dict(id='a', function=dict(name='terminal', arguments=json.dumps(dict(command=command))))]), timestamp=1),
            dict(role='tool', tool_call_id='a', content=json.dumps(dict(exit_code=code, output=output)), timestamp=2),
            dict(role='assistant', content='All tests passed. Done.', timestamp=3)]


class Client:
    def __init__(self, choice='yes'):
        self.pick = choice
        self.state = None

    @staticmethod
    def choice(instructions, criteria):
        return dict(type='choice', instructions=instructions, criteria=criteria)

    def ask(self, state, questions, **kwargs):
        self.state = state
        answers = {k: dict(type='choice', choice=('coding' if k == 'session_type' else 'supported' if k == 'verdict' else self.pick), confidence=0.99) for k in questions}
        return dict(answers=answers, usage={'input_tokens': 3}, latency_ms=1)


class Privacy:
    def __init__(self):
        self.called = False

    def redact(self, text, limit):
        self.called = True
        return text.replace('CANARY', '[redacted]')[:limit]


def test_disabled_never_imports_or_sends(monkeypatch):
    import session_classifier
    monkeypatch.setattr(session_classifier, '_load_jev', lambda: pytest.fail('imported'))
    result = classify_session(messages())
    assert result['jev'] == dict(ok=False, reason='disabled')
    assert result['categories']['tests_run']


def test_redaction_before_send_and_assignment_values():
    client, privacy = Client(), Privacy()
    data = messages('pytest -q # password: hunter2')
    data[-1]['content'] += ' CANARY'
    result = classify_session(data, enabled=True, client=client, privacy=privacy)
    assert result['jev']['ok'] and privacy.called
    assert 'CANARY' not in client.state and 'hunter2' not in client.state
    assert '[redacted]' in client.state


def test_real_privacy_shape_redaction():
    # Optional installed-client integration; no network calls.
    path = Path('/Users/djm/.hermes/plugins/hermes-jev/jevkit/privacy.py')
    if not path.is_file():
        pytest.skip('optional Jev not installed')
    spec = importlib.util.spec_from_file_location('real_jev_privacy', path)
    privacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(privacy)
    client = Client()
    result = classify_session(messages('pytest # api_key=short-value\nsk-abcdefghijklmnopqrstuv user@example.com'), enabled=True, client=client, privacy=privacy)
    assert result['jev']['ok']
    for value in ('short-value', 'sk-abcdefghijklmnopqrstuv', 'user@example.com'):
        assert value not in client.state


def test_completion_args_reserved_in_digest():
    data = messages()
    data.insert(0, dict(role='assistant', tool_calls=json.dumps([dict(id='done', function=dict(name='kanban_complete', arguments=json.dumps(dict(summary='DONE_CANARY', metadata={'tests_run': True}))))])))
    assert 'DONE_CANARY' in build_digest(data)
    assert deterministic(data)['claims_done']


@pytest.mark.parametrize('exc', [ImportError, TimeoutError, ValueError, RuntimeError])
def test_fallback(exc):
    client = Client()
    def fail(*a, **kw):
        raise exc('PRIVATE_DO_NOT_ECHO')
    client.ask = fail
    expected = deterministic(messages())
    result = classify_session(messages(), enabled=True, client=client, privacy=Privacy())
    assert result['categories'] == expected
    assert not result['jev']['ok']
    assert 'PRIVATE_DO_NOT_ECHO' not in json.dumps(result)


def test_malformed_answers_fail_back_atomically():
    client = Client()
    client.ask = lambda *a, **kw: {'answers': {}}
    result = classify_session(messages(), enabled=True, client=client, privacy=Privacy())
    assert not result['jev']['ok']
    assert result['categories'] == result['deterministic']


@pytest.mark.parametrize('original', ['supported', 'contradicted', 'unverified', 'not_evaluable'])
@pytest.mark.parametrize('proposal', ['supported', 'contradicted', 'unverified', 'not_evaluable'])
def test_no_override(original, proposal):
    assert combine_verdict(original, proposal) == original


def test_model_no_cannot_erase_deterministic_tests():
    result = classify_session(messages(), enabled=True, client=Client('no'), privacy=Privacy())
    assert result['categories']['tests_run']


def test_auto_chat_mandatory_only():
    fields = auto_fields(dict(session_type='chat'))
    assert 'verdict' in fields and 'evidence_locators' in fields
    assert 'tests' not in fields and 'cost' not in fields and 'push_check' not in fields


def test_auto_reads_external_rules(tmp_path):
    p = tmp_path/'auto.json'
    p.write_text(json.dumps(dict(required_fields=['verdict'], rules=[dict(when_any={'tests_run': True}, fields=['custom_test'])])))
    assert auto_fields({'tests_run': True}, p) == ['verdict', 'custom_test']


@pytest.mark.parametrize('key,field', [('tests_run','tests'), ('pushed_or_published','push_check'), ('files_changed','files_touched'), ('cost_heavy','cost'), ('long_running','duration'), ('external_side_effect','external_readback')])
def test_auto_gates(key, field):
    assert field in auto_fields({key: True})
    assert field not in auto_fields({key: False})


def test_test_success_requires_outcome_text_not_only_zero():
    assert not deterministic(messages(output=''))['tests_passed']
    assert not deterministic(messages(output='2 passed', code=1))['tests_passed']
    assert deterministic(messages())['tests_passed']


def test_cost_and_duration_thresholds():
    assert deterministic([], {'input_tokens':100000, 'wall_seconds':600})['cost_heavy']
    assert deterministic([], {'wall_seconds':600})['long_running']
    assert not deterministic([], {'estimated_cost_usd':.99})['cost_heavy']


def test_inventory_and_reconciliation_fixture(tmp_path):
    spec = importlib.util.spec_from_file_location('fleet_scan_under_test', ROOT/'scripts'/'jev_fleet.py')
    fleet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet)
    inventory, summarize = fleet.inventory, fleet.summarize
    import sqlite3
    root = tmp_path/'home'
    root.mkdir()
    db = root/'state.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE messages(session_id TEXT, timestamp REAL)')
        conn.executemany('INSERT INTO messages VALUES (?,?)', [('a',10), ('a',11), ('b',20), ('old',1)])
    before = db.read_bytes()
    frames = inventory(root, 10, 21)
    assert frames[0]['sessions'] == ['a', 'b'] and db.read_bytes() == before
    manifest = dict(start=10, end=21, sources=frames)
    report = summarize([dict(profile='default', session='a', ok=False), dict(profile='default', session='b', ok=False)], manifest)
    assert report['profiles']['default']['reconciled']
    assert report['failed'] == 2


def test_response_allowlist_drops_echo_fields():
    client = Client()
    ask = client.ask
    def echo(*args, **kwargs):
        reply = ask(*args, **kwargs)
        reply['answers']['verdict']['echo'] = 'PRIVATE_CANARY'
        reply['answers']['extra'] = {'text': 'PRIVATE_CANARY'}
        reply['usage']['echo'] = 'PRIVATE_CANARY'
        reply['untrusted'] = 'PRIVATE_CANARY'
        return reply
    client.ask = echo
    result = classify_session(messages(), enabled=True, client=client, privacy=Privacy())
    assert result['jev']['ok']
    assert 'PRIVATE_CANARY' not in json.dumps(result)


def test_bad_latency_falls_back_before_category_mutation():
    client = Client()
    ask = client.ask
    def bad(*args, **kwargs):
        reply = ask(*args, **kwargs)
        reply['latency_ms'] = 'PRIVATE_CANARY'
        return reply
    client.ask = bad
    result = classify_session(messages(), enabled=True, client=client, privacy=Privacy())
    assert not result['jev']['ok'] and result['categories'] == result['deterministic']


def test_model_type_and_error_only_add_advisory_sections():
    client = Client()
    ask = client.ask
    def research(*args, **kwargs):
        reply = ask(*args, **kwargs)
        reply['answers']['session_type']['choice'] = 'research'
        return reply
    client.ask = research
    result = classify_session(messages(), enabled=True, client=client, privacy=Privacy())
    fields = auto_fields(result['categories'])
    assert result['categories']['session_type'] == 'coding'
    assert result['categories']['jev_session_type'] == 'research'
    assert 'tests' in fields and 'sources' in fields and 'errors' in fields


@pytest.mark.parametrize('code,passed', [(0, True), (1, False)])
def test_wrapped_tool_results(code, passed):
    data = messages()
    data[1]['content'] = json.dumps({'data': {'exit_code': code, 'output': '2 passed'}})
    assert deterministic(data)['tests_passed'] is passed


def test_scoped_claim_and_observations_are_in_redacted_digest():
    client, privacy = Client(), Privacy()
    meta = dict(claim=dict(kind='tests', span_start=0, span_end=16,
                           scope={'final_message_id': 'final'}),
                evidence=[{'status': 'error', 'note': 'CANARY'}])
    data = messages()
    data[-1]['id'] = 'final'
    result = classify_session(data, meta, enabled=True, client=client, privacy=privacy)
    assert result['jev']['ok']
    assert 'JUDGE ONLY THIS CLAIM' in client.state and 'status' in client.state
    assert 'CANARY' not in client.state and '[redacted]' in client.state


def test_sql_readonly_rejects_writes_and_missing_wal_shm(tmp_path):
    spec = importlib.util.spec_from_file_location('fleet_ro_test', ROOT/'scripts'/'jev_fleet.py')
    fleet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet)
    import sqlite3
    db = tmp_path/'state.db'
    sqlite3.connect(db).close()
    with fleet.connect(db) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute('CREATE TABLE forbidden(x)')
    db.with_name('state.db-wal').touch()
    with pytest.raises(ValueError, match='wal_without_shm'):
        fleet.connect(db)
