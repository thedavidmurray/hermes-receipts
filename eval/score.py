"""Re-score hand-labelled final answers from local profile state.db files.

No answer text is shipped in the repository. Missing historical messages fail closed.
Usage: python3 eval/score.py --profiles /path/to/profiles
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from receipt_core import extract_claims


def score(profiles: Path):
    labels = json.loads((Path(__file__).parent / 'labels.json').read_text())
    counts = Counter()
    for profile, message_id, gold in labels['items']:
        path = profiles / profile / 'state.db'
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            hit = conn.execute('SELECT content, role FROM messages WHERE id=?', (message_id,)).fetchone()
        finally:
            conn.close()
        if hit is None or hit[1] != 'assistant':
            raise RuntimeError(f'missing assistant message {profile}:{message_id}')
        pred = bool(extract_claims(hit[0]))
        counts[bool(gold), pred] += 1
    tp, fp, fn, tn = (counts[True, True], counts[False, True],
                      counts[True, False], counts[False, False])
    print(f'n={len(labels["items"])} TP={tp} FP={fp} FN={fn} TN={tn}')
    print(f'precision={tp / (tp + fp):.4f} recall={tp / (tp + fn):.4f}')
    print('scope: binary detection of explicit past action claims, not verdict correctness')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--profiles', type=Path, required=True)
    score(parser.parse_args().profiles)
