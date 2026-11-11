"""Synthetic scoring plumbing, never frozen holdout scores."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('release_eval', Path(__file__).resolve().parents[1] / 'eval/release_eval.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def span(a, b, kind='write'):
    return dict(kind=kind, span_start=a, span_end=b)


def test_maximum_matching_not_greedy():
    assert m.match_spans([span(0, 4), span(5, 8)], [span(0, 8), span(0, 2)]) == 2
    assert m.match_spans([span(0, 4)], [span(4, 8), span(0, 2, 'done')]) == 0
    assert m.match_spans([span(0, 4)], [span(0, 4), span(1, 3)]) == 1


def test_binary_intervals_and_undefined():
    x = m.binary_counts([(True, True), (True, False), (False, True), (False, False)])
    assert [x[k] for k in ('TP', 'FP', 'FN', 'TN')] == [1, 1, 1, 1]
    assert x['precision'] == x['recall'] == .5
    assert x['precision_wilson95'][0] < .5 < x['precision_wilson95'][1]
    assert m.binary_counts([])['precision'] is None
    with pytest.raises(ValueError): m.binary_counts([(1, True)])


def test_detection_separates_done_and_strata_and_omits_ids():
    gold = [dict(case_id='PRIVATE_A', stratum='random', claims=[span(0, 4)]),
            dict(case_id='PRIVATE_B', stratum='enriched', claims=[span(0, 4, 'done')])]
    pred = [dict(case_id='PRIVATE_A', claims=[]), dict(case_id='PRIVATE_B', claims=[span(0, 4, 'done')])]
    x = m.score_detection(gold, pred)
    assert x['random']['seven_action']['answer']['FN'] == 1
    assert x['random']['done']['answer']['TN'] == 1
    assert x['enriched']['done']['answer']['TP'] == 1
    assert x['detection_target_met'] is False
    assert 'PRIVATE' not in str(x)
    with pytest.raises(ValueError): m.score_detection(gold, pred[:1])
    with pytest.raises(ValueError): m.score_detection(gold, pred + pred[:1])


def test_verdict_four_way_abstention_done_and_kind_limits():
    gold = [dict(case_id=str(i), verdict=v, kind='done', evidence_type='outcome_readback', population='synthetic')
            for i, v in enumerate(m.VERDICTS)]
    predictions = [dict(case_id=str(i), verdict=v) for i, v in enumerate(m.VERDICTS)]
    x = m.score_verdicts(gold, predictions)
    assert x['overall']['macro_f1'] == 1
    assert x['overall']['abstention_rate'] == .5
    assert x['done_effectiveness']['false_done_explicitly_contradicted'] == 1
    assert x['by_kind']['done']['default_kit_reliable'] is False
    assert x['by_kind']['deploy']['macro_f1'] is None
    abstain = [dict(case_id=str(i), verdict='unverified') for i in range(4)]
    y = m.score_verdicts(gold, abstain)
    assert not y['verdict_target_met']
    assert y['done_effectiveness']['true_done_withheld'] == 1
    assert y['done_effectiveness']['true_done_wrongly_contradicted'] == 0
    assert y['done_effectiveness']['false_done_withheld'] == 1
    with pytest.raises(ValueError): m.score_verdicts(gold, predictions[:1])
    assert x['done_effectiveness']['false_done_withheld_rate'] == 1
    assert x['done_effectiveness']['false_done_withheld_wilson95'][0] < 1


def _envelopes():
    # Deliberately generated fixtures: NOT sealed gold or product performance.
    detection = [dict(case_id=f'SYNTHETIC_D{i}', stratum='random' if i < 120 else 'enriched', claims=[])
                 for i in range(240)]
    verdict = [dict(case_id=f'SYNTHETIC_V{i}', verdict=m.VERDICTS[i % 4], kind=m.KINDS[i % 8],
                    evidence_type='missing', population='synthetic') for i in range(120)]
    gold = dict(handle=m.HANDLE, manifest_sha256=m.MANIFEST_SHA256, detection=detection, verdict=verdict)
    pred = dict(handle=m.HANDLE, manifest_sha256=m.MANIFEST_SHA256, repo_sha='a'*40,
                detection=[dict(case_id=r['case_id'], claims=[]) for r in detection],
                verdict=[dict(case_id=r['case_id'], verdict='unverified') for r in verdict])
    return gold, pred


def test_release_requires_denominators_identity_and_exact_commit():
    import copy
    gold, pred = _envelopes()
    report = m.score_release(gold, pred, 'a'*40)
    assert report['release_status'] == 'experimental; NOT v2 accepted'
    assert not report['all_numeric_targets_met']
    assert 'SYNTHETIC_' not in str(report)
    assert set(report['detection']['random']['by_kind_answer']) == set(m.KINDS)
    for key in ('handle', 'manifest_sha256', 'repo_sha'):
        wrong = {**pred, key: 'bad'}
        with pytest.raises(ValueError): m.score_release(gold, wrong, 'a'*40)
    with pytest.raises(ValueError): m.score_release({**gold, 'verdict': gold['verdict'][:-1]}, pred, 'a'*40)
    wrong = copy.deepcopy(pred)
    wrong['verdict'][-1]['case_id'] = wrong['verdict'][0]['case_id']
    with pytest.raises(ValueError): m.score_release(gold, wrong, 'a'*40)


def test_scoring_cli_private_hash_guard_and_exclusive_output(tmp_path):
    import hashlib
    import json
    import subprocess
    import sys
    gold, pred = _envelopes()
    gp, pp, output = tmp_path / 'gold.json', tmp_path / 'pred.json', tmp_path / 'aggregate.json'
    gp.write_text(json.dumps(gold))
    pp.write_text(json.dumps(pred))
    cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / 'eval/release_eval.py'), '--gold', str(gp), '--predictions', str(pp),
           '--expected-repo-sha', 'a'*40, '--output', str(output), '--gold-sha256']
    bad = subprocess.run([*cmd, '0'*64], capture_output=True, text=True)
    assert bad.returncode and not output.exists() and 'hash mismatch' in bad.stderr
    digest = hashlib.sha256(gp.read_bytes()).hexdigest()
    subprocess.run([*cmd, digest], capture_output=True, check=True)
    assert output.stat().st_mode & 0o777 == 0o600
    assert 'SYNTHETIC_' not in output.read_text()
    again = subprocess.run([*cmd, digest], capture_output=True)
    assert again.returncode != 0
