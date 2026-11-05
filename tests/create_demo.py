"""Deterministic controlled demo (not real-session telemetry), PNGs under explicit output dir."""
import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('receipt_demo_package', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from receipt_demo_package.v2.cli import handle
from receipt_demo_package.v2.contracts import claim_key
from receipt_demo_package.v2.store import upsert_run, query_rollup, query_coverage
from receipt_demo_package.v2.tasks import judge_task
from receipt_demo_package.v2.evidence import observe_artifact
from receipt_demo_package.v2.public import to_public_report, to_public_receipt
from receipt_demo_package.v2.render import render_report_png, render_task_png
from receipt_demo_package.v2.detect import DETECTOR_VERSION
from receipt_demo_package.v2.evidence import MATCHER_VERSION


def main(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / 'controlled-artifact.bin'
    artifact.write_bytes(b'controlled demonstration only\n')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    now = time.time()
    scope = {'source_id': 'local-controlled', 'profile': 'demo', 'session_id': 'demo-session',
             'user_message_id': '1', 'final_message_id': '2'}
    target = 'a' * 64
    claim = {'claim_id': claim_key(scope, 'done', 0, 4, DETECTOR_VERSION), 'scope': scope,
             'kind': 'done', 'target_key': target, 'task_key': 'controlled-demo'}
    requirements = [{'requirement_id': 'artifact', 'public_label': 'Artifact bytes match',
                     'required': True, 'evidence_predicate': 'artifact_digest', 'target_key': target,
                     'artifact_version': digest}]
    manifest = output / 'controlled-manifest.private.json'
    manifest.write_text(json.dumps({'claim': claim, 'requirements': requirements,
        'window': [now-60, now+60], 'reviewed_labels': {'artifact': 'Artifact bytes match'},
        'probes': [{'requirement_id': 'artifact', 'path': str(artifact), 'sha256': digest}]}))
    assert handle(argparse.Namespace(receipt_action='task', manifest=str(manifest),
                                   png=str(output / 'controlled-task.png'), print_usb=None)) == 0
    report_db = output / 'controlled-report.private.db'
    evidence = observe_artifact(artifact, digest, scope, target, digest, observed_at=now)
    finding = judge_task(requirements, [evidence], (now-60, now+60), claim=claim)['completion']
    from receipt_demo_package.v2.contracts import turn_key
    upsert_run(report_db, {'sources': [{'source_id': 'local-controlled', 'schema_version': 2,
        'availability': 'available', 'cutoff': now, 'coverage_reason': 'complete'}],
        'turns': [{'turn_key': turn_key(scope), 'source_id': scope['source_id'],
            'profile': scope['profile'], 'session_id': scope['session_id'], 'user_id': scope['user_message_id'],
            'final_id': scope['final_message_id'], 'final_time': now, 'model': 'unknown',
            'model_certainty': 'unknown', 'source': 'unknown', 'project_key': None}],
        'claims': [dict(claim, span_start=0, span_end=4, detector_version=DETECTOR_VERSION)],
        'evidence': [evidence], 'findings': [finding]})
    rows = query_rollup(report_db, {'start': now-60, 'end': now+60,
        'detector_version': DETECTOR_VERSION, 'matcher_version': MATCHER_VERSION}, ['profile'])
    public = to_public_report(rows, query_coverage(report_db),
        start=now-60, end=now+60, group_by=['profile'])
    render_report_png(public, output / 'controlled-statement.png')
    pane_public = to_public_receipt({'requirements': [], 'criterion_findings': {},
        'completion': {'verdict': 'unverified'}, 'observation_start': now - 86400,
        'observation_end': now}, {})
    render_task_png(pane_public, output / 'controlled-pane-unverified.png')
    print('Controlled measured fixture; neither fleet telemetry nor hardware print verified.')


if __name__ == '__main__':
    main(sys.argv[1])
