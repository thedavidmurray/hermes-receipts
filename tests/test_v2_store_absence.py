"""Extra regressions: unknown joins are never proof and no arbitrary data is stored."""
import sqlite3

from v2.contracts import claim_key, turn_key
from v2.store import query_rollup, upsert_run


def test_rollup_missing_finding_is_visible_not_contradiction(tmp_path):
    scope = dict(source_id='s', profile='p', session_id='session', user_message_id='1', final_message_id='2')
    turn = dict(turn_key=turn_key(scope), source_id='s', profile='p', session_id='session',
                user_id='1', final_id='2', final_time=100., model='unknown',
                model_certainty='unknown', source='unknown', project_key=None,
                answer='SECRET ANSWER', messages=[{'command':'PRIVATE COMMAND'}])
    claim = dict(claim_id=claim_key(scope,'done',0,4,'d1'),scope=scope,kind='done',span_start=0,
                 span_end=4,target_key=None,task_key=None,detector_version='d1',raw='SECRET CLAIM')
    db = tmp_path / 'report.db'
    upsert_run(db, {'turns':[turn], 'claims':[claim]})
    results = query_rollup(db, {'start':99,'end':101,'detector_version':'d1','matcher_version':'m1'}, [])
    assert results[0]['done']['contradicted'] == 0
    assert results[0]['done']['not_evaluable'] == 1
    assert results[0]['done']['denominator'] == 0
    assert 'SECRET' not in db.read_bytes().decode('latin1')
    assert 'PRIVATE COMMAND' not in db.read_bytes().decode('latin1')
