"""Receipts: host-observed actions, conservative answer claims and shareable paper receipts."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from plugins.plugin_storage import plugin_db, plugin_data_dir
from hermes_constants import get_hermes_home

from .history import receipt_from_state, verify_hash
from .receipt_core import classify, digest, match_claims
from .render import render_png, render_text

LOG = logging.getLogger(__name__)


def _db():
    db = plugin_db('receipts')
    db.execute('''CREATE TABLE IF NOT EXISTS ledger (
      id INTEGER PRIMARY KEY, session_id TEXT, turn_id TEXT, task_id TEXT, call_id TEXT UNIQUE,
      tool TEXT NOT NULL, args_sha256 TEXT NOT NULL, action TEXT, label TEXT,
      status TEXT NOT NULL, duration_ms INTEGER, observed_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    return db


def _post_tool_call(tool_name='', args=None, result=None, status='unknown', duration_ms=None,
                    session_id='', turn_id='', task_id='', tool_call_id='', **kwargs):
    """Capture only metadata. A missing call id is not deduplicated across tool calls."""
    action, label = classify(tool_name, args)
    try:
        with _db() as conn:
            conn.execute('''INSERT OR IGNORE INTO ledger
                (session_id,turn_id,task_id,call_id,tool,args_sha256,action,label,status,duration_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (session_id, str(turn_id), task_id, tool_call_id or None, tool_name,
                 digest(args), action, label, status, duration_ms))
    except Exception:
        LOG.exception('receipts ledger write failed')


def _source_db():
    return get_hermes_home() / 'state.db'


def _receipt(session_id=None, turn_id=None):
    return receipt_from_state(_source_db(), session_id=session_id or None, turn_id=turn_id,
                              ledger_path=plugin_data_dir('receipts') / 'data.db')


def _image(receipt):
    # Never use a caller-provided path: receipts may be requested from chat.
    path = plugin_data_dir('receipts') / 'images' / f"turn-{receipt['final_id']}-{receipt['hash'][:12]}.png"
    return render_png(receipt, path)


def _slash(raw_args, session_id='', **kwargs):
    try:
        arg = raw_args.strip()
        if arg and not arg.isdigit():
            return 'Usage: /receipt [final message id]'
        receipt = _receipt(turn_id=int(arg) if arg else None)  # latest profile turn by default
        path = _image(receipt)
        return f"{render_text(receipt)}\nMEDIA:{path}"
    except (OSError, ValueError) as exc:
        return f'Receipt unavailable: {exc}'


def _cli_setup(parser):
    sub = parser.add_subparsers(dest='receipt_action', required=True)
    last = sub.add_parser('last', help='Show the last persisted final answer receipt')
    last.add_argument('--session', help='Only this session ID')
    last.add_argument('--png', help='Save a thermal PNG to an explicit path')
    last.add_argument('--print-usb', nargs=2, metavar=('VENDOR_HEX', 'PRODUCT_HEX'),
                      help='Opt-in ESC/POS printer, e.g. --print-usb 0x04b8 0x0202')
    verify = sub.add_parser('verify', help='Recompute a full ledger hash from state.db')
    verify.add_argument('hash', help='Full 64-character sha256 ledger hash')
    from .v2.cli import setup
    setup(sub)


def _cli_handler(args):
    if args.receipt_action in ('task', 'fleet', 'report'):
        from .v2.cli import handle
        return handle(args)
    try:
        if args.receipt_action == 'last':
            receipt = _receipt(session_id=getattr(args, 'session', None))
            print(render_text(receipt))
            if args.png:
                print(f"PNG: {render_png(receipt, Path(args.png).expanduser())}")
            if args.print_usb:
                from .printer import print_receipt
                try:
                    vendor, product = (int(value, 16) for value in args.print_usb)
                except ValueError as exc:
                    raise ValueError('USB ids must be hexadecimal') from exc
                ok, message = print_receipt(render_text(receipt), vendor, product)
                print(message)
                if not ok:
                    return 1
        elif args.receipt_action == 'verify':
            receipt = verify_hash(_source_db(), args.hash, ledger_path=plugin_data_dir('receipts') / 'data.db')
            if receipt is None:
                print('NOT VERIFIED: hash not found in this profile state.db')
                return 1
            print(f"VERIFIED: {receipt['hash']} (session {receipt['session_id']}, final message {receipt['final_id']})")
        return 0
    except (OSError, ValueError) as exc:
        print(f'Receipt unavailable: {exc}')
        return 1


def _stamp(response_text='', session_id='', **kwargs):
    if os.environ.get('HERMES_RECEIPTS_STAMP') != '1':
        return None
    try:
        receipt = _receipt(session_id=session_id)
        claims = match_claims(response_text, receipt['rows'])
        ok = sum(c['verdict'] == '✓' for c in claims)
        bad = sum(c['verdict'] == '✗' for c in claims)
        return response_text + f'\n\n🧾 {ok}✓ {bad}✗' if claims else None
    except (OSError, ValueError):
        return None


def _gate(tool_name='', args=None, session_id='', **kwargs):
    """Opt-in advisory block for the last persisted answer in this exact session.

    Observe-only by default. Never approves tools. Since this checks the prior
    persisted turn, operators should review a block rather than treating it as
    proof that the current completion is wrong.
    """
    if (tool_name != 'kanban_complete' or os.environ.get('HERMES_RECEIPTS_GATE') != 'block'
            or not session_id):
        return None
    try:
        receipt = _receipt(session_id=session_id)
    except (OSError, ValueError):
        return None
    if any(claim['verdict'] == '✗' for claim in receipt['claims']):
        return {'action': 'block', 'reason': 'Last persisted receipt has unbacked action claims; review before completion.'}
    return None


def register(ctx):
    ctx.register_hook('post_tool_call', _post_tool_call)
    ctx.register_hook('transform_llm_output', _stamp)
    ctx.register_hook('pre_tool_call', _gate)
    ctx.register_command('receipt', _slash, description='Print an evidence receipt for an agent turn', args_hint='[final message id]')
    ctx.register_cli_command('receipt', 'Show and verify Hermes turn receipts', _cli_setup, _cli_handler)
