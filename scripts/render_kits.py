"""Render the kit x theme matrix from a real session's telemetry.

For every requested case (``unverified``, ``supported``, ``contradicted``) and every
kit x theme pair this writes a public receipt PNG under ``assets/kits/`` plus one
contact sheet.

What is real here, and what is not:

* the **criteria evidence** is a real local readback - a real file's SHA256 and a
  real bare git remote's ref list - performed by ``v2.evidence``;
* the **fine print** is a real Hermes session's telemetry, read read-only from a
  ``state.db`` (``v2.telemetry``);
* the receipt itself is a **public** receipt: session/profile/agent appear only as
  ``sha256-<12>`` pseudonyms, so nothing private lands in a committed asset.

Usage::

    python3 scripts/render_kits.py [--out assets/kits] [--db <state.db>] [--session-id <id>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2.contracts import claim_key  # noqa: E402
from v2.detect import DETECTOR_VERSION  # noqa: E402
from v2.evidence import observe_artifact, observe_remote_commit  # noqa: E402
from v2.fields import plugin_provenance  # noqa: E402
from v2.public import to_public_receipt, verify_receipt_hash  # noqa: E402
from v2.render import receipt_lines, render_task_png  # noqa: E402
from v2.tasks import judge_task  # noqa: E402
from v2.telemetry import facts_from_state  # noqa: E402

KITS = ('minimal', 'verify', 'full')
THEMES = ('thermal-classic', 'ascii', 'dot-matrix')
CASES = ('unverified', 'supported', 'contradicted')
EXTRA_THEMES = ('minimal-mono', 'bold-branded')
LABELS = {'artifact': 'Artifact bytes match', 'commit': 'Commit present in remote'}
SCOPE = {'source_id': 'render-kits-fixture', 'profile': 'public-fixture',
         'session_id': 'public-fixture', 'user_message_id': '1', 'final_message_id': '2'}
TARGET = hashlib.sha256(b'render-kits-target').hexdigest()


def _git(*argv, cwd):
    return subprocess.run(['git', *argv], cwd=cwd, capture_output=True, text=True, check=True,
                          env={'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin',
                               'HOME': str(cwd), 'GIT_CONFIG_NOSYSTEM': '1',
                               'GIT_AUTHOR_NAME': 'receipts', 'GIT_AUTHOR_EMAIL': 'r@example.invalid',
                               'GIT_COMMITTER_NAME': 'receipts', 'GIT_COMMITTER_EMAIL': 'r@example.invalid'})


def _fixtures(root: Path) -> dict:
    """A real artifact file and a real bare remote holding a real commit."""
    work = root / 'fixture-work'
    work.mkdir(parents=True, exist_ok=True)
    artifact = work / 'artifact.bin'
    artifact.write_bytes(b'controlled demo artifact bytes\n')
    bare = root / 'remote.git'
    if not bare.is_dir():
        _git('init', '--quiet', '--bare', str(bare), cwd=work)
    if not (work / '.git').is_dir():
        _git('init', '--quiet', '.', cwd=work)
        _git('add', 'artifact.bin', cwd=work)
        _git('commit', '--quiet', '-m', 'controlled demo commit', cwd=work)
        _git('push', '--quiet', str(bare), 'HEAD:refs/heads/main', cwd=work)
    commit = _git('rev-parse', 'HEAD', cwd=work).stdout.strip()
    return {'artifact': artifact, 'digest': hashlib.sha256(artifact.read_bytes()).hexdigest(),
            'bare': bare, 'commit': commit}


def _evidence(case: str, fixtures: dict) -> tuple[list, str]:
    """Real readbacks only. The contradicted case really reads mismatching bytes."""
    version = fixtures['digest']
    now = time.time()
    if case == 'unverified':
        return [], version
    artifact = observe_artifact(fixtures['artifact'], fixtures['digest'], SCOPE, TARGET, version,
                                observed_at=now)
    if case == 'contradicted':
        # A real read of the declared version that really does not match the declared bytes.
        return [observe_artifact(fixtures['artifact'], hashlib.sha256(b'other bytes').hexdigest(),
                                 SCOPE, TARGET, version, observed_at=now)], version
    remote = observe_remote_commit(fixtures['bare'], fixtures['commit'], SCOPE, TARGET, version,
                                   observed_at=now)
    return [artifact, remote], version


def _private(case: str, fixtures: dict, observed: list, version: str) -> dict:
    requirements = [
        {'requirement_id': 'artifact', 'public_label': LABELS['artifact'], 'required': True,
         'evidence_predicate': 'artifact_digest', 'target_key': TARGET, 'artifact_version': version},
        {'requirement_id': 'commit', 'public_label': LABELS['commit'], 'required': True,
         'evidence_predicate': 'remote_commit', 'target_key': TARGET, 'artifact_version': version},
    ]
    if case == 'supported':
        # The supported case proves the required set can be met in full.
        requirements = [requirements[0], requirements[1]]
    claim = {'claim_id': claim_key(SCOPE, 'done', 0, 4, DETECTOR_VERSION), 'scope': SCOPE,
             'kind': 'done', 'target_key': TARGET, 'task_key': 'render-kits-demo'}
    window = (time.time() - 3600, time.time() + 60)
    return judge_task(requirements, observed, window, claim=claim)


def _hermes_roots() -> list[Path]:
    """Candidate Hermes homes: ``HERMES_HOME``, ``~/.hermes`` and any ancestor ``.hermes``."""
    roots = []
    configured = os.environ.get('HERMES_HOME')
    if configured:
        roots.append(Path(configured).expanduser())
    home = Path.home()
    roots.append(home / '.hermes')
    roots.append(home)
    for ancestor in home.parents:
        if ancestor.name == '.hermes':
            roots.append(ancestor)
        if (ancestor / '.hermes').is_dir():
            roots.append(ancestor / '.hermes')
    unique: list[Path] = []
    for root in roots:
        if root.is_dir() and root not in unique:
            unique.append(root)
    return unique


def _metrics(db: str | None, session_id: str | None) -> tuple[dict, str]:
    """Real telemetry when a state.db is readable; an explicit note when it is not."""
    candidates: list[tuple[Path, str | None]] = []
    if db:
        candidates.append((Path(db).expanduser(), session_id))
    else:
        for root in _hermes_roots():
            candidates.extend((path, session_id) for path in sorted((root / 'profiles').glob('*/state.db')))
            candidates.append((root / 'state.db', session_id))
    for path, wanted in candidates:
        if not path.is_file():
            continue
        try:
            metrics = facts_from_state(path, session_id=wanted)
        except (OSError, ValueError):
            continue
        if metrics:
            label = f'{path} (profile={metrics.get("profile")}, session={metrics.get("session")})'
            return metrics, label
    return {}, 'no readable state.db: every fine-print field renders n/a'


def _font(size: int, bold: bool = False):
    for name in ('/System/Library/Fonts/Menlo.ttc',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'):
        if Path(name).is_file():
            return ImageFont.truetype(name, size=size, index=1 if bold and name.endswith('.ttc') else 0)
    return ImageFont.load_default(size=size)


def _caption(draw, text, x, y, bold=False):
    draw.text((x, y), text, font=_font(20, bold), fill='#111111')


def _wrap_px(text: str, font, max_width: int) -> list[str]:
    """Wrap to a pixel width, using the real font metrics."""
    lines: list[str] = []
    current = ''
    for word in text.split():
        trial = f'{current} {word}'.strip()
        if not current or font.getlength(trial) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def contact_sheet(paths: list[tuple[str, Path]], out: Path, note: str) -> Path:
    """One PNG overview: kit columns x theme rows, one clearly separated band per row."""
    thumb_width, gap, label_bar, margin, separator = 240, 18, 30, 24, 26
    columns, rows = list(KITS), list(THEMES)
    resample = getattr(Image, 'Resampling', Image).LANCZOS
    loaded = {}
    for key, path in paths:
        with Image.open(path) as image:
            image.load()
            ratio = thumb_width / image.width
            loaded[key] = image.convert('RGB').resize((thumb_width, int(image.height * ratio)), resample)
    width = margin * 2 + len(columns) * (thumb_width + gap) - gap
    title_font, note_font = _font(20, True), _font(17)
    text_width = width - margin * 2
    title_lines = _wrap_px(
        'HERMES / RECEIPTS - kits x themes, contradicted case. Columns = kits ('
        + ', '.join(columns) + '); rows = themes (' + ', '.join(rows) + ').', title_font, text_width)
    note_lines = _wrap_px(note, note_font, text_width)
    header = 16 + len(title_lines) * 26 + 8
    footer = 24 + len(note_lines) * 24
    row_heights = [max(label_bar + loaded.get(f'contradicted-{kit}-{theme}',
                                             Image.new('RGB', (1, 1))).height
                       for kit in columns) for theme in rows]
    height = header + sum(row_heights) + separator * len(rows) + footer
    sheet = Image.new('RGB', (width, height), '#ffffff')
    draw = ImageDraw.Draw(sheet)
    for index, line in enumerate(title_lines):
        draw.text((margin, 12 + index * 26), line, font=title_font, fill='#111111')
    y = header
    for row, theme in enumerate(rows):
        draw.rectangle((margin - 8, y - 10, width - margin + 8, y + row_heights[row]),
                       fill='#f2f2ef', outline='#cccccc')
        for column, kit in enumerate(columns):
            image = loaded.get(f'contradicted-{kit}-{theme}')
            if image is None:
                continue
            x = margin + column * (thumb_width + gap)
            _caption(draw, f'{kit} / {theme}', x, y + 4)
            sheet.paste(image, (x, y + label_bar))
        y += row_heights[row] + separator
    for index, line in enumerate(note_lines):
        draw.text((margin, y + 12 + index * 24), line, font=note_font, fill='#333333')
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, format='PNG', optimize=True)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default=str(ROOT / 'assets' / 'kits'))
    parser.add_argument('--db', help='Hermes state.db to read telemetry from (read-only)')
    parser.add_argument('--session-id', help='session to read telemetry for')
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    metrics, telemetry_source = _metrics(args.db, args.session_id)
    provenance = plugin_provenance()
    print(f'plugin: {provenance.get("plugin_version")} / {provenance.get("git_sha")}')
    print(f'telemetry source: {telemetry_source}')
    print('telemetry keys: ' + json.dumps(sorted(metrics)))

    written: list[tuple[str, Path]] = []
    with tempfile.TemporaryDirectory(prefix='render-kits-') as temp:
        fixtures = _fixtures(Path(temp))
        for case in CASES:
            observed, version = _evidence(case, fixtures)
            private = _private(case, fixtures, observed, version)
            for kit in KITS:
                for theme in THEMES:
                    public = to_public_receipt(private, LABELS, kit=kit, theme=theme, metrics=metrics)
                    if not verify_receipt_hash(public):
                        raise SystemExit('receipt hash did not verify')
                    name = f'{case}-{kit}-{theme}.png'
                    lines = receipt_lines(public, kit=kit, theme=theme)
                    completion = next(line.text for line in lines
                                      if line.text.startswith('COMPLETION:'))
                    path = render_task_png(public, out / name, kit=kit, theme=theme)
                    written.append((f'{case}-{kit}-{theme}', path))
                    print(f'{name:38} {completion:28} lines={len(lines)}')
            if case == 'supported':
                for theme in EXTRA_THEMES:
                    public = to_public_receipt(private, LABELS, kit='full', theme=theme, metrics=metrics)
                    path = render_task_png(public, out / f'supported-full-{theme}.png', kit='full', theme=theme)
                    written.append((f'supported-full-{theme}', path))
                    print(f'supported-full-{theme}.png (extra theme)')

    sheet = contact_sheet(written, out / 'contact-sheet.png',
                          f'{len(written)} receipts in this directory; criteria are real local readbacks, '
                          f'fine print is real session telemetry ({provenance.get("git_sha")}).')
    print(f'contact sheet: {sheet}')
    print('Cases: unverified = no readback in window; supported = artifact + remote ref read back; '
          'contradicted = real read of mismatching bytes.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
