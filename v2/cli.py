"""Local v2 CLI registrations. No fleet root or private manifest accepted over HTTP."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .detect import DETECTOR_VERSION, extract_claims
from .evidence import MATCHER_VERSION, normalize_evidence, judge_claims, observe_artifact, observe_remote_commit
from .fleet import iter_turns
from .public import to_public_receipt, to_public_report
from .render import public_text, render_task_png, render_report_png
from .store import upsert_run, query_coverage, query_rollup
from .tasks import judge_task


def _time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('UTC offset required in start/end')
    return parsed.astimezone(timezone.utc).timestamp()


def setup(sub):
    task = sub.add_parser('task', help='Scoped task receipt from an explicit local manifest')
    task.add_argument('--manifest', required=True)
    task.add_argument('--png', required=True)
    task.add_argument('--print-usb', nargs=2, metavar=('VENDOR_HEX', 'PRODUCT_HEX'))
    fleet = sub.add_parser('fleet', help='Explicit opt-in local-only fleet scan')
    fleet.add_argument('--roots', nargs='+', required=True)
    fleet.add_argument('--start', required=True)
    fleet.add_argument('--end', required=True)
    fleet.add_argument('--report-db', required=True)
    fleet.add_argument('--all-time', action='store_true')
    report = sub.add_parser('report', help='Render grouped local evidence statement')
    report.add_argument('--report-db', required=True)
    report.add_argument('--group-by', required=True)
    report.add_argument('--png', required=True)
    report.add_argument('--start', help='Optional UTC ISO8601 lower bound; defaults to stored finals minimum')
    report.add_argument('--end', help='Optional UTC ISO8601 upper bound; defaults to stored finals maximum + 1s')


def _task(args):
    manifest = json.loads(Path(args.manifest).expanduser().read_text())
    required = {'claim', 'requirements', 'window'}
    if not required <= manifest.keys():
        raise ValueError('explicit claim, requirements and window required')
    window = tuple(float(v) for v in manifest['window'])
    claim = manifest['claim']
    if claim.get('kind') != 'done' or not claim.get('task_key'):
        raise ValueError('explicit linked done claim required')
    evidence = []
    # Manifest-supplied verdicts/readbacks are NOT proof: only perform these
    # explicit, actual local probes here. No shell commands from JSON ever run.
    for probe in manifest.get('probes', []):
        requirement = next((r for r in manifest['requirements'] if r['requirement_id'] == probe.get('requirement_id')), None)
        if not requirement:
            raise ValueError('probe references unknown criterion')
        common = (claim['scope'], requirement['target_key'], requirement['artifact_version'])
        if requirement['evidence_predicate'] == 'artifact_digest' and probe.get('sha256'):
            if len(str(requirement['artifact_version'])) == 64 and probe['sha256'] != requirement['artifact_version']:
                raise ValueError('artifact probe digest does not match declared version')
            evidence.append(observe_artifact(probe['path'], probe['sha256'], *common))
        elif requirement['evidence_predicate'] == 'remote_commit' and probe.get('commit'):
            if len(str(requirement['artifact_version'])) == 40 and probe['commit'] != requirement['artifact_version']:
                raise ValueError('remote probe commit does not match declared version')
            evidence.append(observe_remote_commit(probe['bare_remote'], probe['commit'], *common))
        else:
            raise ValueError('only explicit artifact digest and local bare-remote probes available in CLI; tests need trusted in-process runner')
    private = judge_task(manifest['requirements'], evidence, window, claim=claim, links=manifest.get('links'))
    # A caller-supplied manifest may include labels but can never bypass review.
    public = to_public_receipt(private, manifest.get('reviewed_labels', {}))
    output = render_task_png(public, args.png)
    print(public_text(public))
    print('Public PNG saved to requested local output.')
    if args.print_usb:
        from ..printer import print_receipt
        vendor, product = (int(value, 16) for value in args.print_usb)
        ok, _ = print_receipt(public_text(public), vendor, product)
        print('USB submission attempted; physical paper unverified without an observer.' if ok else
              'USB unavailable; saved public PNG remains the fallback (physical paper unverified).')
        return 0 if ok else 1
    return 0


def _fleet(args):
    start, end = _time(args.start), _time(args.end)
    report_path = Path(args.report_db).expanduser().resolve()
    if any(report_path.is_relative_to(Path(root).expanduser().resolve()) for root in args.roots):
        raise ValueError('report DB must be outside every source root')
    counts = {'turns': 0, 'sources': 0}
    # Keep one small turn in memory; no raw answer, messages, arguments or result persisted.
    for record in iter_turns(args.roots, start, end, {'all_time': args.all_time}):
        if record['record_type'] == 'source':
            upsert_run(args.report_db, {'sources': [record]})
            counts['sources'] += 1
            continue
        claims = extract_claims(record['answer'], record['scope'], {})
        evidence = normalize_evidence(record)
        findings = judge_claims(claims, evidence, (start, end))
        upsert_run(args.report_db, {'turns': [record], 'claims': claims, 'evidence': evidence, 'findings': findings})
        counts['turns'] += 1
    print(f"Imported {counts['turns']} finals from {counts['sources']} sources; no source DB writes. Coverage is in report DB.")
    return 0


def _report(args):
    import sqlite3
    db = Path(args.report_db).expanduser().resolve(strict=True)
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
        bounds = conn.execute('SELECT MIN(final_time), MAX(final_time) FROM v2_turns').fetchone()
    now = datetime.now(timezone.utc).timestamp()
    start = _time(args.start) if args.start else (bounds[0] if bounds[0] is not None else now - 30 * 86400)
    end = _time(args.end) if args.end else (bounds[1] + 1 if bounds[1] is not None else now)
    dimensions = args.group_by.split(',')
    groups = query_rollup(args.report_db, {'start': start, 'end': end,
        'detector_version': DETECTOR_VERSION, 'matcher_version': MATCHER_VERSION}, dimensions)
    public = to_public_report(groups, query_coverage(args.report_db), start=start, end=end, group_by=dimensions)
    output = render_report_png(public, args.png)
    print(public_text(public))
    print('Public PNG saved to requested local output.')
    return 0


def handle(args):
    try:
        return {'task': _task, 'fleet': _fleet, 'report': _report}[args.receipt_action](args)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        # Do not echo private paths, raw manifest text, or underlying errors.
        print(f'Receipt unavailable: {type(exc).__name__}; check local inputs and permissions.')
        return 1
