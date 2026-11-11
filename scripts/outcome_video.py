"""Reuse the shared cut renderer with actual controlled PNGs, never drawn fake receipts.

python scripts/outcome_video.py --generator /path/to/videos/make_cut.py --assets assets/integration
No frame intermediates. The voiceover labels replay, local-only fixture and PNG fallback.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generator', type=Path, required=True)
    parser.add_argument('--assets', type=Path, required=True)
    args = parser.parse_args()
    assets = args.assets.resolve()
    result = json.loads((assets / 'outcome-summary.json').read_text())
    if not result['all_expectations_passed']:
        raise SystemExit('Run actual outcome probe first')
    spec = importlib.util.spec_from_file_location('shared_cuts', args.generator)
    cuts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cuts)
    def png_scene(img, draw, t, sec, beat):
        draw.text((60, 80), beat['title'], font=cuts.font(52, bold=True, sans=True), fill=cuts.WHITE)
        with cuts.Image.open(assets / beat['png']) as original:
            receipt = original.convert('RGB')
            receipt.thumbnail((800, 770))
            img.paste(receipt, (100, 185))
        for i, line in enumerate(beat['side']):
            draw.text((970, 260+i*65), line, font=cuts.font(32, sans=True), fill=cuts.WHITE)
    cuts.SCENES['actual_png'] = png_scene
    storyboard = {'voice': 'Samantha', 'label': 'CONTROLLED FIXTURE REPLAY | Actual probe output, not fleet telemetry | PNG fallback; hardware unverified', 'beats': [
        {'scene': 'title', 'lines': ["Your agent says it's done.", 'Get the receipt.'], 'size': 78,
         'sub': 'Actual local outcome probe; experimental v2',
         'vo': "Your agent says it's done. Get the receipt. This is a replay of an actual controlled local run, not customer telemetry."},
        {'scene': 'actual_png', 'title': 'FAILURE: a done claim without the required push', 'png': 'task-before.png',
         'side': ['Actual bytes: matched digest', 'Actual test: bound to those bytes', 'Remote criterion: missing', 'Done claim: UNVERIFIED'],
         'vo': "The file exists, and the test checks those exact bytes. But the required remote commit evidence is missing. Two criteria are not three. Completion stays unverified."},
        {'scene': 'terminal', 'lines': ['$ git push LOCAL_BARE_REMOTE HEAD:refs/heads/main',
             '$ git --git-dir LOCAL_BARE_REMOTE rev-parse refs/heads/main', '+ exact commit matched',
             '$ observe_remote_commit(...)', '+ outcome readback: ok'],
         'caption': 'Actual commands executed by scripts/outcome_probe.py',
         'vo': "Repair the missing step: push to the local bare remote. Then read the remote reference back independently. A successful wrapper alone is not proof."},
        {'scene': 'actual_png', 'title': 'REPAIR: all three criteria, same version and window', 'png': 'task-after.png',
         'side': ['All required criteria supported', 'Explicit task and target linkage', 'Same artifact version and window', 'Not a global correctness guarantee'],
         'vo': "Now all three required criteria are supported for this task, version, and observation window. The receipt changes. This is scoped evidence, not a guarantee that everything is correct."},
        {'scene': 'actual_png', 'title': 'Statement: completion is separate from action claims', 'png': 'statement.png',
         'side': ['Controlled one-task population', 'Unknown fields stay unknown', 'Cost stays in the footer', f"{result['scenario_count']} outcome scenarios checked"],
         'vo': f"The statement keeps completion separate from action claims. Unknown fields remain unknown. The controlled oracle checked {result['scenario_count']} scenarios, including stale tests, wrong targets, missing pushes, and repairs."},
        {'scene': 'title', 'lines': ['Experimental. Not v2 accepted.', 'Independent holdout still pending.'], 'size': 68,
         'sub': 'PNG fallback only. Physical USB print unverified.',
         'vo': "This is experimental, not an accepted verification release. Independent frozen holdout results are still pending. These are PNG receipts. Physical printing is unverified."}
    ]}
    with tempfile.TemporaryDirectory(prefix='receipt-storyboard-') as temp:
        board = Path(temp) / 'storyboard.json'
        board.write_text(json.dumps(storyboard))
        sys.argv = [str(args.generator), str(board), str(assets / 'outcome-demo.mp4')]
        cuts.main()


if __name__ == '__main__':
    main()
