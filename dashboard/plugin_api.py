"""Profile-scoped, authenticated gateway API for the Desktop receipt pane."""
from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
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


@router.get('/last')
def last(session_id: str | None = Query(default=None, max_length=256)):
    try:
        receipt = receipt_from_state(get_hermes_home() / 'state.db', session_id=session_id,
                                     ledger_path=plugin_data_dir('receipts') / 'data.db')
        path = plugin_data_dir('receipts') / 'images' / f"turn-{receipt['final_id']}-{receipt['hash'][:12]}.png"
        render_png(receipt, path)
        return {'text': render_text(receipt), 'png': 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode(),
                'hash': receipt['hash'], 'final_id': receipt['final_id']}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
