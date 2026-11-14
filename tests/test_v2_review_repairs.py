"""Adversarial regressions from independent fields/kits review t_c4b408de.

Controlled SQLite/router and PNG fixtures, not detector holdout acceptance.
"""
from contextlib import closing
from io import BytesIO
import importlib
import importlib.util
from pathlib import Path
import sqlite3

import pytest
from PIL import Image, ImageColor

from v2 import fields, render
from v2.telemetry import facts_from_state

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def themes(monkeypatch):
    snapshot = dict(fields.THEMES.items())
    monkeypatch.setattr(fields, 'THEMES', snapshot)
    return snapshot


@pytest.mark.parametrize('verdict', fields.VERDICT_WORDS)
@pytest.mark.parametrize('parity', [0, 1])
def test_dark_striped_required_verdict_pixels_remain_legible(themes, verdict, parity):
    themes['dark-striped'] = {
        **themes['thermal-classic'], 'mono': True, 'paper': '#000000',
        'ink': '#ffffff', 'green_bar': True,
        **{'ink_' + v: '#ffffff' for v in fields.VERDICT_WORDS},
    }
    theme = fields.load_theme('dark-striped')
    lines = [render.Line('padding')] * parity + [render.Line(
        f'REQ 1: required {fields.verdict_word(verdict)} {fields.verdict_glyph(verdict)}',
        kind='verdict', verdict=verdict)]
    output = BytesIO()
    render._draw(lines, theme, output)
    output.seek(0)
    with Image.open(output) as image:
        y = render.TOP + parity * render.LINE_HEIGHT
        # Inside inverse rectangles but outside glyphs. The contradicted claim
        # deliberately inverts; other verdicts must retain validated black paper.
        inverse = verdict == 'contradicted'
        background = '#ffffff' if inverse else '#000000'
        ink = '#000000' if inverse else '#ffffff'
        assert image.getpixel((render.LEFT - 7, y)) == ImageColor.getrgb(background)
        glyphs = image.crop((render.LEFT, y, 610, y + render.LINE_HEIGHT - 5))
        pixels = {glyphs.getpixel((x, row)) for x in range(glyphs.width)
                  for row in range(glyphs.height)}
        assert ImageColor.getrgb(ink) in pixels, 'required verdict glyph pixels disappeared'
        assert fields.contrast_ratio(ink, background) >= fields.MIN_VERDICT_CONTRAST
        assert image.getpixel((37, y)) == (0, 0, 0), 'pale stripe returned'


@pytest.mark.parametrize('first,second', [
    ('supported', 'contradicted'), ('supported', 'unverified'),
    ('supported', 'not_evaluable'), ('contradicted', 'unverified'),
    ('contradicted', 'not_evaluable'), ('unverified', 'not_evaluable'),
])
@pytest.mark.parametrize('base', ['thermal-classic', 'bold-branded'])
def test_nonmono_semantic_colour_role_inversions_are_rejected(themes, base, first, second):
    raw = dict(themes[base])
    a, b = 'ink_' + first, 'ink_' + second
    raw[a], raw[b] = raw[b], raw[a]
    themes['inverted'] = raw
    with pytest.raises(fields.ThemeError, match='colour role'):
        fields.load_theme('inverted')


@pytest.mark.parametrize('verdict,ink', [
    ('supported', '#555555'), ('supported', '#203f80'),
    ('contradicted', '#0b5c33'), ('unverified', '#8c1d18'),
    ('not_evaluable', '#0b5c33'),
])
def test_distinct_contrasting_wrong_colour_families_are_rejected(themes, verdict, ink):
    themes['wrong-role'] = {**themes['thermal-classic'], 'ink_' + verdict: ink}
    with pytest.raises(fields.ThemeError, match='colour role'):
        fields.load_theme('wrong-role')


def test_custom_shades_keep_semantic_roles_and_render(themes):
    themes['custom-shades'] = {**themes['thermal-classic'], 'paper': '#ffffff',
                              'ink_supported': '#0b5c33', 'ink_contradicted': '#8c1d18',
                              'ink_unverified': '#7a5200', 'ink_not_evaluable': '#444444'}
    theme = fields.load_theme('custom-shades')
    for verdict in fields.VERDICT_WORDS:
        assert render._line_color(render.Line('required', kind='verdict', verdict=verdict), theme) == (
            themes['custom-shades']['ink_' + verdict])


@pytest.mark.parametrize('key', [
    'hide_required', 'soften_failure', 'suppress_criteria', 'mute_completion',
    'verdict_size', 'glyph_custom', 'relabel_required', 'rename_failure', 'remap_supported',
    ' HIDE_new_family_member ', 'Glyph_set', 'glyph_set_custom',
])
def test_unlisted_forbidden_prefix_members_fail_closed(themes, key):
    themes['prefix'] = {**themes['thermal-classic'], key: True}
    with pytest.raises(fields.ThemeError, match='never hide or soften'):
        fields.load_theme('prefix')


