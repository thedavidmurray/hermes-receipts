"""Lane A: fixture-only source safety, identities, rollups and partial coverage."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from v2.contracts import claim_key, fingerprint_target, turn_key
from v2.fleet import iter_turns
from v2.store import query_coverage, query_rollup, upsert_run


def test_local_keyed_target_not_raw_or_cross_key():
    a = fingerprint_target('/private/secret', b'a' * 32)
    assert len(a) == 64 and a != fingerprint_target('/private/secret', b'b' * 32)
    with pytest.raises(ValueError):
        fingerprint_target('secret', b'weak')


def fixture(root: Path, *, profile: str, model_column: bool = True, wal: bool = False):
    root.mkdir(parents=True)
    db = root / 'state.db'
    model = ', model TEXT' if model_column else ''
    with sqlite3.connect(db) as conn:
        conn.executescript(f'''CREATE TABLE sessions(id TEXT, profile_name TEXT, source TEXT, git_repo_root TEXT{model});
            CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, content TEXT,
                tool_calls TEXT, tool_call_id TEXT, timestamp REAL, is_display_only INTEGER DEFAULT 0);''')
        conn.execute('INSERT INTO sessions VALUES (?,?,?,?,?)' if model_column else 'INSERT INTO sessions VALUES (?,?,?,?)',
                     ('same', profile, 'gateway', '/private/project', 'model-A') if model_column else ('same', profile, 'gateway', '/private/project'))
        conn.executemany('INSERT INTO messages(id,session_id,role,content,timestamp) VALUES (?,?,?,?,?)', [
            (1, 'same', 'user', 'do it', 100.0),
            (2, 'same', 'assistant', 'Done', 101.0),
            (3, 'same', 'user', 'again', 102.0),
            (4, 'same', 'assistant', 'Done again', 103.0),
            (5, 'same', 'assistant', 'display-only', 104.0),
        ])
        conn.execute('UPDATE messages SET is_display_only=1 WHERE id=5')
        if wal:
            conn.execute('PRAGMA journal_mode=WAL')
    return db


def hashes(db):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in db.parent.glob('state.db*')}


def test_fleet_collision_boundary_unknown_and_no_source_writes(tmp_path):
    one = fixture(tmp_path / 'one', profile='alpha', wal=True)
    two = fixture(tmp_path / 'two', profile='beta', model_column=False)
    before = {p: hashes(p) for p in (one, two)}
    turns = list(iter_turns([tmp_path / 'one', tmp_path / 'two'], 101, 103, {'max_finals': 10}))
    assert {x['profile'] for x in turns if x['record_type'] == 'turn'} == {'alpha', 'beta'}
    assert len([x for x in turns if x['record_type'] == 'turn']) == 2
    assert all(x['final_time'] == 101 for x in turns if x['record_type'] == 'turn')
    assert len({x['turn_key'] for x in turns if x['record_type'] == 'turn'}) == 2
    assert all(x['model'] == 'unknown' and x['model_certainty'] == 'unknown'
               for x in turns if x.get('profile') == 'beta')
    assert all(x['coverage_reason'] == 'complete' for x in turns if x['record_type'] == 'source')
    assert before == {p: hashes(p) for p in (one, two)}


def test_fleet_multiplexed_profiles_partial_limit_and_no_config_reads(tmp_path):
    db = fixture(tmp_path / 'root', profile='alpha')
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', ('other', 'beta', 'remote', None, None))
        conn.executemany('INSERT INTO messages(id,session_id,role,content,timestamp) VALUES (?,?,?,?,?)', [
            (6, 'other', 'user', 'start', 105), (7, 'other', 'assistant', 'Done', 106),
        ])
    (tmp_path / 'root' / 'config.yaml').write_text('TOPSECRET')
    turns = list(iter_turns([tmp_path / 'root'], 100, 200, {'max_finals': 1, 'page_size': 1}))
    assert len([x for x in turns if x['record_type'] == 'turn']) == 1
    assert turns[-1]['record_type'] == 'source' and turns[-1]['coverage_reason'] == 'max_finals'
    assert turns[-1]['cutoff'] is not None and turns[-1]['cursor'] == '2'
    report = tmp_path / 'partial.db'
    upsert_run(report, {'sources': [turns[-1]]})
    assert query_coverage(report)[0]['coverage_reason'] == 'max_finals;cursor=2'
    all_turns = list(iter_turns([tmp_path / 'root'], 100, 200, {'max_finals': 10, 'page_size': 1}))
    assert {t['profile'] for t in all_turns if t['record_type'] == 'turn'} == {'alpha', 'beta'}
    assert 'TOPSECRET' not in repr(all_turns)


def test_missing_schema_and_opt_in_constraints(tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    with sqlite3.connect(empty / 'state.db'):
        pass
    items = list(iter_turns([empty], 0, 10, {}))
    assert items[0]['record_type'] == 'source' and items[0]['coverage_reason'] == 'missing_schema'
    with pytest.raises(ValueError):
        list(iter_turns([], 0, 10, {}))
    with pytest.raises(ValueError):
        list(iter_turns([empty], 10, 10, {}))
    with pytest.raises(ValueError):
        list(iter_turns([empty], 0, 10, {'max_finals': 0}))


def test_active_quiescent_wal_shm_and_missing_shm(tmp_path):
    db = fixture(tmp_path / 'root', profile='alpha')
    writer = sqlite3.connect(db)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('UPDATE sessions SET source=source WHERE id=?', ('same',))
        writer.commit()
        assert db.with_name('state.db-wal').is_file()
        assert db.with_name('state.db-shm').is_file()
        before = hashes(db)
        events = list(iter_turns([db.parent], 100, 200, {'max_finals': 10}))
        assert events[-1]['coverage_reason'] == 'complete'
        assert hashes(db) == before
        assert all(e['record_type'] != 'turn' or e['source'] == 'gateway' for e in events)
    finally:
        writer.close()
    # A WAL with no SHM must fail closed before opening the DB; the fixture owns
    # this directory, so deliberately remove its SHM to exercise the safety gate.
    writer = sqlite3.connect(db)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('INSERT INTO messages(id,session_id,role,content,timestamp) VALUES (?,?,?,?,?)',
                       (8, 'same', 'user', 'later', 108))
        writer.commit()
        db.with_name('state.db-shm').unlink()
        before = hashes(db)
        events = list(iter_turns([db.parent], 100, 200, {}))
        assert events == [dict(record_type='source', source_id=events[0]['source_id'],
                               schema_version=2, availability='unavailable', cutoff=None,
                               coverage_reason='wal_without_shm')]
        assert hashes(db) == before
    finally:
        writer.close()


def test_store_rejects_raw_target_keys(tmp_path):
    from v2.store import upsert_run
    with pytest.raises(ValueError, match='target_key'):
        upsert_run(tmp_path / 'report.db', {'requirements': [{
            'task_key': 'task', 'requirement_id': 'r', 'required': True,
            'evidence_predicate': 'artifact_digest', 'target_key': '/private/secret',
            'artifact_version': None, 'public_label': 'Artifact bytes match',
        }]})


def test_store_versions_idempotence_and_group_denominators(tmp_path):
    db = tmp_path / 'report.db'
    scope = {'source_id': 'source', 'profile': 'alpha', 'session_id': 'same',
             'user_message_id': '1', 'final_message_id': '2'}
    tk = turn_key(scope)
    cid = claim_key(scope, 'done', 0, 4, 'd1')
    source = {'record_type': 'source', 'source_id': 'source', 'schema_version': 2,
              'availability': 'available', 'cutoff': 102., 'coverage_reason': 'complete'}
    turn = {'record_type': 'turn', 'turn_key': tk, 'source_id': 'source', 'profile': 'alpha',
            'session_id': 'same', 'user_id': '1', 'final_id': '2', 'final_time': 101.,
            'model': 'unknown', 'model_certainty': 'unknown', 'source': 'unknown',
            'project_key': 'project'}
    claim = {'claim_id': cid, 'scope': scope, 'kind': 'done', 'span_start': 0,
             'span_end': 4, 'target_key': None, 'task_key': 'task', 'project_key': 'project',
             'detector_version': 'd1'}
    finding = {'claim_id': cid, 'matcher_version': 'm1', 'verdict': 'unverified',
               'reason_code': 'no_evidence', 'evidence_ids': []}
    entities = {'sources': [source], 'turns': [turn], 'claims': [claim], 'findings': [finding],
                'task_links': [{'task_key': 'task', 'claim_id': cid, 'provenance': 'manifest'}],
                'requirements': [{'task_key': 'task', 'requirement_id': 'req', 'required': True,
                                  'evidence_predicate': 'artifact_digest', 'target_key': None,
                                  'artifact_version': None, 'public_label': 'Artifact bytes match'}]}
    upsert_run(db, entities)
    upsert_run(db, entities)
    assert query_coverage(db)[0]['coverage_reason'] == 'complete'
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM v2_claims').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM v2_task_links').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM v2_requirements').fetchone()[0] == 1
        assert not any('content' in row[1] or 'command' in row[1]
                       for row in conn.execute('PRAGMA table_info(v2_turns)'))
    rows = query_rollup(db, {'start': 100, 'end': 102, 'detector_version': 'd1',
                             'matcher_version': 'm1'}, ['profile', 'model', 'source', 'project_key'])
    assert rows[0]['profile'] == 'alpha' and rows[0]['model'] == 'unknown'
    assert rows[0]['done']['unverified'] == 1 and rows[0]['action']['supported'] == 0
    assert rows[0]['done']['denominator'] == 1 and rows[0]['tasks'] == 1
    assert query_rollup(db, {'start': 101, 'end': 101.5, 'detector_version': 'd1', 'matcher_version': 'm1'}, [])
    assert query_rollup(db, {'start': 102, 'end': 110, 'detector_version': 'd1', 'matcher_version': 'm1'}, []) == []
    with pytest.raises(ValueError):
        query_rollup(db, {'start': 0, 'end': 10, 'detector_version': 'd1', 'matcher_version': 'm1'}, ['evil; DROP TABLE v2_turns'])
    with pytest.raises(ValueError):
        query_rollup(db, {'start': 0, 'end': 10}, [])
