"""Run exact committed-source gates in a clean disposable stage, never a live profile.

PYTHONPATH=/path/to/hermes-agent /path/to/hermes-python scripts/release_gates.py
Uses the current interpreter and installed Hermes modules. No install or network.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True):
        raise SystemExit('Commit and clean source first; only HEAD is tested.')
    archive = subprocess.check_output(['git', '-C', str(root), 'archive', 'HEAD'])
    with tempfile.TemporaryDirectory(prefix='receipts-release-gates-') as tmp:
        base = Path(tmp)
        stage, home = base / 'plugin', base / 'home'
        stage.mkdir()
        home.mkdir()
        if base.resolve().is_relative_to(root):
            raise SystemExit('TMPDIR must be outside the scanned source tree.')
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(stage, filter='data')
        env = {**os.environ, 'HERMES_HOME': str(home), 'HOME': str(home),
               'HERMES_RECEIPTS_JEV': '0', 'HERMES_RECEIPTS_STAMP': '0',
               'HERMES_RECEIPTS_GATE': 'observe', 'HERMES_ENABLE_PROJECT_PLUGINS': '0'}
        commands = [
            ('tests', [sys.executable, '-m', 'pytest', '-q', '--import-mode=importlib',
                       '--rootdir=tests', '--confcutdir=tests', 'tests']),
            ('validate', [sys.executable, '-c',
                          'from hermes_cli.plugins_cmd_catalog import cmd_validate; '
                          'cmd_validate(".", as_json=True, install_deps=False)']),
            ('doctor', [sys.executable, '-c',
                        'from hermes_cli.plugins_cmd import cmd_plugin_doctor; '
                        'cmd_plugin_doctor(".", ci=True)']),
        ]
        for name, argv in commands:
            # validate exits the interpreter; doctor MUST be a separate process.
            subprocess.run(argv, cwd=stage, env=env, check=True, timeout=180)
            print(json.dumps({'gate': name, 'repo_sha': sha, 'result': 'PASS'}), flush=True)
    print(json.dumps({'repo_sha': sha, 'all_gates_passed': True, 'scope': 'disposable local stage, not activation'}))


if __name__ == '__main__':
    main()
