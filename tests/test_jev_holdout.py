"""Synthetic fixtures only: no holdout labels, seals, or source databases."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from eval import jev_holdout as runner


HANDLE = 'receipts-v2-gold-final-v1'
ROOT = Path(__file__).resolve().parents[1]


def synthetic_case(case_id='synthetic-tests', kind='tests', status=None) -> dict:
    scope = dict(source_id='synthetic', profile='synthetic', session_id='synthetic',
                 user_message_id='synthetic-user', final_message_id='synthetic-final')
    claim = dict(claim_id=case_id + '-claim', scope=scope, kind=kind, span_start=0,
                 span_end=12, target_key=None, task_key=None, project_key=None,
                 detector_version='synthetic')
    evidence = [] if status is None else [dict(
        evidence_id=case_id + '-evidence', scope=scope, call_id='synthetic-call',
        action=kind, target_key=None, artifact_version=None, observed_at=1.0,
        level='process_result', status=status)]
    return dict(case_id=case_id, claim=claim, evidence=evidence,
                messages=[dict(role='assistant', content='Synthetic claim only.')])


def bundle(*cases):
    return dict(schema_version=1, handle=HANDLE, cases=list(cases))


def classifier(choice='supported', ok=True):
    def classify(messages, metadata=None, enabled=False):
        assert enabled is True
        assert metadata['claim']['kind'] in runner.KINDS
        return {'categories': {}, 'jev': {'ok': ok, 'answers': {'verdict': {'type': 'choice', 'choice': choice, 'confidence': 0.5}}}}
    return classify


def test_predictions_use_actual_v2_evidence_and_never_upgrade_unknown_support():
    data = bundle(synthetic_case('synthetic-supported', status='ok'),
                  synthetic_case('synthetic-contradicted', status='error'),
                  synthetic_case('synthetic-unverified'))
    before = copy.deepcopy(data)
    result = runner.predict_cases(data, handle=HANDLE, classifier=classifier())
    rows = result['predictions']
    assert [r['deterministic'] for r in rows] == ['supported', 'contradicted', 'unverified']
    assert [r['jev'] for r in rows] == ['supported'] * 3
    assert [r['combined'] for r in rows] == ['supported', 'contradicted', 'unverified']
    assert rows[2]['experimental_recommendation'] == 'supported'
    assert result['handle'] == HANDLE
    assert data == before
    assert 'messages' not in json.dumps(result)
    assert runner.predict_cases(data, handle=HANDLE, classifier=classifier()) == result


@pytest.mark.parametrize('choice,ok', [('supported', False), ('invented', True), (None, True)])
def test_unusable_jev_fails_closed(choice, ok):
    row = runner.predict_cases(bundle(synthetic_case()), handle=HANDLE,
                               classifier=classifier(choice, ok))['predictions'][0]
    assert row['jev'] == 'not_evaluable'
    assert row['combined'] == 'unverified'
    assert row['jev_status'] == 'unavailable'


def test_scoped_policy_is_forwarded_and_serialized_for_jev():
    from session_classifier import build_digest
    first = synthetic_case('inside', status='ok')
    second = synthetic_case('outside', status='ok')
    first['window'], second['window'] = [0, 2], [10, 20]
    first['links'] = {'artifact_version': 'v1'}
    second['links'] = {'artifact_version': 'v2'}
    captured = []

    def capture(messages, metadata=None, enabled=False):
        captured.append((metadata, build_digest(messages, metadata)))
        return classifier()(messages, metadata=metadata, enabled=enabled)

    runner.predict_cases(bundle(first, second), handle=HANDLE, classifier=capture)
    for case, (meta, digest) in zip((first, second), captured):
        assert meta['window'] == case['window']
        assert meta['links'] == case['links']
        assert 'WINDOW: ' + json.dumps(case['window']) in digest
        assert 'LINKS: ' + json.dumps(case['links']) in digest
        assert 'out-of-window and unbound evidence' in digest
    assert captured[0][1] != captured[1][1]


def test_classifier_exception_is_redacted_and_does_not_destroy_baseline():
    def broken(*args, **kwargs):
        raise RuntimeError('sensitive message must not be exported')
    result = runner.predict_cases(bundle(synthetic_case(status='error')), handle=HANDLE,
                                  classifier=broken)
    assert result['predictions'][0]['combined'] == 'contradicted'
    assert 'sensitive' not in json.dumps(result)


def test_scoring_all_modes_per_kind_with_counts_and_fixed_four_class_macro():
    data = bundle(synthetic_case('synthetic-a', status='ok'),
                  synthetic_case('synthetic-b'), synthetic_case('synthetic-c', kind='push'))
    predictions = runner.predict_cases(data, handle=HANDLE, classifier=classifier('contradicted'))
    # Synthetic test expectations, NOT private holdout annotations.
    labels = {'synthetic-a': 'supported', 'synthetic-b': 'contradicted', 'synthetic-c': 'unverified'}
    result = runner.score_predictions(predictions, labels, handle=HANDLE)
    deterministic = result['modes']['deterministic']
    assert deterministic['overall']['n'] == 3
    assert deterministic['overall']['macro_f1'] == pytest.approx(5 / 12)
    supported = deterministic['overall']['per_verdict']['supported']
    assert supported == dict(tp=1, fp=0, fn=0, tn=2, gold_count=1, predicted_count=1,
                             precision=1.0, recall=1.0, f1=1.0)
    assert deterministic['per_kind']['tests']['n'] == 2
    assert deterministic['per_kind']['push']['n'] == 1
    assert deterministic['per_kind']['done']['macro_f1'] is None
    assert result['modes']['combined'] == result['modes']['deterministic']
    assert 'calibration' not in result


@pytest.mark.parametrize('labels', [{}, {'extra': 'supported'}, {'synthetic-tests': 'invalid'}])
def test_scoring_rejects_partial_extra_or_invalid_labels(labels):
    predictions = runner.predict_cases(bundle(synthetic_case()), handle=HANDLE, classifier=classifier())
    with pytest.raises(ValueError):
        runner.score_predictions(predictions, labels, handle=HANDLE)


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(handle=HANDLE + ' '),
    lambda d: d.update(labels={}),
    lambda d: d['cases'][0].update(gold='supported'),
    lambda d: d['cases'][0]['claim'].update(kind='unknown'),
    lambda d: d['cases'][0]['claim'].update(span_end=-1),
    lambda d: d['cases'][0]['evidence'].append({'status': 'ok'}),
    lambda d: d['cases'].append(copy.deepcopy(d['cases'][0])),
    lambda d: d['cases'][0].update(window=[2, 1]),
])
def test_bad_or_label_bearing_cases_rejected_before_classifier(mutate):
    data = bundle(synthetic_case())
    mutate(data)
    def forbidden(*args, **kwargs):
        pytest.fail('classifier must not run on invalid cases')
    with pytest.raises(ValueError):
        runner.predict_cases(data, handle=HANDLE, classifier=forbidden)


@pytest.mark.parametrize('handle', ['', HANDLE.upper(), HANDLE + ' ', 'another-handle'])
def test_handle_must_be_exact(handle):
    with pytest.raises(ValueError):
        runner.predict_cases(bundle(), handle=handle, classifier=classifier())


def test_classifier_cannot_mutate_prediction_identity_or_kind():
    def mutating(messages, metadata=None, enabled=False):
        metadata['claim']['kind'] = 'push'
        metadata['claim']['claim_id'] = 'tampered'
        return classifier('contradicted')(messages, metadata=metadata, enabled=enabled)
    result = runner.predict_cases(bundle(synthetic_case()), handle=HANDLE, classifier=mutating)
    assert result['predictions'][0]['kind'] == 'tests'


@pytest.mark.parametrize('baseline', runner.VERDICTS)
@pytest.mark.parametrize('proposed', runner.VERDICTS)
def test_all_combination_boundaries(baseline, proposed):
    case = synthetic_case(status={'supported': 'ok', 'contradicted': 'error'}.get(baseline))
    if baseline == 'not_evaluable':
        case['claim']['stream_incomplete'] = True
    row = runner.predict_cases(bundle(case), handle=HANDLE,
                               classifier=classifier(proposed))['predictions'][0]
    expected = baseline
    assert row['deterministic'] == baseline
    assert row['jev'] == proposed
    assert row['combined'] == expected


@pytest.mark.parametrize('location', ['metadata', 'messages'])
def test_nested_label_fields_rejected(location):
    case = synthetic_case()
    if location == 'metadata':
        case['metadata'] = {'nested': [{'gold_label': 'synthetic-only'}]}
    else:
        case['messages'][0]['labels'] = {'synthetic': 'supported'}
    with pytest.raises(ValueError, match='label/seal'):
        runner.predict_cases(bundle(case), handle=HANDLE, classifier=classifier())


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(handle='wrong'),
    lambda d: d.update(schema_version=2),
    lambda d: d['predictions'].append(copy.deepcopy(d['predictions'][0])),
    lambda d: d['predictions'][0].update(kind='unknown'),
    lambda d: d['predictions'][0].update(jev='unknown'),
])
def test_scoring_rejects_malformed_prediction_documents(mutate):
    predictions = runner.predict_cases(bundle(synthetic_case()), handle=HANDLE, classifier=classifier())
    mutate(predictions)
    with pytest.raises(ValueError):
        runner.score_predictions(predictions, {'synthetic-tests': 'unverified'}, handle=HANDLE)


def test_default_classifier_adapter_and_unavailable_backend(monkeypatch):
    monkeypatch.setattr(runner, '_default_classifier', lambda: classifier('contradicted'))
    row = runner.predict_cases(bundle(synthetic_case()), handle=HANDLE)['predictions'][0]
    assert row['jev'] == 'contradicted'
    assert row['combined'] == row['deterministic'] == 'unverified'
    monkeypatch.setattr(runner, '_default_classifier', lambda: None)
    unavailable = runner.predict_cases(bundle(synthetic_case()), handle=HANDLE)['predictions'][0]
    assert unavailable['jev'] == 'not_evaluable'
    assert unavailable['combined'] == 'unverified'


def test_cli_schema_is_machine_readable():
    result = subprocess.run([sys.executable, '-m', 'eval.jev_holdout', '--handle', HANDLE,
                             '--schema'], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == runner.CASES_SCHEMA


def test_empty_batch_scoring_and_schema_documentation():
    predictions = runner.predict_cases(bundle(), handle=HANDLE, classifier=classifier())
    scores = runner.score_predictions(predictions, {}, handle=HANDLE)
    assert scores['modes']['jev']['overall']['macro_f1'] is None
    assert runner.CASES_SCHEMA['additionalProperties'] is False
    assert runner.CASES_SCHEMA['properties']['handle']['const'] == HANDLE


def test_cli_requires_handle_and_rejects_invalid_handle_before_opening_cases(tmp_path):
    missing = tmp_path / 'not-present.json'
    output = tmp_path / 'predictions.json'
    cmd = [sys.executable, '-m', 'eval.jev_holdout', '--cases', str(missing), '--output', str(output)]
    for extra in ([], ['--handle', HANDLE + ' ']):
        result = subprocess.run(cmd + extra, cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 2
        assert 'handle' in result.stderr
        assert 'Traceback' not in result.stderr
        assert not output.exists()


def test_cli_runs_synthetic_batch_and_refuses_to_overwrite_input(tmp_path):
    cases = tmp_path / 'synthetic-cases.json'
    output = tmp_path / 'synthetic-predictions.json'
    cases.write_text(json.dumps(bundle(synthetic_case(status='ok'))))
    # Inject a synthetic classifier into the child too; never invoke a live model.
    harness = ("import sys; from eval import jev_holdout as r; "
               "r._default_classifier=lambda: (lambda *a, **k: "
               "{'jev': {'ok': True, 'answers': {'verdict': {'choice': 'supported'}}}}); "
               "raise SystemExit(r.main(sys.argv[1:]))")
    cmd = [sys.executable, '-c', harness, '--handle', HANDLE, '--cases', str(cases),
           '--output', str(output)]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    document = json.loads(output.read_text())
    assert document['predictions'][0]['deterministic'] == 'supported'
    assert document['predictions'][0]['combined'] == 'supported'
    same = subprocess.run(cmd[:-1] + [str(cases)], cwd=ROOT, capture_output=True, text=True)
    assert same.returncode == 2
    assert json.loads(cases.read_text())['cases']
