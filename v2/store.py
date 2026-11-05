"""Private normalized v2 store. No source DB or v1 ledger is ever modified."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .contracts import SCHEMA_VERSION, claim_key, evidence_key, turn_key

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS v2_sources(
 source_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, availability TEXT NOT NULL,
 cutoff REAL, coverage_reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v2_turns(
 turn_key TEXT PRIMARY KEY, source_id TEXT NOT NULL, profile TEXT NOT NULL,
 session_id TEXT NOT NULL, user_id TEXT NOT NULL, final_id TEXT NOT NULL,
 final_time REAL NOT NULL, model TEXT NOT NULL, model_certainty TEXT NOT NULL,
 source TEXT NOT NULL, project_key TEXT);
CREATE TABLE IF NOT EXISTS v2_claims(
 claim_id TEXT PRIMARY KEY, turn_key TEXT NOT NULL, kind TEXT NOT NULL,
 span_start INTEGER NOT NULL, span_end INTEGER NOT NULL, target_key TEXT,
 task_key TEXT, detector_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v2_evidence(
 evidence_id TEXT PRIMARY KEY, turn_key TEXT NOT NULL, call_id TEXT NOT NULL,
 action TEXT NOT NULL, target_key TEXT, artifact_version TEXT,
 observed_at REAL NOT NULL, level TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v2_findings(
 claim_id TEXT NOT NULL, matcher_version TEXT NOT NULL, verdict TEXT NOT NULL,
 reason_code TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
 PRIMARY KEY(claim_id,matcher_version));
CREATE TABLE IF NOT EXISTS v2_task_links(
 task_key TEXT NOT NULL, claim_id TEXT NOT NULL, provenance TEXT NOT NULL,
 PRIMARY KEY(task_key,claim_id,provenance));
CREATE TABLE IF NOT EXISTS v2_requirements(
 task_key TEXT NOT NULL, requirement_id TEXT NOT NULL, required INTEGER NOT NULL,
 predicate TEXT NOT NULL, target_key TEXT, artifact_version TEXT,
 reviewed_public_label TEXT NOT NULL, PRIMARY KEY(task_key,requirement_id));
CREATE INDEX IF NOT EXISTS v2_turn_session ON v2_turns(profile,session_id,final_time);
CREATE INDEX IF NOT EXISTS v2_turn_group ON v2_turns(model,source,final_time);
CREATE INDEX IF NOT EXISTS v2_claim_task ON v2_claims(task_key,kind);
'''
_TABLES = {
    'sources': ('v2_sources', ('source_id','schema_version','availability','cutoff','coverage_reason')),
    'turns': ('v2_turns', ('turn_key','source_id','profile','session_id','user_id','final_id',
                         'final_time','model','model_certainty','source','project_key')),
    'claims': ('v2_claims', ('claim_id','turn_key','kind','span_start','span_end','target_key',
                           'task_key','detector_version')),
    'evidence': ('v2_evidence', ('evidence_id','turn_key','call_id','action','target_key',
                               'artifact_version','observed_at','level','status')),
    'findings': ('v2_findings', ('claim_id','matcher_version','verdict','reason_code','evidence_ids_json')),
    'task_links': ('v2_task_links', ('task_key','claim_id','provenance')),
    'requirements': ('v2_requirements', ('task_key','requirement_id','required','predicate',
                                       'target_key','artifact_version','reviewed_public_label')),
}
_VERDICTS = ('supported','contradicted','unverified','not_evaluable')
_GROUPS = {'profile': 't.profile', 'model': 't.model', 'source': 't.source',
           'source_id': 't.source_id', 'project_key': 't.project_key',
           'task_key': 'c.task_key', 'session_id': 't.session_id',
           'turn_key': 't.turn_key', 'day': "strftime('%Y-%m-%d', t.final_time, 'unixepoch')",
           'month': "strftime('%Y-%m', t.final_time, 'unixepoch')"}
_FILTERS = {'profile': 't.profile', 'model': 't.model', 'source': 't.source',
            'source_id': 't.source_id', 'project_key': 't.project_key',
            'task_key': 'c.task_key', 'session_id': 't.session_id', 'turn_key': 't.turn_key'}


def _normalized(kind, entry):
    """Allowlist keys only; reject forged identities, malformed enums and raw labels."""
    d = dict(entry)
    if 'target_key' in d and d['target_key'] is not None and not re.fullmatch(r'[0-9a-f]{64}', d['target_key']):
        raise ValueError('target_key must be a keyed hex fingerprint')
    if kind == 'sources':
        if d['schema_version'] != SCHEMA_VERSION:
            raise ValueError('unsupported schema version')
        if 'cursor' in d:
            if not re.fullmatch(r'[0-9]{1,20}', str(d['cursor'])):
                raise ValueError('invalid private cursor')
            d['coverage_reason'] = d['coverage_reason'] + ';cursor=' + str(d['cursor'])
    if kind == 'turns':
        if d['turn_key'] != turn_key({'source_id': d['source_id'], 'profile': d['profile'],
           'session_id': d['session_id'], 'user_message_id': d['user_id'], 'final_message_id': d['final_id']}):
            raise ValueError('turn composite identity mismatch')
    if kind in ('claims', 'evidence'):
        scope = d['scope']
        d.setdefault('turn_key', turn_key(scope))
        if d['turn_key'] != turn_key(scope):
            raise ValueError('source/profile/session/turn identity mismatch')
        if kind == 'claims' and d['claim_id'] != claim_key(scope, d['kind'], d['span_start'],
                                                           d['span_end'], d['detector_version']):
            raise ValueError('claim rule-version identity mismatch')
        if kind == 'evidence' and d['evidence_id'] != evidence_key(scope, d['call_id'],
                                                                    d['action'], d['level']):
            raise ValueError('evidence identity mismatch')
    if kind == 'findings':
        if d['verdict'] not in _VERDICTS:
            raise ValueError('invalid verdict')
        d['evidence_ids_json'] = json.dumps(d['evidence_ids'], separators=(',', ':'))
    if kind == 'requirements':
        d['predicate'] = d['evidence_predicate']
        d['reviewed_public_label'] = d['public_label']
        d['required'] = int(d['required'])
    if kind == 'task_links' and d['provenance'] not in ('manifest', 'structured_id'):
        raise ValueError('task links require explicit provenance')
    return tuple(d[col] for col in _TABLES[kind][1])


def upsert_run(db: str | Path | sqlite3.Connection, entities: dict) -> None:
    """Atomically upsert sanitized entities; report DB may be a chosen external path.

    Caller supplies scope-backed claim/evidence and metadata-only turns. Raw answer,
    messages, arguments and result fields are ignored, never persisted. No migration
    is applied to v1. Input is trusted local ingestion, not a web API.
    """
    owns = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(db) if owns else db
    try:
        with conn:
            conn.executescript(_SCHEMA)
            for kind, (table, cols) in _TABLES.items():
                values = [_normalized(kind, entry) for entry in entities.get(kind, ())]
                if not values:
                    continue
                placeholders = ','.join('?' * len(cols))
                # REPLACE is deterministic for the same version; no unbounded duplicate history.
                conn.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})", values)
    finally:
        if owns:
            conn.close()


def query_coverage(db: str | Path | sqlite3.Connection):
    """Show even empty/unavailable sources; a rollup alone cannot represent them."""
    owns = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(f'{Path(db).resolve().as_uri()}?mode=ro', uri=True) if owns else db
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute('SELECT source_id,schema_version,availability,cutoff,coverage_reason FROM v2_sources ORDER BY source_id')]
    finally:
        if owns:
            conn.close()


def query_rollup(db: str | Path | sqlite3.Connection, filters: dict, group_by: list[str] | tuple[str, ...]):
    """Detected-claim and unique-task counts with distinct done/action denominators.

    Version pins are mandatory; no stored missing claim is inferred contradicted.
    Reports aggregate only normalized metadata, with unknown attribution retained.
    """
    if not group_by or len(group_by) != len(set(group_by)):
        if group_by:
            raise ValueError('duplicate group dimension')
    if any(x not in _GROUPS for x in group_by):
        raise ValueError('unsupported group dimension')
    if not {'start', 'end', 'detector_version', 'matcher_version'} <= filters.keys():
        raise ValueError('explicit UTC window and detector/matcher versions required')
    if filters['start'] >= filters['end']:
        raise ValueError('start must precede end')
    if set(filters) - {'start','end','detector_version','matcher_version', *_FILTERS}:
        raise ValueError('unsupported filter')
    if 'task_key' in group_by:
        # A claim without a task remains an explicit unknown bucket.
        pass
    owns = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(f'{Path(db).resolve().as_uri()}?mode=ro', uri=True) if owns else db
    try:
        conn.row_factory = sqlite3.Row
        selects = ', '.join(f'{_GROUPS[g]} AS g{i}' for i, g in enumerate(group_by))
        conditions = ['t.final_time>=?', 't.final_time<?', 'c.detector_version=?']
        values = [filters[x] for x in ('matcher_version', 'start', 'end', 'detector_version')]
        for name, expr in _FILTERS.items():
            if name in filters:
                conditions.append(f'{expr}=?')
                values.append(filters[name])
        sql = f'''SELECT {selects + ',' if selects else ''} c.claim_id,c.kind,c.task_key,
                  COALESCE(f.verdict,'not_evaluable') AS verdict,t.final_time,t.source_id,t.model,t.source,t.profile
                  FROM v2_claims c JOIN v2_turns t ON t.turn_key=c.turn_key
                  LEFT JOIN v2_findings f ON f.claim_id=c.claim_id AND f.matcher_version=?
                  WHERE {' AND '.join(conditions)} ORDER BY t.final_time,c.claim_id'''
        buckets = {}
        for row in conn.execute(sql, values):
            key = tuple(row[f'g{i}'] for i in range(len(group_by)))
            bucket = buckets.setdefault(key, {**dict(zip(group_by, key)),
                'done': dict.fromkeys((*_VERDICTS, 'denominator'), 0),
                'action': dict.fromkeys((*_VERDICTS, 'denominator'), 0),
                'tasks': 0, 'task_completion': dict.fromkeys((*_VERDICTS, 'denominator'), 0),
                'unknown_model_claims': 0, 'unknown_source_claims': 0,
                '_tasks': set(), '_completion': {}})
            side = bucket['done' if row['kind'] == 'done' else 'action']
            side[row['verdict']] += 1
            if row['verdict'] != 'not_evaluable':
                side['denominator'] += 1
            if row['model'] == 'unknown':
                bucket['unknown_model_claims'] += 1
            if row['source'] == 'unknown':
                bucket['unknown_source_claims'] += 1
            if row['task_key']:
                bucket['_tasks'].add(row['task_key'])
                if row['kind'] == 'done':
                    # latest assertion per task in selected window/version, not every utterance
                    bucket['_completion'][row['task_key']] = row['verdict']
        result = []
        for bucket in buckets.values():
            bucket['tasks'] = len(bucket.pop('_tasks'))
            for verdict in bucket.pop('_completion').values():
                bucket['task_completion'][verdict] += 1
                if verdict != 'not_evaluable':
                    bucket['task_completion']['denominator'] += 1
            result.append(bucket)
        return result
    finally:
        if owns:
            conn.close()