@pytest.mark.parametrize('selector', ['ascii', 'unicode'])
def test_intended_glyph_set_selector_remains_allowed(themes, selector):
    themes['selector'] = {**themes['thermal-classic'], 'glyph_set': selector}
    assert fields.load_theme('selector')['glyph_set'] == selector


def _state(path, usage_sql=''):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""CREATE TABLE sessions(id TEXT PRIMARY KEY, profile_name TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL);
            INSERT INTO sessions VALUES ('good','default');
            INSERT INTO messages VALUES (1,'good','user','Controlled fixture',NULL,NULL,NULL,10);
            INSERT INTO messages VALUES (2,'good','assistant','Done',NULL,NULL,NULL,20);
            """ + usage_sql)
        conn.commit()


@pytest.mark.parametrize('usage_sql,provider', [
    ('CREATE TABLE session_model_usage(session_id TEXT);', None),
    ('CREATE TABLE session_model_usage(billing_provider TEXT, last_seen REAL);', None),
    ('CREATE TABLE session_model_usage(session_id TEXT, billing_provider TEXT);'
     "INSERT INTO session_model_usage VALUES ('good','only-provider'),('other','wrong');", 'only-provider'),
    ('CREATE TABLE session_model_usage(session_id TEXT, billing_provider TEXT);'
     "INSERT INTO session_model_usage VALUES ('good','a'),('good','b');", None),
    ('CREATE TABLE session_model_usage(session_id TEXT, billing_provider TEXT, last_seen REAL);'
     "INSERT INTO session_model_usage VALUES ('good','old',1),('good','new',2),('other','wrong',3);", 'new'),
])
def test_optional_usage_columns_are_inspected_without_losing_other_facts(tmp_path, usage_sql, provider):
    db = tmp_path / 'lean.db'
    _state(db, usage_sql)
    before = db.read_bytes()
    metrics = facts_from_state(db, session_id='good')
    assert metrics.get('provider') == provider
    assert metrics['session'] == 'good' and metrics['turns'] == 1
    assert metrics['duration_seconds'] == 10 and 'tokens_in' not in metrics
    assert db.read_bytes() == before


@pytest.mark.parametrize('error', [None, sqlite3.OperationalError, sqlite3.DatabaseError])
def test_actual_router_returns_unverified_png_when_optional_telemetry_fails(tmp_path, monkeypatch, error):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _state(tmp_path / 'state.db', 'CREATE TABLE session_model_usage(session_id TEXT);')
    # The legacy ledger reader requires these session columns. They remain NULL:
    # this regression targets a lean optional usage table, not ledger migrations.
    with closing(sqlite3.connect(tmp_path / 'state.db')) as conn:
        for name in ('input_tokens', 'output_tokens', 'estimated_cost_usd'):
            conn.execute(f'ALTER TABLE sessions ADD COLUMN {name} REAL')
        conn.commit()
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('pane_api_repairs', ROOT / 'dashboard/plugin_api.py')
    assert spec is not None and spec.loader is not None
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    telemetry = importlib.import_module('receipt_printer_dashboard_lib.v2.telemetry')
    renderer = importlib.import_module('receipt_printer_dashboard_lib.v2.render')
    if error:
        def failed_read(*args, **kwargs):
            raise error('controlled SQLite read failure')
        monkeypatch.setattr(telemetry, 'facts_from_state', failed_read)
    captured = []
    actual_render = renderer.render_task_png

    def observe(public, *args, **kwargs):
        captured.append(public)
        return actual_render(public, *args, **kwargs)

    monkeypatch.setattr(renderer, 'render_task_png', observe)
    app = FastAPI()

    @app.middleware('http')
    async def owner(request, call_next):
        request.state.hosted_http_authenticated = True
        return await call_next(request)

    app.include_router(api.router)
    with TestClient(app) as client:
        response = client.get('/current?session_id=good&kit=cost&theme=ascii')
    assert response.status_code == 200
    body = response.json()
    assert body['completion'] == 'unverified'
    assert body['warning'] == 'No explicit task manifest; completion unverified.'
    import base64
    with Image.open(BytesIO(base64.b64decode(body['png'].split(',', 1)[1]))) as image:
        image.verify()
    assert len(captured) == 1
    text = renderer.public_text(captured[0], kit='cost', theme='ascii')
    assert 'TOKENS IN: n/a' in text and 'COST (EST.): n/a' in text
    if error:
        assert not captured[0].get('metrics'), 'failed reads must not invent measured metrics'
