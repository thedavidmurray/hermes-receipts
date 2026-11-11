"""Render the "custom shop" printer example from a real session and a config file.

Usage::

    PYTHONPATH=/path/to/hermes-agent python3 scripts/render_custom_shop.py \
        --state-db ~/.hermes/profiles/kilo/state.db --out assets/custom

What it proves, end to end:

* one documented ``receipts.yaml`` (this directory by default) drives paper, theme,
  header/footer, extra fine-print fields, logo, barcode and the USB printer options;
* the run facts (profile, agent, model, provider, turns, tool calls, tokens, cost,
  duration) come from a real Hermes ``state.db`` read read-only, never invented;
* the same receipt prints at 58 mm dot-matrix (with logo) and at 80 mm thermal;
* the criterion is a genuine in-process readback of a real file's bytes.

Run facts that identify the run (profile/agent/session) are pseudonymised by
``to_public_receipt`` before they reach the receipt, so the PNG is safe to share.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from v2.config import load_config  # noqa: E402
from v2.contracts import claim_key  # noqa: E402
from v2.evidence import observe_artifact  # noqa: E402
from v2.fields import metrics_from_state  # noqa: E402
from v2.public import to_public_receipt  # noqa: E402
from v2.render import public_text, render_task_png  # noqa: E402
from v2.tasks import judge_task  # noqa: E402

SCOPE = {'source_id': 'local-render', 'profile': 'kilo', 'session_id': 'local-render',
         'user_message_id': '1', 'final_message_id': '2'}
TARGET = 'a' * 64


def shop_logo(path: Path) -> Path:
    """A small one-colour shop mark; the renderer dithers it to 1-bit ink."""
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGB', (420, 150), '#fbf7ef')
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, 415, 145), outline='#282620', width=4)
    draw.rectangle((4, 44, 415, 108), fill='#282620')
    font = None
    for candidate in ('/System/Library/Fonts/Menlo.ttc',
                      '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, 40)
            break
    text = 'MURRAY & SONS'
    if font is None:  # pragma: no cover - depends on host fonts
        draw.text((24, 54), text, fill='#fbf7ef')
    else:
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((210 - (box[2] - box[0]) / 2, 76 - (box[3] - box[1]) / 2), text,
                  font=font, fill='#fbf7ef')
    image.save(path)
    return path


def build_public(artifact: Path, metrics: dict, now: float) -> dict:
    """A receipt from a genuine artifact readback plus the real session's facts."""
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    requirements = [{'requirement_id': 'artifact', 'public_label': 'Artifact bytes match',
                     'required': True, 'evidence_predicate': 'artifact_digest',
                     'target_key': TARGET, 'artifact_version': digest}]
    claim = {'claim_id': claim_key(SCOPE, 'done', 0, 4, 'deterministic-2.0'), 'scope': SCOPE,
             'kind': 'done', 'task_key': 'custom-shop', 'target_key': TARGET}
    evidence = observe_artifact(artifact, digest, SCOPE, TARGET, digest, observed_at=now)
    private = judge_task(requirements, [evidence], (now - 60, now + 60), claim=claim)
    return to_public_receipt(private, {'artifact': 'Artifact bytes match'}, metrics=metrics)


def default_state_db() -> str:
    """The real Hermes state.db to read run facts from, or '' when none is found.

    ``HOME`` may be a sandbox home under a profile, so every plausible root is tried
    before giving up; the caller can always pass ``--state-db`` explicitly.
    """
    candidates = [os.environ.get('HERMES_RECEIPTS_STATE_DB', '')]
    for root in (os.environ.get('HERMES_HOME', ''),
                 str(Path.home() / '.hermes'),
                 str(Path(__file__).resolve().parents[3] / '.hermes')):
        if root:
            candidates.append(str(Path(root) / 'profiles' / 'kilo' / 'state.db'))
            candidates.append(str(Path(root) / 'state.db'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default=str(ROOT / 'assets' / 'custom'))
    parser.add_argument('--config', default=None, help='receipts.yaml (default: <out>/receipts.yaml)')
    parser.add_argument('--state-db', default=default_state_db(),
                        help='real Hermes state.db to read run facts from (read-only)')
    parser.add_argument('--artifact', default=str(ROOT / 'v2' / 'config.py'),
                        help='real file whose bytes the criterion reads back')
    args = parser.parse_args()
    if not args.state_db:
        raise SystemExit('no state.db found; pass --state-db /path/to/state.db')

    out = Path(args.out).expanduser()
    config_path = Path(args.config).expanduser() if args.config else out / 'receipts.yaml'
    logo = shop_logo(out / 'logo.png')

    metrics = metrics_from_state(Path(args.state_db).expanduser())
    public = build_public(Path(args.artifact), metrics, time.time())
    config = load_config(config_path)
    if config is None:
        raise SystemExit(f'config file not found: {config_path}')

    wide = config.with_paper('80mm')
    png_small = render_task_png(public, out / 'shop-58mm-dot-matrix.png', config=config)
    png_wide = render_task_png(public, out / 'shop-80mm-thermal.png',
                               theme='thermal-classic', config=wide)

    text_small = public_text(public, config=config)
    text_wide = public_text(public, theme='thermal-classic', config=wide)
    (out / 'custom-shop-58mm.txt').write_text(text_small + '\n', encoding='utf-8')
    (out / 'custom-shop-80mm.txt').write_text(text_wide + '\n', encoding='utf-8')

    def shown(value):
        """Repo-relative when the path is inside the checkout, absolute otherwise."""
        text = str(value)
        try:
            return os.path.relpath(text, ROOT)
        except ValueError:  # pragma: no cover - different drive on Windows
            return text

    summary = {
        'source_state_db': str(Path(args.state_db).expanduser()),
        'source_artifact': shown(Path(args.artifact).expanduser()),
        'config_file': shown(config_path),
        'config': {'kit': config.kit, 'theme': config.theme, 'paper': config.paper,
                   'columns': config.columns, 'header': dict(config.header),
                   'footer': dict(config.footer), 'extra_fields': [f.key for f in config.extra_fields],
                   'logo': shown(config.logo), 'barcode': config.barcode,
                   'printer': dict(config.printer)},
        'public_metrics': public['metrics'],
        'completion': public['completion'],
        'receipt_hash': public['receipt_hash'],
        'pngs': {str(p.name): hashlib.sha256(p.read_bytes()).hexdigest()[:16]
                 for p in (png_small, png_wide)},
    }
    (out / 'rendered.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n',
                                       encoding='utf-8')

    print('logo            :', logo)
    print('== 58 mm dot-matrix (currency of the custom shop config) ==')
    print(text_small)
    print('== 80 mm thermal (same receipt, wider paper and the classic theme) ==')
    print(text_wide)
    print('metrics source  :', summary['source_state_db'])
    print('pngs            :', json.dumps(summary['pngs']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
