import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_predict', ROOT / 'eval/release_predict.py')
m = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(ROOT))
spec.loader.exec_module(m)


def test_blind_predictions_preserve_done_and_reject_labels():
    scope = dict(source_id='s', profile='p', session_id='s', user_message_id='1', final_message_id='2')
    data = {'handle': m.HANDLE, 'detection': [{'case_id': 'd1', 'answer': 'Done.', 'scope': scope}],
            'verdict': [{'case_id': 'v1', 'claim': {'claim_id': 'c', 'kind': 'done', 'scope': scope,
               'task_key': 'task', 'target_key': 'a'*64}, 'evidence': [], 'window': [0, 10]}]}
    result = m.predict(data)
    assert result['detection'][0]['claims'][0]['kind'] == 'done'
    assert result['verdict'] == [{'case_id': 'v1', 'verdict': 'unverified'}]
    assert 'answer' not in str(result) and 'scope' not in str(result)
    data['detection'][0]['label'] = True
    with pytest.raises(ValueError): m.predict(data)


def test_actual_outcome_probe_all_variants_and_public_pngs(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts/outcome_probe.py'), '--output', str(tmp_path)],
                          capture_output=True, text=True, check=True)
    import json
    result = json.loads(proc.stdout)
    assert result['scenario_count'] == 13
    assert result['all_expectations_passed'] and result['idempotent_receipt_and_store']
    assert result['scenarios']['absent_push']['completion'] == 'unverified'
    assert result['scenarios']['failed_then_repaired']['completion'] == 'supported'
    for name in ('task-before.png', 'task-after.png', 'statement.png', 'pane-unverified.png'):
        with Image.open(tmp_path / name) as image:
            image.load()
            assert image.width == 652 and not image.info
        text = subprocess.check_output(['tesseract', str(tmp_path / name), 'stdout'], text=True)
        assert 'CANARY' not in text and '/Users/' not in text
