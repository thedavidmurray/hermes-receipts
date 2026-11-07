"""Blind verifier-side v2 verdict experiment; never discovers labels or seals.

Prediction CLI (the handle is an opaque identifier, NOT a credential):
    python -m eval.jev_holdout --handle receipts-v2-gold-final-v1 \
        --cases /verifier/unlabelled.json --output /verifier/predictions.json
    python -m eval.jev_holdout --handle receipts-v2-gold-final-v1 --schema

CASES_SCHEMA below is the explicit JSON Schema (draft 2020-12). Each case is
ONE already-extracted claim, its native v2 Evidence list, and the messages the
verifier permits Jev to see. Required envelope: schema_version=1, exact handle,
cases=[{case_id, claim, evidence, messages}]. Optional metadata is classifier
context only; optional window=[start,end] and links are passed to judge_claims.
Use separate case IDs when one turn contains multiple claims. The classifier
receives metadata.claim and metadata.case_id for the particular claim. It must
return that claim's verdict, not an aggregate session success judgement.

No evidence is inferred from prose and no outcome probes are run. The caller
must supply evidence, scope, target/version bindings, and observation boundaries
from its verifier export. No source DB, profile, label path, or seal is opened.
Unknown fields at the envelope/case/claim/evidence boundary are rejected; label
and seal fields are rejected recursively, including in metadata/messages.

predict_cases(..., classifier=callable) accepts the classify_session interface.
By default it lazily loads session_classifier (or v2.session_classifier). Missing,
failed, disabled, or malformed Jev results become not_evaluable, never support.
Raw Jev choices remain experimental; combined preserves deterministic support
and contradiction and never upgrades unverified to supported. not_evaluable is
also retained rather than interpreting missing observations as a verdict.

Scoring is deliberately NOT a CLI command. Only the verifier should call:
    score_predictions(predictions, {case_id: verdict, ...}, handle=HANDLE)
Labels must exactly cover the prediction IDs. No partial scoring or joins to
private files occur. Macro-F1 uses all four fixed verdict classes (absent-class
F1=0); empty subsets have macro_f1=None. Undefined precision/recall are None.
Counts and precision/recall/F1 are reported for every verdict, including support
and contradiction, overall and per kind, for all three modes. No confidence
scores or calibration claims are produced. Synthetic unit fixtures are NOT
holdout results. An exact handle validates identity, not verifier authorization;
the caller is responsible for keeping labels outside the builder environment.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
from pathlib import Path
from typing import Callable

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from v2.evidence import judge_claims

HANDLE = 'receipts-v2-gold-final-v1'
VERDICTS = ('supported', 'contradicted', 'unverified', 'not_evaluable')
KINDS = ('tests', 'commit', 'push', 'send', 'deploy', 'write', 'run', 'done')
MODES = ('deterministic', 'jev', 'combined')
_STRING = {'type': 'string', 'minLength': 1}
_NULLABLE_STRING = {'type': ['string', 'null']}


def _object(properties, optional=()):
    return {'type': 'object', 'properties': properties,
            'required': [key for key in properties if key not in optional],
            'additionalProperties': False}


_SCOPE = _object({key: _STRING for key in (
    'source_id', 'profile', 'session_id', 'user_message_id', 'final_message_id')})
_CLAIM = _object({
    'claim_id': _STRING, 'scope': _SCOPE, 'kind': {'enum': list(KINDS)},
    'span_start': {'type': 'integer', 'minimum': 0},
    'span_end': {'type': 'integer', 'minimum': 0},
    'target_key': _NULLABLE_STRING, 'task_key': _NULLABLE_STRING,
    'project_key': _NULLABLE_STRING, 'detector_version': _STRING,
    'stream_incomplete': {'type': 'boolean'}, 'external': {'type': 'boolean'},
}, optional=('stream_incomplete', 'external'))
_EVIDENCE = _object({
    'evidence_id': _STRING, 'scope': _SCOPE, 'call_id': _STRING, 'action': _STRING,
    'target_key': _NULLABLE_STRING, 'artifact_version': _NULLABLE_STRING,
    'observed_at': {'type': 'number'},
    'level': {'enum': ['invocation', 'process_result', 'outcome_readback']},
    'status': {'enum': ['ok', 'error', 'unknown']},
})
_CASE = _object({
    'case_id': _STRING, 'claim': _CLAIM,
    'evidence': {'type': 'array', 'items': _EVIDENCE},
    'messages': {'type': 'array', 'items': {'type': 'object'}},
    'metadata': {'type': 'object'},
    'window': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 2, 'maxItems': 2},
    'links': _object({'task_key': _NULLABLE_STRING, 'target_key': _NULLABLE_STRING,
                      'artifact_version': _NULLABLE_STRING},
                     optional=('task_key', 'target_key', 'artifact_version')),
}, optional=('metadata', 'window', 'links'))
CASES_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'Verifier-exported unlabelled Receipts v2 Jev cases',
    **_object({'schema_version': {'type': 'integer', 'const': 1},
               'handle': {'type': 'string', 'const': HANDLE},
               'cases': {'type': 'array', 'items': _CASE}}),
}


def _handle(handle):
    if handle != HANDLE:
        raise ValueError('handle must be exactly ' + HANDLE)


def _validate(value, schema, path='$'):
    """Validate the subset of JSON Schema used by CASES_SCHEMA, without deps."""
    types = schema.get('type')
    if types:
        types = [types] if isinstance(types, str) else types
        matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
                   'string': isinstance(value, str), 'integer': type(value) is int,
                   'number': type(value) in (int, float), 'boolean': type(value) is bool,
                   'null': value is None}
        if not any(matches[t] for t in types):
            raise ValueError(f'{path}: incorrect type')
    if 'const' in schema and value != schema['const']:
        raise ValueError(f'{path}: incorrect constant')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path}: unsupported value')
    if type(value) in (int, float):
        if not math.isfinite(value) or value < schema.get('minimum', -math.inf):
            raise ValueError(f'{path}: invalid number')
    if isinstance(value, str) and len(value) < schema.get('minLength', 0):
        raise ValueError(f'{path}: empty string')
    if isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', math.inf):
            raise ValueError(f'{path}: incorrect array length')
        for i, item in enumerate(value):
            _validate(item, schema.get('items', {}), f'{path}[{i}]')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if set(schema.get('required', ())) - value.keys():
            raise ValueError(f'{path}: missing required fields')
        if schema.get('additionalProperties') is False and value.keys() - properties.keys():
            raise ValueError(f'{path}: unexpected fields (unlabelled input only)')
        for key, item in value.items():
            if key.lower() in {'gold', 'label', 'labels', 'seal', 'seals', 'gold_label',
                               'gold_labels', 'gold_verdict', 'expected_verdict'}:
                raise ValueError(f'{path}: label/seal fields forbidden')
            _validate(item, properties.get(key, {}), f'{path}.{key}')


def validate_cases(document: dict, *, handle: str) -> None:
    _handle(handle)
    _validate(document, CASES_SCHEMA)
    seen = set()
    for case in document['cases']:
        if case['case_id'] in seen:
            raise ValueError('duplicate case_id')
        seen.add(case['case_id'])
        if case['claim']['span_start'] > case['claim']['span_end']:
            raise ValueError('claim span must be ordered')
        if 'window' in case and case['window'][0] >= case['window'][1]:
            raise ValueError('window must be a nonempty half-open interval')


def _default_classifier():
    for name in ('session_classifier', 'v2.session_classifier'):
        try:
            return importlib.import_module(name).classify_session
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
    return None


def _combine(deterministic, proposed):
    # Same policy as session_classifier.combine_verdict: until independent gold
    # establishes lift, judgments are advisory and do not change outcome verdicts.
    return deterministic


def predict_cases(document: dict, *, handle: str, classifier: Callable | None = None) -> dict:
    """Blind predictions only; injected classifier receives a defensive JSON copy."""
    validate_cases(document, handle=handle)
    # Also reject non-JSON objects/NaN in otherwise unconstrained message context.
    cases = json.loads(json.dumps(document['cases'], allow_nan=False))
    if classifier is None and cases:
        try:
            classifier = _default_classifier()
        except Exception:
            classifier = None
    rows = []
    for case in cases:
        finding = judge_claims([case['claim']], case['evidence'],
                               window=tuple(case['window']) if 'window' in case else None,
                               links=case.get('links'))[0]
        proposed, status = 'not_evaluable', 'unavailable'
        if classifier is not None:
            try:
                context = json.loads(json.dumps(
                    {**case.get('metadata', {}), 'claim': case['claim'], 'evidence': case['evidence'],
                     'case_id': case['case_id'], 'window': case.get('window'),
                     'links': case.get('links')}, allow_nan=False))
                result = classifier(case['messages'], metadata=context, enabled=True)
                jev = result['jev']
                answer = jev['answers']['verdict']
                choice, confidence = answer.get('choice'), answer.get('confidence')
                if (jev.get('ok') is True and answer.get('type') == 'choice'
                        and choice in VERDICTS and type(confidence) in (int, float)
                        and math.isfinite(confidence) and 0 <= confidence <= 1):
                    proposed, status = choice, 'ok'
            except Exception:
                # Never export exceptions: classifiers may embed private message text.
                pass
        deterministic = finding['verdict']
        rows.append({'case_id': case['case_id'], 'kind': case['claim']['kind'],
                     'deterministic': deterministic, 'jev': proposed,
                     'combined': _combine(deterministic, proposed), 'jev_status': status,
                     'experimental_recommendation': proposed if status == 'ok' else None})
    return {'schema_version': 1, 'handle': handle, 'predictions': rows,
            'policy': 'blind-verdicts-v1; experimental Jev; no support upgrades'}


def _metrics(rows, labels, mode):
    n = len(rows)
    per_verdict = {}
    for verdict in VERDICTS:
        tp = sum(labels[r['case_id']] == verdict and r[mode] == verdict for r in rows)
        fp = sum(labels[r['case_id']] != verdict and r[mode] == verdict for r in rows)
        fn = sum(labels[r['case_id']] == verdict and r[mode] != verdict for r in rows)
        per_verdict[verdict] = {
            'tp': tp, 'fp': fp, 'fn': fn, 'tn': n - tp - fp - fn,
            'gold_count': tp + fn, 'predicted_count': tp + fp,
            'precision': tp / (tp + fp) if tp + fp else None,
            'recall': tp / (tp + fn) if tp + fn else None,
            'f1': 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
        }
    return {'n': n, 'macro_f1': sum(v['f1'] for v in per_verdict.values()) / len(VERDICTS)
            if n else None, 'per_verdict': per_verdict}


def score_predictions(predictions: dict, labels: dict[str, str], *, handle: str) -> dict:
    """Verifier-only in-memory scoring; never reads labels from disk or resolves handle."""
    _handle(handle)
    if not isinstance(predictions, dict) or predictions.get('handle') != handle:
        raise ValueError('prediction handle mismatch')
    if predictions.get('schema_version') != 1 or not isinstance(predictions.get('predictions'), list):
        raise ValueError('invalid prediction document')
    rows = predictions['predictions']
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('case_id'), str) or not row['case_id']:
            raise ValueError('invalid prediction row')
        if row['case_id'] in ids or row.get('kind') not in KINDS:
            raise ValueError('duplicate case_id or invalid kind')
        ids.add(row['case_id'])
        if any(row.get(mode) not in VERDICTS for mode in MODES):
            raise ValueError('invalid predicted verdict')
    if not isinstance(labels, dict) or set(labels) != ids or any(v not in VERDICTS for v in labels.values()):
        raise ValueError('labels must exactly cover prediction case IDs with valid verdicts')
    return {'schema_version': 1, 'handle': handle, 'macro_f1_classes': list(VERDICTS),
            'modes': {mode: {'overall': _metrics(rows, labels, mode),
                             'per_kind': {kind: _metrics([r for r in rows if r['kind'] == kind],
                                                        labels, mode) for kind in KINDS}}
                      for mode in MODES}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--handle', required=True, choices=[HANDLE])
    parser.add_argument('--cases', type=Path, help='Explicit verifier-exported unlabelled JSON only')
    parser.add_argument('--output', type=Path, help='Prediction JSON (never a label file)')
    parser.add_argument('--schema', action='store_true', help='Print case JSON Schema and exit')
    args = parser.parse_args(argv)
    if args.schema:
        if args.cases or args.output:
            parser.error('--schema cannot be combined with --cases or --output')
        print(json.dumps(CASES_SCHEMA, indent=2, sort_keys=True))
        return 0
    if args.cases is None or args.output is None:
        parser.error('--cases and --output are required for prediction')
    if args.cases.resolve() == args.output.resolve():
        parser.error('--output must not overwrite --cases')
    try:
        document = json.loads(args.cases.read_text(encoding='utf-8'))
        result = predict_cases(document, handle=args.handle)
        # Exclusive creation prevents accidental overwrites of any verifier asset.
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n')
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f'prediction failed ({type(exc).__name__}); check unlabelled case schema and output path')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
