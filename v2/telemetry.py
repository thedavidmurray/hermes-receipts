"""Read-only telemetry for one real Hermes session.

The receipt's fine print has to come from somewhere real. This module reads a
Hermes ``state.db`` with ``mode=ro`` and ``PRAGMA query_only=ON`` and returns a
plain metrics mapping; it never writes, never touches the ledger, and never
returns a value it could not read (a missing metric is simply absent, and the
field renderer prints ``n/a``).

Every read is bounded:

* one session row by primary key,
* one ``session_model_usage`` row by session (or two distinct provider values
  when optional ordering metadata is absent),
* a bounded window of messages for turns / top tools / files touched.

Session-scoped token and cost totals are labelled as such through
``cost_scope``; Hermes stores usage per session, so a per-turn figure is never
invented from it.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

#: Hard caps so a 4 GB state.db cannot turn a receipt into a table scan.
MAX_WINDOW_MESSAGES = 5000
MAX_TOOL_CALLS_PARSED = 4000
TOP_TOOL_COUNT = 5
#: Tools whose arguments name a file the turn wrote to.
_FILE_TOOLS = ('write_file', 'patch', 'edit_file', 'multi_edit', 'create_file', 'str_replace')
_PATH_KEYS = ('path', 'file_path', 'filename', 'target', 'file')
_SESSION_SCOPE = 'session totals (not per turn)'


def _connection(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def _col(row: Mapping, columns: set[str], key: str) -> Any:
    """Value of an optional column; older/leaner state DBs simply lack some."""
    return row[key] if key in columns else None


def _json(text: Any, fallback: Any) -> Any:
    try:
        value = json.loads(text) if isinstance(text, str) and text.strip() else fallback
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _tool_name(call: dict) -> str:
    raw_function = call.get('function')
    function = raw_function if isinstance(raw_function, dict) else {}
    name = function.get('name') or call.get('name') or 'unknown'
    return str(name)


def _tool_args(call: dict) -> dict:
    raw_function = call.get('function')
    function = raw_function if isinstance(raw_function, dict) else {}
    raw = function.get('arguments') or call.get('arguments') or {}
    if isinstance(raw, dict):
        return raw
    parsed = _json(raw, {})
    return parsed if isinstance(parsed, dict) else {}


def _resolve_session(conn: sqlite3.Connection, session_id: str | None,
                     turn_id: int | None) -> sqlite3.Row:
    if turn_id is not None:
        row = conn.execute('SELECT id, session_id, timestamp FROM messages WHERE id=?', (turn_id,)).fetchone()
        if row is None:
            raise ValueError('no such message')
        session_id = row['session_id']
    if not session_id:
        row = conn.execute("SELECT session_id FROM messages WHERE role='assistant' "
                           'AND content IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
        if row is None:
            raise ValueError('no session found in this state.db')
        session_id = row['session_id']
    session = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    if session is None:
        # A message may exist for a session row that was pruned; identity only.
        raise ValueError('session unavailable')
    return session


def _window(conn: sqlite3.Connection, session_id: str, turn_id: int | None) -> tuple[float | None, float | None]:
    """Half-open-ish (start, end) timestamps bounding the reported turn."""
    final = None
    if turn_id is not None:
        final = conn.execute('SELECT id, timestamp FROM messages WHERE id=?', (turn_id,)).fetchone()
    if final is None:
        final = conn.execute("SELECT id, timestamp FROM messages WHERE session_id=? AND role='assistant' "
                             'AND content IS NOT NULL ORDER BY id DESC LIMIT 1', (session_id,)).fetchone()
    if final is None:
        return None, None
    user = conn.execute("SELECT id, timestamp FROM messages WHERE session_id=? AND role='user' AND id<? "
                        'ORDER BY id DESC LIMIT 1', (session_id, final['id'])).fetchone()
    return (float(user['timestamp']) if user else None, float(final['timestamp']))


def _messages(conn: sqlite3.Connection, session_id: str, start: float | None,
              end: float | None) -> list[sqlite3.Row]:
    conditions = ['session_id=?']
    values: list[Any] = [session_id]
    if start is not None:
        conditions.append('timestamp>=?')
        values.append(start)
    if end is not None:
        conditions.append('timestamp<=?')
        values.append(end)
    sql = (f"SELECT id,role,content,tool_calls,tool_name,timestamp FROM messages WHERE "
           f"{' AND '.join(conditions)} ORDER BY id LIMIT {MAX_WINDOW_MESSAGES}")
    return conn.execute(sql, values).fetchall()


def _top_tools_from_session(conn: sqlite3.Connection, session: Mapping) -> list[list]:
    """Cheap fallback histogram from ``sessions.tool_names`` when present."""
    raw = session['tool_names'] if 'tool_names' in session.keys() else None
    if not isinstance(raw, str) or not raw.strip():
        return []
    names = _json(raw, [])
    if not isinstance(names, list):
        names = [part for part in re.split(r'[,\s]+', raw) if part]
    counts = Counter(str(name) for name in names if str(name).strip())
    return [[name, count] for name, count in counts.most_common(TOP_TOOL_COUNT)]


def _provider(session: Mapping, columns: set[str], conn: sqlite3.Connection,
              session_id: str) -> str | None:
    billing = _col(session, columns, 'billing_provider')
    if isinstance(billing, str) and billing.strip():
        return billing.strip()
    usage_columns = _columns(conn, 'session_model_usage')
    if {'session_id', 'billing_provider'} <= usage_columns:
        if 'last_seen' in usage_columns:
            row = conn.execute('SELECT billing_provider FROM session_model_usage WHERE session_id=? '
                               'ORDER BY last_seen DESC LIMIT 1', (session_id,)).fetchone()
        else:
            # Without ordering metadata, only an unambiguous provider is evidence.
            # Two distinct values suffice to reject ambiguity; never guess latest.
            rows = conn.execute('SELECT DISTINCT billing_provider FROM session_model_usage '
                                'WHERE session_id=? LIMIT 2', (session_id,)).fetchall()
            row = rows[0] if len(rows) == 1 else None
        if row is not None and isinstance(row['billing_provider'], str) and row['billing_provider'].strip():
            return row['billing_provider'].strip()
    model = _col(session, columns, 'model')
    if isinstance(model, str) and '/' in model:
        return model.split('/', 1)[0] or None
    return None


def facts_from_state(db_path: Path | str, session_id: str | None = None,
                     turn_id: int | None = None) -> dict:
    """Measured metrics for one real session (read-only; never a guess).

    Returns a mapping with only the keys that were actually observed. Anything
    unavailable is omitted, and the field renderer prints ``n/a`` for it.
    """
    conn = _connection(db_path)
    try:
        session = _resolve_session(conn, session_id, turn_id)
        sid = str(session['id'])
        columns = _columns(conn, 'sessions')
        start, end = _window(conn, sid, turn_id)
        rows = _messages(conn, sid, start, end)

        turns = sum(1 for row in rows if row['role'] == 'assistant')
        tool_total = 0
        counter: Counter[str] = Counter()
        files: set[str] = set()
        for row in rows:
            calls = _json(row['tool_calls'], [])
            for call in calls[:MAX_TOOL_CALLS_PARSED]:
                if not isinstance(call, dict):
                    continue
                tool_total += 1
                name = _tool_name(call)
                counter[name] += 1
                if name in _FILE_TOOLS:
                    args = _tool_args(call)
                    for key in _PATH_KEYS:
                        value = args.get(key)
                        if isinstance(value, str) and value.strip():
                            files.add(value.strip())
                            break
        top_tools = counter.most_common(TOP_TOOL_COUNT) or _top_tools_from_session(conn, session)
        if tool_total == 0 and top_tools:
            tool_total = sum(count for _, count in top_tools)

        metrics: dict[str, Any] = {}
        incoming = _col(session, columns, 'input_tokens')
        outgoing = _col(session, columns, 'output_tokens')
        if isinstance(incoming, (int, float)) and incoming >= 0:
            metrics['tokens_in'] = incoming
        if isinstance(outgoing, (int, float)) and outgoing >= 0:
            metrics['tokens_out'] = outgoing
        if 'tokens_in' in metrics and 'tokens_out' in metrics:
            metrics['tokens_total'] = metrics['tokens_in'] + metrics['tokens_out']
        cost = _col(session, columns, 'estimated_cost_usd')
        if isinstance(cost, (int, float)) and cost >= 0:
            metrics['cost_usd'] = cost
            metrics['cost_scope'] = _SESSION_SCOPE
        if turns:
            metrics['turns'] = turns
        if tool_total:
            metrics['tool_calls'] = tool_total
        if top_tools:
            metrics['top_tools'] = [[str(name), int(count)] for name, count in top_tools]
        if files:
            metrics['files_touched'] = len(files)
        model = _col(session, columns, 'model')
        if isinstance(model, str) and model.strip():
            metrics['model'] = model.strip()
        provider = _provider(session, columns, conn, sid)
        if provider:
            metrics['provider'] = provider
        profile = _col(session, columns, 'profile_name')
        if isinstance(profile, str) and profile.strip():
            metrics['profile'] = profile.strip()
            metrics['agent'] = profile.strip()
        metrics['session'] = sid

        duration = _duration(session, columns, start, end)
        if duration is not None:
            metrics['duration_seconds'] = duration
        return metrics
    finally:
        conn.close()


def _duration(session: Mapping, columns: set[str], start: float | None,
              end: float | None) -> float | None:
    """Wall clock for the turn when known, else the session's own bounds."""
    if start is not None and end is not None and end >= start:
        return round(end - start, 1)
    finished = None
    for key in ('ended_at', 'last_activity_at'):
        value = _col(session, columns, key)
        if isinstance(value, (int, float)) and value:
            finished = float(value)
            break
    if finished is None and end is not None:
        finished = end
    began = None
    started = _col(session, columns, 'started_at')
    if isinstance(started, (int, float)) and started:
        began = float(started)
    if began is None or finished is None or finished < began:
        return None
    return round(finished - began, 1)
