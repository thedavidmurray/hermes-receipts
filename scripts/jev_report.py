"""Verify a completed private fleet run and export ONLY aggregate measurements."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jev_fleet import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.input.with_suffix('.manifest.json').read_text())
    records = [json.loads(line) for line in args.input.read_text().splitlines()]
    expected = {(f['profile'], sid) for f in manifest['sources'] for sid in f['sessions']}
    actual = [(r['profile'], r['session']) for r in records]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError('duplicate/missing/unexpected records; report refused')
    report = summarize(records, manifest)
    if any(p['source_error'] or not p['reconciled'] for p in report['profiles'].values()):
        raise ValueError('unreconciled source; report refused')
    report['sources_count'] = len(manifest['sources'])
    report['window_utc'] = {k: datetime.fromtimestamp(v, timezone.utc).isoformat() for k, v in report['window'].items()}
    report['private_jsonl_sha256'] = hashlib.sha256(args.input.read_bytes()).hexdigest()
    report['failure_types'] = dict(Counter(r.get('reason') or r.get('jev', {}).get('reason') for r in records if not r['ok']))
    by_type = defaultdict(Counter)
    for r in records:
        if not r['ok']:
            continue
        answers = r['jev']['answers']
        kind = answers['session_type']['choice']
        by_type[kind]['n'] += 1
        by_type[kind].update(k for k, a in answers.items() if a['choice'] == 'yes')
    report['by_model_type'] = {k: dict(v) for k, v in by_type.items()}
    for values in report['factual_agreement'].values():
        values['agreement_percent_all_ok'] = round(100 * values.get('agree', 0) / values['n'], 2)
    report['limitations'] += [
        'This is the frozen fleet-run baseline. Post-scan wrapped-result parsing and response-allowlist hardening were unit-tested, not rescanned.',
        'Private answers are not published. Only aggregate output, no session IDs or message text.',
        'SQLite SQL is read-only; normal WAL readers can update transient SHM read marks. Source bytes/metadata were not asserted immutable.',
        'Usage totals cover successful replies; failed-call provider billing is unknown. No USD pricing was inferred.',
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('sources_count', 'attempted', 'ok', 'failed', 'latency_ms', 'failure_types', 'window_utc', 'factual_agreement')}, indent=2))


if __name__ == '__main__':
    main()
