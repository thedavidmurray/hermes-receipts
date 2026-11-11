"""Blind, no-network release prediction runner; inputs exported by verifier only.

Never opens labels/seals or discovers profile DBs. Use a CLEAN committed repo:
python -m eval.release_predict --cases /private/unlabelled.json --output /private/predictions.json
Output is exclusive-create (no accidental rerun overwrite), 0600. Verifier owns
one-run protocol and export/seal identity; the handle is not authorization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from v2.detect import DETECTOR_VERSION, extract_claims
from v2.evidence import MATCHER_VERSION, judge_claims
from v2.tasks import judge_task

HANDLE = 'receipts-v2-gold-final-v1'
MANIFEST_SHA256 = '0f6debbb22a5b4c21778cef59764d46ecd6179368cac9b8da0898f708849bc57'


def predict(envelope):
    if set(envelope) != {'handle', 'detection', 'verdict'} or envelope['handle'] != HANDLE:
        raise ValueError('explicit unlabelled envelope and exact handle required')
    detection, verdict, seen = [], [], set()
    def identity(row, allowed):
        key = row['case_id']
        if set(row) - allowed or not isinstance(key, str) or not key or key in seen:
            raise ValueError('invalid, duplicated or labelled case')
        seen.add(key)
        return key
    for row in envelope['detection']:
        key = identity(row, {'case_id', 'answer', 'scope', 'links'})
        claims = extract_claims(row['answer'], row['scope'], row.get('links') or {})
        detection.append({'case_id': key, 'claims': [{k: c[k] for k in ('kind', 'span_start', 'span_end')}
                                                    for c in claims]})
    for row in envelope['verdict']:
        key = identity(row, {'case_id', 'claim', 'evidence', 'window', 'links', 'requirements'})
        if row['claim']['kind'] == 'done':
            finding = judge_task(row.get('requirements') or [], row['evidence'], tuple(row['window']),
                                 claim=row['claim'], links=row.get('links'))['completion']
        else:
            finding = judge_claims([row['claim']], row['evidence'], tuple(row['window']), row.get('links'))[0]
        verdict.append({'case_id': key, 'verdict': finding['verdict']})
    return {'handle': HANDLE, 'manifest_sha256': MANIFEST_SHA256,
            'detector_version': DETECTOR_VERSION, 'matcher_version': MATCHER_VERSION,
            'detection': detection, 'verdict': verdict}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if any(p.resolve().is_relative_to(root) for p in (args.cases, args.output)):
        raise SystemExit('Private evaluation inputs/outputs must remain outside the public repo.')
    if args.output.exists():
        raise SystemExit('Refusing repeat output; verifier owns one-run protocol.')
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True)
    if dirty:
        raise SystemExit('Refusing holdout run: commit and clean the evaluation tree first.')
    raw = args.cases.read_bytes()
    result = predict(json.loads(raw))
    result['repo_sha'] = head
    result['unlabelled_input_sha256'] = hashlib.sha256(raw).hexdigest()
    payload = (json.dumps(result, sort_keys=True, indent=2)+'\n').encode()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
    print(json.dumps({'handle': HANDLE, 'repo_sha': head,
                      'detection_n': len(result['detection']), 'verdict_n': len(result['verdict']),
                      'predictions_sha256': hashlib.sha256(payload).hexdigest()}))


if __name__ == '__main__':
    main()
