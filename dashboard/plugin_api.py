"""Profile-scoped, authenticated gateway API for the Desktop receipt pane."""
from __future__ import annotations

import base64
import importlib.util
import io
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
import sqlite3
from hermes_constants import get_hermes_home
from plugins.plugin_storage import plugin_data_dir

# Dashboard API files are loaded as standalone modules, not package children.
_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location('receipts_dashboard_lib', _ROOT / '__init__.py',
                                              submodule_search_locations=[str(_ROOT)])
_LIB = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _LIB
_SPEC.loader.exec_module(_LIB)
receipt_from_state, render_png, render_text = _LIB.receipt_from_state, _LIB.render_png, _LIB.render_text

router = APIRouter()
#: Query-parameter style limits: a bad kit/theme is a client error, not a missing session.
MAX_STYLE_LENGTH = 64


def _style(kit: str | None, theme: str | None, fields: str | None) -> dict:
    """Validate a pane's kit/theme/field choice against the on-disk registries."""
    from receipts_dashboard_lib.v2.fields import (kit_names, theme_names, validate_fields)

    if kit and kit not in kit_names():
        raise HTTPException(status_code=400, detail='Unsupported receipt kit')
    if theme and theme not in theme_names():
        raise HTTPException(status_code=400, detail='Unsupported receipt theme')
    style: dict = {'kit': kit, 'theme': theme, 'fields': None}
    if fields:
        try:
            style['fields'] = validate_fields(fields)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='Unsupported receipt field') from exc
    return style


def _authorize(request: Request):
    # Host-isolated routes receive a per-request boolean after gateway auth.
    # Fail closed if mounted in-process or accessed outside the admitted IPC path.
    if request.scope.get('state', {}).get('hosted_http_authenticated') is not True:
        raise HTTPException(status_code=401, detail='Authenticated gateway request required')


def _session_profile_guard(session_id: str) -> None:
    """Do not serve another persisted profile through the current-profile route."""
    if not session_id or len(session_id) > 256:
        raise HTTPException(status_code=400, detail='Active session required')
    source = get_hermes_home() / 'state.db'
    try:
        with sqlite3.connect(source.resolve(strict=True).as_uri() + '?mode=ro', uri=True) as conn:
            conn.execute('PRAGMA query_only=ON')
            cols = {row[1] for row in conn.execute('PRAGMA table_info(sessions)')}
            if 'profile_name' not in cols:
                return  # legacy single-profile DB: session selection below still exact
            row = conn.execute('SELECT profile_name FROM sessions WHERE id=?', (session_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='Session unavailable')
            current = get_hermes_home().name if get_hermes_home().parent.name == 'profiles' else 'default'
            if row[0] != current:
                raise HTTPException(status_code=404, detail='Session unavailable')
    except sqlite3.Error as exc:
        raise HTTPException(status_code=404, detail='Session unavailable') from exc


@router.get('/options', dependencies=[Depends(_authorize)])
def options():
    """The kits, themes and fields a pane may offer, straight from the registry."""
    from receipts_dashboard_lib.v2.fields import (KNOWN_FIELDS, OPTIONAL_SECTIONS,
                                                  kit_descriptions, theme_descriptions)
    return {'kits': kit_descriptions(), 'themes': theme_descriptions(),
            'fields': list(KNOWN_FIELDS), 'optional_sections': list(OPTIONAL_SECTIONS),
            'default': 'verify'}


@router.get('/current', dependencies=[Depends(_authorize)])
def current(session_id: str = Query(..., min_length=1, max_length=256),
            kit: str = Query(None, max_length=MAX_STYLE_LENGTH),
            theme: str = Query(None, max_length=MAX_STYLE_LENGTH),
            fields: str = Query(None, max_length=2000)):
    """Active-pane-only public preview; absent manifest can NEVER certify done.

    ``kit``/``theme``/``fields`` are optional; the verdict block prints whatever
    the caller chooses, and an unknown choice is rejected rather than ignored.
    """
    _session_profile_guard(session_id)
    style = _style(kit, theme, fields)
    try:
        source = get_hermes_home() / 'state.db'
        receipt_from_state(source, session_id=session_id,
                           ledger_path=plugin_data_dir('receipts') / 'data.db')
        from datetime import datetime, timezone
        from receipts_dashboard_lib.v2.public import to_public_receipt
        from receipts_dashboard_lib.v2.render import render_task_png
        from receipts_dashboard_lib.v2.telemetry import facts_from_state
        start = datetime.now(timezone.utc).timestamp() - 86400
        end = datetime.now(timezone.utc).timestamp()
        private = {'requirements': [], 'criterion_findings': {}, 'completion': {'verdict': 'unverified'},
                   'observation_start': start, 'observation_end': end}
        try:
            # Real telemetry for the fine print; a read error must not break the pane.
            metrics = facts_from_state(source, session_id=session_id)
        except (OSError, ValueError):
            metrics = {}
        public = to_public_receipt(private, {}, metrics=metrics, **style)
        png = io.BytesIO()
        render_task_png(public, png, kit=style['kit'], theme=style['theme'])
        return {'completion': public['completion'], 'kit': style['kit'] or 'verify',
                'theme': style['theme'] or 'thermal-classic',
                'warning': 'No explicit task manifest; completion unverified.',
                'png': 'data:image/png;base64,' + base64.b64encode(png.getvalue()).decode()}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail='Session unavailable') from exc


@router.get('/last', dependencies=[Depends(_authorize)])
def last(session_id: str = Query(..., min_length=1, max_length=256)):
    _session_profile_guard(session_id)
    try:
        receipt = receipt_from_state(get_hermes_home() / 'state.db', session_id=session_id,
                                     ledger_path=plugin_data_dir('receipts') / 'data.db')
        path = plugin_data_dir('receipts') / 'images' / f"turn-{receipt['final_id']}-{receipt['hash'][:12]}.png"
        render_png(receipt, path)
        return {'text': render_text(receipt), 'png': 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode(),
                'hash': receipt['hash'], 'final_id': receipt['final_id']}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
