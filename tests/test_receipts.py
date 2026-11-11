from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('receipts_test_package', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


@pytest.fixture
def historical_db(tmp_path):
    path = tmp_path / 'state.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
          content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL);
          CREATE TABLE sessions(id TEXT PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL);
          INSERT INTO sessions VALUES ('s1', 100, 200, 0.003);''')
        conn.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', [
            (1, 's1', 'user', 'Test then push', None, None, None, 10),
            (2, 's1', 'assistant', '', None, json.dumps([{'id': 'c1', 'function': {'name': 'terminal',
               'arguments': json.dumps({'command': 'pytest -q'})}}]), None, 11),
            (3, 's1', 'tool', json.dumps({'output': '2 failed', 'exit_code': 1}), 'c1', None, 'terminal', 12),
            (4, 's1', 'assistant', 'All tests pass. Pushed to main.', None, None, None, 13),
            (5, 's1', 'user', 'Now fix the tests', None, None, None, 14),
            (6, 's1', 'assistant', '', None, json.dumps([{'id': 'c2', 'function': {'name': 'terminal',
               'arguments': json.dumps({'command': 'pytest -q'})}}]), None, 15),
            (7, 's1', 'tool', json.dumps({'exit_code': 0, 'output': '2 passed'}), 'c2', None, 'terminal', 16),
            (8, 's1', 'assistant', 'All tests pass.', None, None, None, 17),
        ])
    return path


def test_retroactive_turn_boundaries_and_failures(historical_db):
    old = plugin.receipt_from_state(historical_db, turn_id=4)
    assert [c['verdict'] for c in old['claims']] == ['✗', '✗']
    assert len(old['rows']) == 1 and old['rows'][0]['status'] == 'error'
    latest = plugin.receipt_from_state(historical_db)
    assert latest['final_id'] == 8 and len(latest['rows']) == 1
    assert latest['claims'][0]['verdict'] == '✓'
    assert latest['hash'] != old['hash']
    assert plugin.verify_hash(historical_db, old['hash'])['final_id'] == 4
    with pytest.raises(ValueError, match='full'):
        plugin.verify_hash(historical_db, old['hash'][:8])


def test_long_initial_inference_counts_toward_auto_duration(historical_db):
    with sqlite3.connect(historical_db) as conn:
        conn.execute('DELETE FROM messages WHERE id NOT IN (5,6,8)')
        conn.execute('UPDATE messages SET timestamp=0 WHERE id=5')
        conn.execute("UPDATE messages SET timestamp=650, tool_calls=NULL, content='Thinking.' WHERE id=6")
        conn.execute("UPDATE messages SET timestamp=700, content='Hello.' WHERE id=8")
    receipt = plugin.receipt_from_state(historical_db, turn_id=8)
    assert receipt['wall_seconds'] == 700
    assert receipt['classification']['categories']['session_type'] == 'chat'
    assert receipt['classification']['categories']['long_running'] is True
    assert 'duration' in receipt['auto_fields']


def test_read_only_backfill_and_png(historical_db, tmp_path):
    before = historical_db.stat().st_mtime_ns
    receipt = plugin.receipt_from_state(historical_db)
    output = plugin.render_png(receipt, tmp_path / 'receipt.png')
    assert output.read_bytes().startswith(b'\x89PNG\r\n')
    assert historical_db.stat().st_mtime_ns == before
    assert 'VERIFIED    1 / 1' in plugin.render_text(receipt)
    assert receipt['tokens'] is None and receipt['cost_usd'] is None


def test_gateway_slash_media_is_deliverable(historical_db, monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter
    from receipts_test_package.v2 import fields
    monkeypatch.setenv('HERMES_HOME', str(historical_db.parent))
    monkeypatch.setenv('HERMES_RECEIPTS_CONFIG_DIR', str(historical_db.parent / 'cfg'))
    fields.reload()
    reply = plugin._slash('8')
    media, cleaned = BasePlatformAdapter.extract_media(reply)
    assert len(media) == 1 and Path(media[0][0]).is_file()
    # `/receipt` now renders through the on_demand trigger, so the reply is the same
    # public receipt the printer prints: the media contract holds, and the verdict
    # line is the one immutable line every receipt carries.
    assert media[0][0].endswith('.png')
    assert 'COMPLETION: SUPPORTED \u2713' in cleaned
    assert 'tests pass' in cleaned and 'CLAIMS DETECTED' in cleaned
    assert 'MEDIA:' not in cleaned


def test_live_hook_overlay_preserves_hash_and_duration(historical_db, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(historical_db.parent))
    plugin._post_tool_call(tool_name='terminal', args={'command': 'pytest -q'},
                           status='ok', session_id='s1', turn_id='5', tool_call_id='c2', duration_ms=348)
    ledger_path = historical_db.parent / 'plugin-data' / 'receipts' / 'data.db'
    receipt = plugin.receipt_from_state(historical_db, turn_id=8, ledger_path=ledger_path)
    assert receipt['rows'][0]['duration_ms'] == 348
    assert plugin.verify_hash(historical_db, receipt['hash'], ledger_path=ledger_path)['final_id'] == 8


def test_dashboard_api_serves_authenticated_router_payload(historical_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv('HERMES_HOME', str(historical_db.parent))
    spec = importlib.util.spec_from_file_location('receipts_test_dashboard', ROOT / 'dashboard' / 'plugin_api.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.state.auth_required = True
    @app.middleware('http')
    async def simulated_gateway_owner(request, call_next):
        if request.headers.get('x-test-owner') == 'verified':
            request.state.hosted_http_authenticated = True
        return await call_next(request)
    app.include_router(module.router)
    client = TestClient(app)
    assert client.get('/last?session_id=s1').status_code == 401
    payload = client.get('/last?session_id=s1', headers={'x-test-owner': 'verified'})
    assert payload.status_code == 200
    data = payload.json()
    assert data['png'].startswith('data:image/png;base64,')
    assert data['final_id'] == 8 and 'VERIFIED    1 / 1' in data['text']
    assert client.get('/last?session_id=missing', headers={'x-test-owner': 'verified'}).status_code == 404


def test_claims_require_matching_target_and_success():
    from receipts_test_package.receipt_core import match_claims
    rows = [{'tool': 'write_file', 'label': 'other.py', 'action': 'write', 'status': 'ok', 'call_id': 'a'},
            {'tool': 'terminal', 'label': 'test command', 'action': 'test', 'status': 'error', 'call_id': 'b'}]
    claims = match_claims('Wrote example.py. 4 tests pass. Ran pytest.', rows)
    assert [c['verdict'] for c in claims] == ['✗', '✗', '✗']
    rows[0]['label'] = 'example.py'
    rows[1]['status'] = 'ok'
    assert [c['verdict'] for c in match_claims('Wrote example.py. 4 tests pass. Ran pytest.', rows)] == ['✓', '✓', '✓']


def test_claim_extraction_is_conservative():
    from receipts_test_package.receipt_core import extract_claims, match_claims
    assert extract_claims('I did not push to main. Please run pytest. `tests pass`') == []
    assert match_claims('The subagent pushed to main.', [])[0]['verdict'] == 'not observed'
    assert match_claims('Pushed to main.', [])[0]['verdict'] == '✗'
    assert match_claims('All tests pass.', [{'tool': 'terminal', 'action': 'test', 'status': 'error'}])[0]['verdict'] == '✗'


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
    from receipts_test_package.receipt_core import extract_claims, match_claims
    assert extract_claims(answer) == []
    assert match_claims(answer, []) == []


def test_disclaimer_does_not_hide_later_local_action():
    from receipts_test_package.receipt_core import extract_claims
    assert [c['kind'] for c in extract_claims('Nothing was deployed, but I committed changes.')] == ['commit']


def test_optional_gate_default_observe_and_scoped_block(historical_db, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(historical_db.parent))
    monkeypatch.delenv('HERMES_RECEIPTS_GATE', raising=False)
    assert plugin._gate(tool_name='kanban_complete', session_id='s1') is None
    monkeypatch.setenv('HERMES_RECEIPTS_GATE', 'block')
    assert plugin._gate(tool_name='kanban_complete', session_id='') is None
    assert plugin._gate(tool_name='write_file', session_id='s1') is None
    # The latest final answer is backed; old failing claims cannot block it.
    assert plugin._gate(tool_name='kanban_complete', session_id='s1') is None
    with sqlite3.connect(historical_db) as conn:
        conn.execute('DELETE FROM messages WHERE id >= 5')
    blocked = plugin._gate(tool_name='kanban_complete', session_id='s1')
    assert blocked['action'] == 'block' and 'review' in blocked['reason']


def test_hook_ledger_only_digests(monkeypatch, tmp_path):
    from receipts_test_package import receipt_core
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    plugin._post_tool_call(tool_name='terminal', args={'command': 'pytest -q', 'password': 'PRIVATE'},
                           status='ok', session_id='s', turn_id='t', tool_call_id='c1', duration_ms=27)
    db = sqlite3.connect(tmp_path / 'plugin-data' / 'receipts' / 'data.db')
    row = db.execute('SELECT tool,action,status,duration_ms,args_sha256 FROM ledger').fetchone()
    assert row[:4] == ('terminal', 'test', 'ok', 27)
    assert len(row[4]) == 64 and 'PRIVATE' not in repr(row)
    assert receipt_core.digest({'b': 1, 'a': 2}) == receipt_core.digest({'a': 2, 'b': 1})


def test_no_printer_fails_cleanly():
    from receipts_test_package.printer import print_receipt
    ok, message = print_receipt('test', 0xffff, 0xffff)
    assert not ok and ('not installed' in message or 'unavailable' in message)
