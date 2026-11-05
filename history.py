"""Read-only, lazy backfill from the selected profile's Hermes state.db."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .receipt_core import classify, digest, ledger_hash, match_claims


def _json(text, fallback):
    try:
        value = json.loads(text) if text else fallback
        return value if isinstance(value, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _result_status(text: str | None) -> tuple[str, int | None]:
    data = _json(text, {})
    if not data and isinstance(text, str) and text.startswith("{"):
        outer = _json(text, {})
        data = outer.get("data") if isinstance(outer.get("data"), dict) else outer
    code = data.get("exit_code") if isinstance(data, dict) else None
    if code is None and isinstance(data, dict) and isinstance(data.get("result"), dict):
        code = data["result"].get("exit_code")
    if code is not None:
        return ("ok" if code == 0 else "error"), code
    if isinstance(data, dict) and (data.get("error") or data.get("success") is False or data.get("ok") is False or data.get("status") == "error"):
        return "error", None
    # No explicit success bit: do not promote unknown to verified.
    if isinstance(data, dict) and (data.get("success") is True or data.get("ok") is True or data.get("verified") is True):
        return "ok", None
    return "unknown", None


def _args(call):
    fn = call.get("function", {}) if isinstance(call, dict) else {}
    raw = fn.get("arguments", "{}")
    return _json(raw, {}) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})


def _tool_name(call):
    fn = call.get("function", {}) if isinstance(call, dict) else {}
    return fn.get("name") or call.get("name") or "unknown"


def _contents(text):
    if not text:
        return ""
    data = _json(text, {})
    return str(data.get("content", text)) if isinstance(data, dict) else str(text)


def receipt_from_state(db_path: Path, session_id: str | None = None, turn_id: int | None = None,
                       ledger_path: Path | None = None) -> dict:
    """Find any final answer by id; past turns work immediately, without import or migration."""
    db_path = Path(db_path).expanduser().resolve(strict=True)
    # SQLite URI mode=ro guarantees this function does not create or mutate the source.
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if turn_id is not None:
            final = conn.execute("SELECT id,session_id,content,timestamp FROM messages WHERE id=? AND role='assistant' AND content IS NOT NULL AND trim(content)<>'' AND tool_calls IS NULL", (turn_id,)).fetchone()
        elif session_id:
            final = conn.execute("SELECT id,session_id,content,timestamp FROM messages WHERE session_id=? AND role='assistant' AND content IS NOT NULL AND trim(content)<>'' AND tool_calls IS NULL ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        else:
            final = conn.execute("SELECT id,session_id,content,timestamp FROM messages WHERE role='assistant' AND content IS NOT NULL AND trim(content)<>'' AND tool_calls IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if final is None:
            raise ValueError("no persisted final assistant answer found")
        user = conn.execute("SELECT id,timestamp FROM messages WHERE session_id=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1", (final['session_id'], final['id'])).fetchone()
        if user is None:
            raise ValueError("final answer has no preceding user turn")
        messages = conn.execute("SELECT id,role,tool_call_id,tool_calls,tool_name,content,timestamp FROM messages WHERE session_id=? AND id>? AND id<=? ORDER BY id", (final['session_id'], user['id'], final['id'])).fetchall()
        results = {m['tool_call_id']: m for m in messages if m['role'] == 'tool' and m['tool_call_id']}
        rows = []
        for msg in messages:
            if msg['role'] != 'assistant' or not msg['tool_calls']:
                continue
            for call in _json(msg['tool_calls'], []):
                if not isinstance(call, dict):
                    continue
                cid = str(call.get('id') or call.get('call_id') or '')
                result = results.get(cid)
                name = str(_tool_name(call))
                args = _args(call)
                action, label = classify(name, args)
                status, exit_code = _result_status(result['content']) if result else ('unknown', None)
                rows.append(dict(tool=name, action=action, label=label, args_sha256=digest(args), status=status,
                                 exit_code=exit_code, duration_ms=None, call_id=cid, session_id=final['session_id'],
                                 turn_id=user['id'], timestamp=msg['timestamp']))
        if ledger_path is not None and Path(ledger_path).is_file():
            # Enrich only exact call-id/argument-digest matches; never cross a session boundary.
            observed = sqlite3.connect(Path(ledger_path).resolve().as_uri() + '?mode=ro', uri=True)
            try:
                for row in rows:
                    if not row['call_id']:
                        continue
                    hit = observed.execute('''SELECT duration_ms,status FROM ledger
                        WHERE call_id=? AND session_id=? AND args_sha256=?''',
                        (row['call_id'], row['session_id'], row['args_sha256'])).fetchone()
                    if hit:
                        row['duration_ms'] = hit[0]
                        row['status'] = hit[1]
            finally:
                observed.close()
        # Hermes stores usage at session scope, not per turn. Never present session totals as turn totals.
        usage = conn.execute("SELECT input_tokens,output_tokens,estimated_cost_usd FROM sessions WHERE id=?", (final['session_id'],)).fetchone()
        answer = _contents(final['content'])
        return dict(session_id=final['session_id'], turn_id=user['id'], final_id=final['id'], answer=answer,
                    rows=rows, claims=match_claims(answer, rows), hash=ledger_hash(rows),
                    tokens=None, cost_usd=None,
                    session_tokens=(usage['input_tokens'] or 0) + (usage['output_tokens'] or 0) if usage else None,
                    session_cost_usd=usage['estimated_cost_usd'] if usage else None,
                    wall_seconds=round(final['timestamp'] - user['timestamp'], 1),
                    source='state.db (read-only retrospective)', completeness='tool calls persisted in this turn only')
    finally:
        conn.close()


def verify_hash(db_path: Path, expected: str, ledger_path: Path | None = None) -> dict | None:
    """Recompute from the original state DB; reject ambiguous short hashes."""
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected.lower()):
        raise ValueError('verify requires a full 64-character sha256')
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM messages WHERE role='assistant' AND content IS NOT NULL AND trim(content)<>'' AND tool_calls IS NULL ORDER BY id DESC")]
    finally:
        conn.close()
    for mid in ids:
        try:
            receipt = receipt_from_state(db_path, turn_id=mid, ledger_path=ledger_path)
            if receipt['hash'] == expected.lower():
                return receipt
        except ValueError:
            continue
    return None
