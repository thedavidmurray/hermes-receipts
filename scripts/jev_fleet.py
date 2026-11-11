"""Explicit opt-in Jev fleet scan. Source DBs are read-only; outputs private.

Usage: python scripts/jev_fleet.py --root ~/.hermes --output .private/jev.jsonl
Resume uses the original frozen [start,end) from output.manifest.json. All sessions
with at least one message in that window are attempted, including no-tool/chat.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_classifier import classify_session


def sources(root):
    return [p for p in [root/'state.db', *sorted((root/'profiles').glob('*/state.db'))] if p.is_file()]


def connect(path):
    if path.with_name(path.name+'-wal').exists() and not path.with_name(path.name+'-shm').exists():
        raise ValueError('wal_without_shm')
    conn = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def inventory(root, start, end):
    frames = []
    for path in sources(root):
        profile = 'default' if path.parent == root else path.parent.name
        try:
            with connect(path) as conn:
                ids = [r[0] for r in conn.execute('SELECT DISTINCT session_id FROM messages WHERE timestamp>=? AND timestamp<? ORDER BY session_id', (start, end))]
            frames.append(dict(profile=profile, path=str(path), sessions=ids, error=None))
        except (sqlite3.Error, ValueError) as exc:
            frames.append(dict(profile=profile, path=str(path), sessions=[], error=type(exc).__name__))
    return frames


def process(job, start, end):
    profile, path, sid = job
    base = dict(profile=profile, session=sid)
    started = time.monotonic()
    try:
        with connect(Path(path)) as conn:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(messages)')}
            active = ' AND active=1' if 'active' in cols else ''
            messages = [dict(r) for r in conn.execute('SELECT * FROM messages WHERE session_id=? AND timestamp>=? AND timestamp<?'+active+' ORDER BY timestamp,id', (sid, start, end))]
            row = conn.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            meta = dict(row) if row else {}
        times = [m['timestamp'] for m in messages]
        meta['wall_seconds'] = max(times)-min(times) if times else 0
        result = classify_session(messages, meta, enabled=True)
        base.update(ok=result['jev']['ok'], det=result['deterministic'], categories=result['categories'],
                    jev=result['jev'], messages=len(messages))
    except Exception as exc:
        base.update(ok=False, reason=type(exc).__name__)
    base['elapsed_ms'] = round((time.monotonic()-started)*1000)
    return base


def summarize(records, manifest):
    profiles = {f['profile']: dict(population=len(f['sessions']), attempted=0, ok=0, failed=0, source_error=f['error']) for f in manifest['sources']}
    agree = {k: Counter() for k in ('claims_done','tests_run','tests_passed','files_changed','pushed_or_published','external_side_effect','cost_heavy','long_running')}
    prevalence, cooccur = Counter(), Counter()
    latencies, usage = [], Counter()
    for row in records:
        p = profiles[row['profile']]
        p['attempted'] += 1
        p['ok' if row['ok'] else 'failed'] += 1
        if not row['ok']:
            continue
        jev, det = row['jev'], row['det']
        latencies.append(jev['latency_ms'])
        for key, value in jev.get('usage', {}).items():
            if isinstance(value, (int, float)):
                usage[key] += value
        ans = {k: a['choice'] for k, a in jev['answers'].items()}
        positive = sorted(k for k, v in ans.items() if v == 'yes')
        prevalence.update(positive)
        prevalence['session_type:'+ans['session_type']] += 1
        for i, a in enumerate(positive):
            for b in positive[i+1:]:
                cooccur[a+' & '+b] += 1
        for key in agree:
            pick = ans[key]
            agree[key]['n'] += 1
            agree[key]['unknown' if pick == 'unknown' else 'agree' if (pick == 'yes') == det[key] else 'disagree'] += 1
            if pick != 'unknown':
                agree[key][('TP' if det[key] else 'FP') if pick == 'yes' else ('FN' if det[key] else 'TN')] += 1
    for p in profiles.values():
        p['reconciled'] = p['attempted'] == p['ok']+p['failed'] == p['population']
    return dict(window=dict(start=manifest['start'], end=manifest['end']), profiles=profiles,
                attempted=len(records), ok=sum(r['ok'] for r in records), failed=sum(not r['ok'] for r in records),
                factual_agreement={k: dict(v) for k, v in agree.items()}, prevalence=dict(prevalence), cooccurrence=dict(cooccur),
                latency_ms=dict(median=statistics.median(latencies) if latencies else None, max=max(latencies) if latencies else None), usage=dict(usage),
                limitations=['Agreement is against invocation heuristics, NOT independent outcome truth.', 'Bounded sampled digests can omit evidence.', 'Jev scores are uncalibrated; judgments experimental.', 'No gold labels read or evaluated by this scan.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jev-path', type=Path, help='Explicit installed jevkit parent; no installation or live edits')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--inventory-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error('workers must be 1..16')
    if args.jev_path:
        sys.path.insert(0, str(args.jev_path.expanduser().resolve(strict=True)))
    root, output = args.root.expanduser().resolve(strict=True), args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_path = output.with_suffix('.manifest.json')
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest['root'] != str(root):
            parser.error('resume root differs')
    else:
        end = time.time()
        manifest = dict(root=str(root), start=end-30*86400, end=end, sources=inventory(root, end-30*86400, end))
        fd = os.open(manifest_path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(manifest, f)
    print('population', sum(len(f['sessions']) for f in manifest['sources']), 'sources', len(manifest['sources']), flush=True)
    if args.inventory_only:
        return
    records = [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []
    seen = {(r['profile'], r['session']) for r in records}
    jobs = ((f['profile'], f['path'], sid) for f in manifest['sources'] for sid in f['sessions'] if (f['profile'], sid) not in seen)
    fd = os.open(output, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
    os.chmod(output, 0o600)
    with os.fdopen(fd, 'a') as fh, ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = set()
        exhausted = False
        while pending or not exhausted:
            while len(pending) < args.workers*2 and not exhausted:
                job = next(jobs, None)
                if job is None:
                    exhausted = True
                else:
                    pending.add(pool.submit(process, job, manifest['start'], manifest['end']))
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                row = future.result()
                records.append(row)
                fh.write(json.dumps(row, separators=(',', ':'))+'\n')
                fh.flush()
                if len(records) % 200 == 0:
                    print('attempted', len(records), 'ok', sum(r['ok'] for r in records), flush=True)
    summary = summarize(records, manifest)
    summary_path = output.with_suffix('.summary.json')
    fd = os.open(summary_path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: summary[k] for k in ('attempted','ok','failed','latency_ms')}, indent=2))


if __name__ == '__main__':
    main()
