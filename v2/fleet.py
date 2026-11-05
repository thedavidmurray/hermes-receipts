"""Explicit local-only streaming fleet inventory; never opens config or mutates a source.

The iterator owns a short-lived read transaction per source and emits memory-only
turns followed by a SourceCoverage record. Callers must consume/close it; partial
coverage is not negative evidence. This module has no web entry point.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from .contracts import FleetLimits, Scope, SourceCoverage, TurnRecord, turn_key


def _sources(roots):
    if not roots:
        raise ValueError('explicit local root required')
    seen = set()
    for raw in roots:
        root = Path(raw).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError('fleet root must be a local directory')
        candidates = [root / 'state.db']
        profiles = root / 'profiles'
        if profiles.is_dir():
            candidates.extend(sorted(profiles.glob('*/state.db')))
        for path in candidates:
            if path.is_file():
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    raise ValueError('source symlink escapes allowed root')
                if resolved not in seen:
                    seen.add(resolved)
                    yield resolved
        if not any(p.is_file() for p in candidates):
            # Include an explicit missing source, not a silent empty population.
            missing = (root / 'state.db').resolve()
            if missing not in seen:
                seen.add(missing)
                yield missing


def _columns(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def _json_content(raw):
    if not isinstance(raw, str):
        return ''
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get('content'), str):
            return data['content']
    except (TypeError, ValueError):
        pass
    return raw


def _read_source(path, source_id, start, end, page, cap, deadline, cancelled) -> Iterator[TurnRecord | SourceCoverage]:
    coverage: SourceCoverage = dict(record_type='source', source_id=source_id,
                                    schema_version=2, availability='unavailable',
                                    cutoff=None, coverage_reason='unavailable')
    if not path.is_file():
        coverage['coverage_reason'] = 'missing_db'
        yield coverage
        return
    # SQLite may create SHM for a RO WAL. Never let a source open create one.
    if path.with_name(path.name + '-wal').exists() and not path.with_name(path.name + '-shm').exists():
        coverage['coverage_reason'] = 'wal_without_shm'
        yield coverage
        return
    try:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.2)
    except sqlite3.Error:
        coverage['coverage_reason'] = 'unavailable_ro'
        yield coverage
        return
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'messages' not in tables or 'sessions' not in tables:
            coverage['coverage_reason'] = 'missing_schema'
            return
        mcols, scols = _columns(conn, 'messages'), _columns(conn, 'sessions')
        required_m = {'id', 'session_id', 'role', 'content', 'timestamp'}
        if not required_m <= mcols or 'id' not in scols:
            coverage['coverage_reason'] = 'missing_schema'
            return
        coverage['availability'] = 'available'
        profile = "COALESCE(NULLIF(s.profile_name,''),'unknown')" if 'profile_name' in scols else "'unknown'"
        model = "COALESCE(NULLIF(s.model,''),'unknown')" if 'model' in scols else "'unknown'"
        source = "COALESCE(NULLIF(s.source,''),'unknown')" if 'source' in scols else "'unknown'"
        project = 's.git_repo_root' if 'git_repo_root' in scols else 'NULL'
        display = ''.join(f' AND COALESCE(m.{col},0)=0' for col in ('is_display_only', 'is_compacted') if col in mcols)
        # Id-only cursor is stable within one transaction; SQL values are parameterized.
        sql = f'''SELECT m.id,m.session_id,m.content,m.timestamp, {profile} profile,
                  {model} model, {source} source, {project} project
                  FROM messages m LEFT JOIN sessions s ON s.id=m.session_id
                  WHERE m.role='assistant' AND m.content IS NOT NULL AND trim(m.content)<>''
                  {"AND m.tool_calls IS NULL" if 'tool_calls' in mcols else ''}
                  {display} AND m.timestamp>=? AND m.timestamp<? AND m.id>?
                  ORDER BY m.id LIMIT ?'''
        # Duplicate sessions.id with different profile rows cannot be attributed safely.
        ambiguous = {r[0] for r in conn.execute('SELECT id FROM sessions GROUP BY id HAVING COUNT(*)>1')}
        cursor, count = -1, 0
        while True:
            if time.monotonic() >= deadline or (cancelled is not None and cancelled.is_set()):
                coverage['coverage_reason'] = 'cancelled' if cancelled is not None and cancelled.is_set() else 'time_budget'
                break
            rows = conn.execute(sql, (start, end, cursor, min(page, cap - count + 1))).fetchall()
            if not rows:
                if coverage['coverage_reason'] == 'unavailable':
                    coverage['coverage_reason'] = 'complete'
                if coverage['coverage_reason'] == 'complete':
                    coverage['cutoff'] = end
                break
            for final in rows:
                if count >= cap:
                    coverage['coverage_reason'] = 'max_finals'
                    break
                cursor = final['id']
                if final['session_id'] in ambiguous:
                    coverage['coverage_reason'] = 'ambiguous_session'
                    continue
                user = conn.execute('''SELECT id FROM messages WHERE session_id=? AND role='user'
                                    AND id<? ORDER BY id DESC LIMIT 1''', (final['session_id'], cursor)).fetchone()
                if user is None:
                    coverage['coverage_reason'] = 'orphan_final'
                    continue
                # Bound retained messages per turn; truncated turns cannot prove an outcome.
                messages = [dict(r) for r in conn.execute('''SELECT * FROM messages WHERE session_id=?
                    AND id>? AND id<=? ORDER BY id LIMIT 5001''',
                    (final['session_id'], user['id'], cursor))]
                if len(messages) > 5000:
                    coverage['coverage_reason'] = 'truncated_turn'
                    messages = []
                p = final['profile']
                scope: Scope = dict(source_id=source_id, profile=p, session_id=str(final['session_id']),
                                    user_message_id=str(user['id']), final_message_id=str(cursor))
                model_name = final['model']
                # Session-level model cannot establish which model produced this reply.
                project_key = (hashlib.sha256(('receipts.project:' + final['project']).encode()).hexdigest()
                               if isinstance(final['project'], str) and final['project'] else None)
                yield dict(record_type='turn', turn_key=turn_key(scope), source_id=source_id,
                           profile=p, session_id=scope['session_id'], user_id=scope['user_message_id'],
                           final_id=scope['final_message_id'], final_time=float(final['timestamp']),
                           model=model_name, model_certainty='session_only' if model_name != 'unknown' else 'unknown',
                           source=final['source'], project_key=project_key, scope=scope,
                           answer=_json_content(final['content']), messages=messages)
                coverage['cutoff'] = float(final['timestamp'])
                count += 1
            if coverage['coverage_reason'] == 'max_finals':
                break
            if len(rows) < min(page, cap - count + 1) and count < cap:
                # Let the next query prove exhaustion (even when a page ended at the cap).
                continue
        if coverage['coverage_reason'] != 'complete' and coverage['cutoff'] is None:
            coverage['cutoff'] = start
        if coverage['coverage_reason'] != 'complete' and cursor >= 0:
            coverage['cursor'] = str(cursor)
    except sqlite3.Error:
        coverage['availability'] = 'unavailable'
        coverage['coverage_reason'] = 'read_error'
    finally:
        conn.rollback()
        conn.close()
        yield coverage


def iter_turns(roots, start: float, end: float, limits: FleetLimits | None = None) -> Iterator[TurnRecord | SourceCoverage]:
    """Scan explicit local roots in UTC [start,end); emit turns and final coverage.

    start/end are POSIX UTC seconds. A 30-day window is the maximum default;
    longer windows require all_time=True. No remote paths, config or web access.
    """
    if not roots:
        raise ValueError('explicit local root required')
    limits = limits or {}
    if start >= end:
        raise ValueError('UTC half-open window must have start < end')
    if end - start > 30 * 86400 and not limits.get('all_time', False):
        raise ValueError('window >30 days requires all_time opt-in')
    page = limits.get('page_size', 500)
    cap = limits.get('max_finals', 100_000)
    budget = limits.get('max_seconds', 300)
    if not (1 <= page <= 500 and cap > 0 and budget > 0):
        raise ValueError('invalid resource limits')
    deadline = time.monotonic() + budget
    for path in _sources(roots):
        sid = hashlib.sha256(('receipts.source:' + str(path)).encode()).hexdigest()
        yield from _read_source(path, sid, start, end, page, cap, deadline, limits.get('cancelled'))
