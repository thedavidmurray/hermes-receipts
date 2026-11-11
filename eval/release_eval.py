"""Pure verifier-side metrics; no discovery of private gold, profiles or networks.

Builders test this on synthetic fixtures ONLY. The independent verifier supplies
frozen labels and predictions, and keeps all individual IDs/text private.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path

HANDLE = 'receipts-v2-gold-final-v1'
MANIFEST_SHA256 = '0f6debbb22a5b4c21778cef59764d46ecd6179368cac9b8da0898f708849bc57'

KINDS = ('tests', 'commit', 'push', 'send', 'deploy', 'write', 'run', 'done')
VERDICTS = ('supported', 'contradicted', 'unverified', 'not_evaluable')


def wilson(success: int, total: int) -> list[float] | None:
    """Wilson 95% binomial interval, not model uncertainty or causal assurance."""
    if not 0 <= success <= total:
        raise ValueError('invalid binomial counts')
    if not total:
        return None
    z = 1.959963984540054
    p = success / total
    d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    half = z * math.sqrt(p * (1-p) / total + z*z / (4*total*total)) / d
    return [max(0., center-half), min(1., center+half)]


def _ratio(n, d):
    return n / d if d else None


def binary_counts(pairs):
    counts = dict.fromkeys(('TP', 'FP', 'FN', 'TN'), 0)
    for gold, prediction in pairs:
        if type(gold) is not bool or type(prediction) is not bool:
            raise ValueError('binary labels must be boolean')
        counts['TP' if gold and prediction else 'FN' if gold else 'FP' if prediction else 'TN'] += 1
    tp, fp, fn, tn = (counts[k] for k in ('TP', 'FP', 'FN', 'TN'))
    return {**counts, 'n': tp+fp+fn+tn, 'precision': _ratio(tp, tp+fp),
            'recall': _ratio(tp, tp+fn), 'precision_n': tp+fp, 'recall_n': tp+fn,
            'precision_wilson95': wilson(tp, tp+fp), 'recall_wilson95': wilson(tp, tp+fn)}


def _validate_spans(spans):
    for s in spans:
        if (s['kind'] not in KINDS or type(s['span_start']) is not int or
                type(s['span_end']) is not int or not 0 <= s['span_start'] < s['span_end']):
            raise ValueError('invalid span')


def match_spans(gold, predicted):
    """Maximum-cardinality one-to-one same-kind overlapping span assignment.

    Augmenting paths avoid the greedy error when one prediction overlaps two
    gold spans and another prediction can match only the first.
    """
    _validate_spans(gold)
    _validate_spans(predicted)
    edges = [[j for j, g in enumerate(gold) if p['kind'] == g['kind'] and
              max(p['span_start'], g['span_start']) < min(p['span_end'], g['span_end'])]
             for p in predicted]
    owners = {}
    def visit(i, seen):
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owners or visit(owners[j], seen):
                owners[j] = i
                return True
        return False
    for i in range(len(predicted)):
        visit(i, set())
    return len(owners)


def _index(rows):
    indexed = {}
    for row in rows:
        key = row['case_id']
        if not isinstance(key, str) or not key or key in indexed:
            raise ValueError('invalid or duplicate case_id')
        indexed[key] = row
    return indexed


def score_detection(gold, predictions):
    """Explicit random/enriched strata, seven-action separate from done.

    Rows: case_id, stratum (gold only), claims=[{kind,span_start,span_end}].
    No per-case IDs, spans, source strings or labels appear in returned scores.
    """
    g, p = _index(gold), _index(predictions)
    if set(g) != set(p) or not g:
        raise ValueError('exact nonempty gold/prediction ID coverage required')
    if any(r['stratum'] not in ('random', 'enriched') for r in g.values()):
        raise ValueError('unknown stratum')
    for row in [*g.values(), *p.values()]:
        _validate_spans(row['claims'])
    result = {}
    for stratum in ('random', 'enriched'):
        ids = [k for k, r in g.items() if r['stratum'] == stratum]
        sides = {}
        for name, kinds in [('seven_action', KINDS[:-1]), ('done', ('done',))]:
            pairs, tp, ng, np = [], 0, 0, 0
            for key in ids:
                gs = [c for c in g[key]['claims'] if c['kind'] in kinds]
                ps = [c for c in p[key]['claims'] if c['kind'] in kinds]
                pairs.append((bool(gs), bool(ps)))
                tp += match_spans(gs, ps)
                ng += len(gs)
                np += len(ps)
            sides[name] = {'answer': binary_counts(pairs), 'claim': {
                'TP': tp, 'FP': np-tp, 'FN': ng-tp, 'gold_n': ng, 'prediction_n': np,
                'precision': _ratio(tp, np), 'recall': _ratio(tp, ng),
                'precision_wilson95': wilson(tp, np), 'recall_wilson95': wilson(tp, ng)}}
        sides['by_kind_answer'] = {
            kind: binary_counts((any(c['kind'] == kind for c in g[k]['claims']),
                                 any(c['kind'] == kind for c in p[k]['claims'])) for k in ids)
            for kind in KINDS}
        result[stratum] = sides
    primary = result['random']['seven_action']['answer']
    result['detection_target_met'] = (primary['precision'] is not None and primary['recall'] is not None
                                      and primary['precision'] >= .84 and primary['recall'] >= .75)
    return result


def _verdict_metrics(pairs):
    matrix = {g: dict.fromkeys(VERDICTS, 0) for g in VERDICTS}
    for gold, pred in pairs:
        if gold not in VERDICTS or pred not in VERDICTS:
            raise ValueError('unknown verdict')
        matrix[gold][pred] += 1
    classes = {}
    for v in VERDICTS:
        tp = matrix[v][v]
        gn = sum(matrix[v].values())
        pn = sum(matrix[g][v] for g in VERDICTS)
        classes[v] = {'TP': tp, 'gold_n': gn, 'prediction_n': pn,
                      'precision': _ratio(tp, pn), 'recall': _ratio(tp, gn),
                      'f1': 2*tp/(gn+pn) if gn+pn else 0.,
                      'precision_wilson95': wilson(tp, pn), 'recall_wilson95': wilson(tp, gn)}
    n = len(pairs)
    decided = sum(classes[v]['prediction_n'] for v in VERDICTS[:2])
    return {'n': n, 'matrix_gold_rows_prediction_columns': matrix, 'classes': classes,
            'macro_f1': sum(c['f1'] for c in classes.values())/4 if n else None,
            'support_coverage': _ratio(classes['supported']['prediction_n'], n),
            'decision_coverage': _ratio(decided, n), 'abstention_rate': _ratio(n-decided, n)}


def score_verdicts(gold, predictions):
    """Rows: case_id, verdict; gold also kind, evidence_type, population.

    Per-kind/default-kit reliability is evidence reporting, never auto-activation.
    Completion false-support and true-completion withholding use gold supported
    as the explicitly defined proxy; unverified gold is NOT proven false done.
    """
    g, p = _index(gold), _index(predictions)
    if not g or set(g) != set(p):
        raise ValueError('exact nonempty gold/prediction ID coverage required')
    if any(r['kind'] not in KINDS for r in g.values()):
        raise ValueError('unknown kind')
    if any(r['evidence_type'] not in ('invocation', 'process_result', 'outcome_readback', 'missing', 'mixed')
           or r['population'] not in ('synthetic', 'reviewed_real') for r in g.values()):
        raise ValueError('use reviewed aggregate evidence/population aliases only')
    def metric(keys):
        return _verdict_metrics([(g[k]['verdict'], p[k]['verdict']) for k in keys])
    overall = metric(g)
    kinds = {kind: metric([k for k in g if g[k]['kind'] == kind]) for kind in KINDS}
    for m in kinds.values():
        precisions = [m['classes'][v]['precision'] for v in VERDICTS[:2]]
        m['default_kit_reliable'] = (m['n'] >= 10 and all(v is not None and v >= .90 for v in precisions))
    by_evidence = {name: metric([k for k in g if g[k]['evidence_type'] == name])
                   for name in sorted({r['evidence_type'] for r in g.values()})}
    populations = {name: metric([k for k in g if g[k]['population'] == name])
                   for name in sorted({r['population'] for r in g.values()})}
    done_ids = [k for k in g if g[k]['kind'] == 'done']
    false_done = [k for k in done_ids if g[k]['verdict'] == 'contradicted']
    true_done = [k for k in done_ids if g[k]['verdict'] == 'supported']
    done: dict = {'n': len(done_ids), 'proven_false_done_n': len(false_done),
            'false_done_withheld': sum(p[k]['verdict'] != 'supported' for k in false_done),
            'false_done_explicitly_contradicted': sum(p[k]['verdict'] == 'contradicted' for k in false_done),
            'supported_done_n': len(true_done),
            'true_done_withheld': sum(p[k]['verdict'] != 'supported' for k in true_done),
            'true_done_wrongly_contradicted': sum(p[k]['verdict'] == 'contradicted' for k in true_done)}
    for name, denominator in [('false_done_withheld', len(false_done)),
                              ('false_done_explicitly_contradicted', len(false_done)),
                              ('true_done_withheld', len(true_done)),
                              ('true_done_wrongly_contradicted', len(true_done))]:
        done[name + '_rate'] = _ratio(done[name], denominator)
        done[name + '_wilson95'] = wilson(done[name], denominator)
    sp, cp = (overall['classes'][v]['precision'] for v in VERDICTS[:2])
    return {'overall': overall, 'by_kind': kinds, 'by_evidence_type': by_evidence,
            'by_population': populations, 'done_effectiveness': done,
            'verdict_target_met': overall['macro_f1'] >= .85 and sp is not None and sp >= .95
                                  and cp is not None and cp >= .90}


def score_release(gold, predictions, expected_repo_sha):
    """Verifier-export adapter. Seal/export equivalence remains verifier-owned.

    Strict holdout denominators prevent a small fixture from passing as release
    evidence. Synthetic fixtures may exercise the individual score functions.
    """
    if (gold.get('handle') != HANDLE or predictions.get('handle') != HANDLE or
            gold.get('manifest_sha256') != MANIFEST_SHA256 or
            predictions.get('manifest_sha256') != MANIFEST_SHA256):
        raise ValueError('frozen handle/manifest mismatch')
    if (len(expected_repo_sha) != 40 or any(c not in '0123456789abcdef' for c in expected_repo_sha)
            or predictions.get('repo_sha') != expected_repo_sha):
        raise ValueError('exact evaluated commit required')
    if (len(gold['detection']) != 240 or len(gold['verdict']) != 120 or
            any(sum(r['stratum'] == s for r in gold['detection']) != 120
                for s in ('random', 'enriched'))):
        raise ValueError('frozen 120 random/120 enriched/120 verdict denominators required')
    detection = score_detection(gold['detection'], predictions['detection'])
    verdict = score_verdicts(gold['verdict'], predictions['verdict'])
    met = detection['detection_target_met'] and verdict['verdict_target_met']
    return {'handle': HANDLE, 'manifest_sha256': MANIFEST_SHA256,
            'repo_sha': expected_repo_sha, 'detection': detection, 'verdict': verdict,
            'all_numeric_targets_met': met,
            'release_status': 'targets met; independent review required' if met else 'experimental; NOT v2 accepted',
            'scope': 'Verifier-export aggregate; model-adjudicated gold, not human truth. No activation authorized.',
            'interval_limits': 'Wilson 95% binomial P/R intervals; no macro-F1 interval. Repeated synthetic families and bounded two-profile frame limit independence/generalization.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gold', type=Path, required=True, help='Private verifier-export envelope, not auto-discovered labels')
    parser.add_argument('--gold-sha256', required=True, help='Verifier-attested export byte SHA256')
    parser.add_argument('--predictions', type=Path, required=True)
    parser.add_argument('--expected-repo-sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if any(p.resolve().is_relative_to(root) for p in (args.gold, args.predictions, args.output)):
        raise SystemExit('Private evaluation inputs/outputs must remain outside the public repo.')
    gold_raw = args.gold.read_bytes()
    if hashlib.sha256(gold_raw).hexdigest() != args.gold_sha256:
        raise SystemExit('Gold export byte hash mismatch')
    prediction_raw = args.predictions.read_bytes()
    result = score_release(json.loads(gold_raw), json.loads(prediction_raw), args.expected_repo_sha)
    result['gold_export_sha256'] = args.gold_sha256
    result['predictions_sha256'] = hashlib.sha256(prediction_raw).hexdigest()
    # Aggregate JSON contains no case IDs, spans, answers or private host paths.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps({'repo_sha': args.expected_repo_sha, 'release_status': result['release_status']}))


if __name__ == '__main__':
    main()
