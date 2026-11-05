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


def test_read_only_backfill_and_png(historical_db, tmp_path):
    before = historical_db.stat().st_mtime_ns
    receipt = plugin.receipt_from_state(historical_db)
    output = plugin.render_png(receipt, tmp_path / 'receipt.png')
    assert output.read_bytes().startswith(b'\x89PNG\r\n')
    assert historical_db.stat().st_mtime_ns == before
    assert 'VERIFIED    1 / 1' in plugin.render_text(receipt)
    assert receipt['tokens'] is None and receipt['cost_usd'] is None


def test_claim_extraction_is_conservative():
    from receipts_test_package.receipt_core import extract_claims, match_claims
    assert extract_claims('I did not push to main. Please run pytest. `tests pass`') == []
    assert match_claims('The subagent pushed to main.', [])[0]['verdict'] == 'not observed'
    assert match_claims('Pushed to main.', [])[0]['verdict'] == '✗'
    assert match_claims('All tests pass.', [{'tool': 'terminal', 'action': 'test', 'status': 'error'}])[0]['verdict'] == '✗'


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
