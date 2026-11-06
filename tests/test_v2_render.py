"""Lane D: scoped PNG, canary, actual CLI, API and current-session boundaries."""
import argparse
import base64
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('receipt_d_package', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from receipt_d_package.v2.contracts import claim_key
from receipt_d_package.v2.public import to_public_receipt, to_public_report
from receipt_d_package.v2.render import public_text, render_task_png, render_report_png
from receipt_d_package.v2.evidence import observe_artifact
from receipt_d_package.v2.tasks import judge_task

SCOPE = {'source_id': 'CANARY_SOURCE', 'profile': 'CANARY_PROFILE', 'session_id': 'CANARY_SESSION',
         'user_message_id': 'CANARY_USER', 'final_message_id': 'CANARY_FINAL'}
TARGET = 'a' * 64


def _fixture(tmp_path):
    artifact = tmp_path / 'artifact.bin'
    artifact.write_bytes(b'actual controlled evidence')
    import hashlib
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    now = time.time()
    requirements = [{'requirement_id': 'CANARY_ID', 'public_label': '/private/CANARY_PATH',
                     'required': True, 'evidence_predicate': 'artifact_digest',
                     'target_key': TARGET, 'artifact_version': digest}]
    claim = {'claim_id': claim_key(SCOPE, 'done', 0, 4, 'deterministic-2.0'), 'scope': SCOPE,
             'kind': 'done', 'task_key': 'CANARY_TASK', 'target_key': TARGET}
    evidence = observe_artifact(artifact, digest, SCOPE, TARGET, digest, observed_at=now)
    return artifact, digest, requirements, claim, evidence, (now - 60, now + 60)


def test_scoped_png_pixel_and_metadata_canaries(tmp_path):
    artifact, digest, requirements, claim, evidence, window = _fixture(tmp_path)
    private = judge_task(requirements, [evidence], window, claim=claim)
    assert private['completion']['verdict'] == 'supported'
    public = to_public_receipt(private, {'CANARY_ID': 'Artifact bytes match'})
    assert public['completion'] == 'supported'
    assert public['artifact_version'] == 'sha256-' + digest[:12]
    output = render_task_png(public, tmp_path / 'public.png')
    text = public_text(public)
    assert 'not unrestricted proof of completeness' in text.replace('\n', ' ')
    assert 'Artifact bytes match' in text
    assert 'CANARY_' not in json.dumps(public)
    with Image.open(output) as im:
        im.load()
        assert im.width == 652 and not im.info
    pixels = subprocess.run(['tesseract', str(output), 'stdout'], capture_output=True, text=True, check=True).stdout
    assert 'ARTIFACT BYTES MATCH' in pixels.upper()
    for canary in ('CANARY_', '/private/', 'CANARY_PATH', 'CANARY_SESSION'):
        assert canary not in output.read_bytes().decode('latin1')
        assert canary not in pixels
        assert canary not in text
    assert 'CANARY' not in output.name
    # Arbitrary manifest labels never leak even when offered for review.
    assert to_public_receipt(private, {'CANARY_ID': '/private/CANARY_PATH'})['criteria'][0]['label'] == 'Criterion 1'


def test_unverified_is_not_contradiction_and_report_small_sample(tmp_path):
    artifact, digest, requirements, claim, evidence, window = _fixture(tmp_path)
    private = judge_task(requirements, [], window, claim=claim)
    public = to_public_receipt(private, {})
    assert public['completion'] == 'unverified'
    assert 'missing evidence is not a lie' in public_text(public).replace('\n', ' ')
    report = to_public_report([{'profile': 'CANARY_PROFILE', 'done': {'supported': 1},
        'action': {'supported': 3, 'unverified': 2}, 'task_completion': {'supported': 1},
        'unknown_model_claims': 1, 'unknown_source_claims': 1, 'tasks': 1}],
        [{'source_id': 'CANARY_SOURCE', 'availability': 'unavailable', 'coverage_reason': 'CANARY_SECRET'}],
        start=window[0], end=window[1], group_by=['profile'])
    assert report['groups'][0]['done']['rate'] == 'counts only (n<10)'
    assert 'CANARY_' not in json.dumps(report)
    image = render_report_png(report, tmp_path / 'statement.png')
    with Image.open(image) as im:
        im.load()
        assert im.width == 652 and not im.info
    pixels = subprocess.run(['tesseract', str(image), 'stdout'], capture_output=True, text=True, check=True).stdout
    assert 'NOT A LIE RATE' in pixels.upper()
    assert 'CANARY' not in pixels + public_text(report)
    assert 'CANARY' not in image.read_bytes().decode('latin1')


def test_public_page_limits_fail_closed_instead_of_hiding_required_criteria():
    private = {'requirements': [{'requirement_id': str(i), 'required': True,
        'target_key': TARGET, 'artifact_version': 'a' * 64} for i in range(41)],
        'criterion_findings': {str(i): {'verdict': 'supported'} for i in range(41)},
        'completion': {'verdict': 'supported'}, 'observation_start': 0, 'observation_end': 10}
    with pytest.raises(ValueError, match='too many criteria'):
        to_public_receipt(private, {})
    with pytest.raises(ValueError, match='too many report groups'):
        to_public_report([{}] * 31, [], start=0, end=10, group_by=['profile'])


def test_cli_task_actual_probe_and_no_manifest_forgery(tmp_path, capsys):
    artifact, digest, requirements, claim, evidence, window = _fixture(tmp_path)
    manifest = tmp_path / 'private.json'
    manifest.write_text(json.dumps({'claim': claim, 'requirements': requirements, 'window': window,
        'reviewed_labels': {'CANARY_ID': 'Artifact bytes match'},
        'evidence': [evidence], 'probes': [{'requirement_id': 'CANARY_ID', 'path': str(artifact), 'sha256': digest}]}))
    parser = argparse.ArgumentParser()
    plugin._cli_setup(parser)
    args = parser.parse_args(['task', '--manifest', str(manifest), '--png', str(tmp_path / 'task.png')])
    assert plugin._cli_handler(args) == 0
    assert 'COMPLETION: SUPPORTED' in capsys.readouterr().out
    artifact.unlink()
    assert plugin._cli_handler(args) == 0
    output = capsys.readouterr().out
    assert 'COMPLETION: CONTRADICTED' not in output  # missing artifact cannot certify completion
    assert str(manifest) not in output and str(artifact) not in output

    # A real file read cannot certify a different declared digest/version.
    artifact.write_bytes(b'actual controlled evidence')
    forged = json.loads(manifest.read_text())
    forged['requirements'][0]['artifact_version'] = 'b' * 64
    manifest.write_text(json.dumps(forged))
    assert plugin._cli_handler(args) == 1
    assert 'COMPLETION: SUPPORTED' not in capsys.readouterr().out


def test_completion_intercept_is_registered_but_opt_in():
    class Context:
        hooks = []
        def register_hook(self, name, callback): self.hooks.append(name)
        def register_command(self, *args, **kwargs): pass
        def register_cli_command(self, *args, **kwargs): pass
    ctx = Context()
    plugin.register(ctx)
    assert ctx.hooks == ['post_tool_call', 'transform_llm_output', 'pre_tool_call']


def test_v2_usb_uses_exact_public_text_and_reports_fallback(tmp_path, monkeypatch, capsys):
    artifact, digest, requirements, claim, evidence, window = _fixture(tmp_path)
    manifest = tmp_path / 'private.json'
    manifest.write_text(json.dumps({'claim': claim, 'requirements': requirements, 'window': window,
        'reviewed_labels': {'CANARY_ID': 'Artifact bytes match'},
        'probes': [{'requirement_id': 'CANARY_ID', 'path': str(artifact), 'sha256': digest}]}))
    import receipt_d_package.printer as printer
    sent = []
    def fake_print(text, vendor, product):
        sent.append((text, vendor, product))
        return False, 'private USB CANARY_SECRET'
    monkeypatch.setattr(printer, 'print_receipt', fake_print)
    parser = argparse.ArgumentParser()
    plugin._cli_setup(parser)
    args = parser.parse_args(['task', '--manifest', str(manifest), '--png', str(tmp_path / 'task.png'),
        '--print-usb', '0xffff', '0xffff'])
    assert plugin._cli_handler(args) == 1
    assert 'Artifact bytes match' in sent[0][0] and 'CANARY_' not in sent[0][0]
    assert sent[0][1:] == (0xffff, 0xffff)
    assert 'CANARY_' not in capsys.readouterr().out


def test_fleet_cli_disposable_source_to_report(tmp_path, capsys):
    root = tmp_path / 'source'
    root.mkdir()
    db = root / 'state.db'
    with sqlite3.connect(db) as conn:
        conn.executescript("""CREATE TABLE sessions(id TEXT PRIMARY KEY,profile_name TEXT,model TEXT,source TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT,timestamp REAL);
            INSERT INTO sessions VALUES ('a','demo','unknown','unknown');
            INSERT INTO messages VALUES (1,'a','user','Do it',NULL,10);
            INSERT INTO messages VALUES (2,'a','assistant','Done.',NULL,20);""")
    import hashlib
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    parser = argparse.ArgumentParser()
    plugin._cli_setup(parser)
    report_db = tmp_path / 'report.db'
    args = parser.parse_args(['fleet', '--roots', str(root), '--start', '1970-01-01T00:00:00Z',
                              '--end', '1970-01-01T00:01:00Z', '--report-db', str(report_db)])
    assert plugin._cli_handler(args) == 0
    assert 'Imported 1 finals' in capsys.readouterr().out
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    args = parser.parse_args(['report', '--report-db', str(report_db), '--group-by', 'profile,model,source',
                              '--png', str(tmp_path / 'statement.png')])
    assert plugin._cli_handler(args) == 0
    text = capsys.readouterr().out
    assert 'NOT A LIE RATE' in text and 'CANARY_' not in text
    assert (tmp_path / 'statement.png').is_file()


def test_current_route_rejects_missing_and_cross_profile(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    db = tmp_path / 'state.db'
    with sqlite3.connect(db) as conn:
        conn.executescript("""CREATE TABLE sessions(id TEXT PRIMARY KEY,profile_name TEXT,input_tokens INTEGER,output_tokens INTEGER,estimated_cost_usd REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,
                tool_call_id TEXT,tool_calls TEXT,tool_name TEXT,timestamp REAL);
            INSERT INTO sessions(id,profile_name) VALUES ('good','default'),('other','different');
            INSERT INTO messages VALUES (1,'good','user','Hi',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES (2,'good','assistant','Done',NULL,NULL,NULL,2);
            INSERT INTO messages VALUES (3,'other','user','Secret',NULL,NULL,NULL,3);
            INSERT INTO messages VALUES (4,'other','assistant','Done',NULL,NULL,NULL,4);""")
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    modspec = importlib.util.spec_from_file_location('receipt_d_api', ROOT / 'dashboard/plugin_api.py')
    mod = importlib.util.module_from_spec(modspec)
    modspec.loader.exec_module(mod)
    app = FastAPI()
    app.state.auth_required = True
    @app.middleware('http')
    async def simulated_gateway_owner(request, call_next):
        if request.headers.get('x-test-owner') == 'verified':
            request.state.hosted_http_authenticated = True
        return await call_next(request)
    app.include_router(mod.router)
    unauthenticated = TestClient(app)
    assert unauthenticated.get('/current?session_id=good').status_code == 401
    assert unauthenticated.get('/last?session_id=good').status_code == 401
    client = TestClient(app, headers={'x-test-owner': 'verified'})
    assert client.get('/current').status_code == 422
    assert client.get('/last').status_code == 422
    assert client.get('/current?session_id=other').status_code == 404
    assert client.get('/current?session_id=missing').status_code == 404
    response = client.get('/current?session_id=good')
    assert response.status_code == 200
    data = response.json()
    assert data['completion'] == 'unverified' and 'session_id' not in data
    assert b'CANARY' not in base64.b64decode(data['png'].split(',', 1)[1])
