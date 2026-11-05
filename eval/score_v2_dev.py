"""Development-only old200 answer-level seven-action detection (NOT holdout/verdict)."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from v2.contracts import Scope
from v2.detect import extract_claims


def score(profiles: Path) -> dict[str, int | float | None]:
    labels = json.loads((Path(__file__).parent / 'labels.json').read_text())['items']
    counts: Counter[tuple[bool, bool]] = Counter()
    for profile, message_id, gold in labels:
        path = profiles / profile / 'state.db'
        # Existing local source DBs only; never create a missing database.
        if not path.is_file():
            raise FileNotFoundError(path)
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.execute('PRAGMA query_only=ON')
            hit = conn.execute('SELECT content, role FROM messages WHERE id=?', (message_id,)).fetchone()
        if hit is None or hit[1] != 'assistant':
            raise RuntimeError(f'missing assistant message id {message_id} for profile {profile}')
        scope = Scope(source_id=profile, profile=profile, session_id='development',
                      user_message_id='development', final_message_id=str(message_id))
        pred = any(claim['kind'] != 'done' for claim in extract_claims(hit[0], scope, {}))
        counts[bool(gold), pred] += 1
    tp, fp, fn, tn = (counts[True, True], counts[False, True],
                      counts[True, False], counts[False, False])
    return {'n': len(labels), 'TP': tp, 'FP': fp, 'FN': fn, 'TN': tn,
            'precision': tp / (tp + fp) if tp + fp else None,
            'recall': tp / (tp + fn) if tp + fn else None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', type=Path, required=True)
    result = score(parser.parse_args().profiles)
    print('old200 development, enriched answer-level seven-action detection only; NOT independent holdout or verdict accuracy')
    print(json.dumps(result, sort_keys=True))
    print('done detection: unknown (old200 has no done gold labels)')
