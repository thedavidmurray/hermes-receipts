"""Fire the real trigger hooks in a disposable HERMES_HOME and render what they print.

This is the card's end-to-end probe, not a mock: it builds a disposable ``HERMES_HOME``
with a real ``state.db`` (a session that ran ``pytest`` and then claimed it passed),
registers the plugin's *actual* hook callbacks through ``register(ctx)``, and calls
them with the kwargs the host fires: the published payload shapes for
``on_session_end`` and the documented kwargs for ``kanban_task_completed`` (see
``HOST_PAYLOADS`` below, and the Observer hooks page of the docs).

Two things are proven per event:

1. **the registered hook callback completes off the hot path** — ``fire()`` returns
   immediately, and the harness then waits for the delivery the background thread
   wrote into the trigger state file (so "never blocks the turn" is measured, not
   asserted in prose);
2. **the receipt is real paper** — a PNG on disk plus the same receipt as 7-bit ASCII,
   both written under ``assets/triggers/`` for the card.

Run it with the Hermes runtime importable::

    PYTHONPATH=~/.hermes/hermes-agent python3 scripts/trigger_harness.py

Nothing here touches a live profile: the whole run lives in a temporary directory and
is deleted at the end (pass ``--keep`` to inspect it).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets' / 'triggers'
PACKAGE = 'hermes_receipts_trigger_harness'
WAIT_SECONDS = 10.0

RECEIPTS_YAML = '''\
schema_version: 2
paper: 80mm
theme: thermal-classic
barcode: hash
triggers:
  defaults: {cooldown: 0, max_per_hour: 30, dedupe: true}
  task_complete: {enabled: true, destinations: {local: true, chat: true}}
  goal_complete: {enabled: true, destinations: {local: true}}
  claim_check: {enabled: true, when: exceptions_only, destinations: {local: true, chat: true}}
  unattended_session_end: {enabled: true, destinations: {local: true, chat: true}}
  interactive_session_end: {enabled: false, when: "min_claims 1"}
  digest: {enabled: true}
include:
  claims: {tests: true, commit: true, push: true, send: false, deploy: true, write: true, run: true}
  experimental: {external: false, jev: false}
  tools: {preset: actions_only}
  redaction: public
  show: {cost: true, tokens: true, model: true}
  max_claims: 12
  evidence: summary
'''

STATE_DB = '''\
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
  tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL);
CREATE TABLE sessions(id TEXT PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER,
  estimated_cost_usd REAL, model TEXT, profile_name TEXT, started_at REAL, goal_mode INTEGER);
'''

#: The kwargs the host passes to each hook, from the published contract: the synthetic
#: payloads in ``hermes_cli/hooks.py`` and the Observer hooks page of the docs. Copied
#: deliberately — the harness must not import a private core module.
HOST_PAYLOADS: dict[str, dict] = {
    'on_session_end': {'session_id': 'test-session', 'task_id': 'test-task',
                       'turn_id': 'test-turn', 'completed': True, 'failed': False,
                       'interrupted': False, 'turn_exit_reason': 'text_response(stop)',
                       'model': 'gpt-4', 'platform': 'cli'},
    'kanban_task_completed': {'task_id': 'test-task', 'board': 'edgeless', 'assignee': 'kilo',
                              'run_id': 1, 'profile_name': 'kilo', 'summary': 'done'},
    'transform_llm_output': {'session_id': 'test-session', 'turn_id': 'test-turn',
                             'response_text': ''},
}


def load_plugin():
    """Import the plugin exactly as the host does: a package with submodule search."""
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / '__init__.py',
                                                  submodule_search_locations=[str(ROOT)])
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise SystemExit('cannot import the plugin package')
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def seed_state_db(home: Path, *, goal: bool = False) -> Path:
    import sqlite3
    path = home / 'state.db'
    with sqlite3.connect(path) as conn:
        conn.executescript(STATE_DB)
        conn.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
                     ('local-0f2a1c9e5b7d4a6f', 6120, 1840, 0.0413, 'gpt-5.3-codex', 'kilo',
                      time.time() - 96, 1 if goal else 0))
        conn.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', [
            (1, 'local-0f2a1c9e5b7d4a6f', 'user', 'Ship the trigger lane', None, None, None,
             time.time() - 96),
            (2, 'local-0f2a1c9e5b7d4a6f', 'assistant', '', None, json.dumps([{'id': 'call_1',
                'function': {'name': 'terminal',
                             'arguments': json.dumps({'command': 'python3 -m pytest -q tests'})}}]),
             None, time.time() - 90),
            (3, 'local-0f2a1c9e5b7d4a6f', 'tool',
             json.dumps({'exit_code': 0, 'output': '285 passed'}), 'call_1', None, 'terminal',
             time.time() - 88),
            (4, 'local-0f2a1c9e5b7d4a6f', 'assistant', '', None, json.dumps([{'id': 'call_2',
                'function': {'name': 'terminal',
                             'arguments': json.dumps({'command': 'git -c user.name=Kilo push origin kilo/t_9390d1b3-triggers'})}}]),
             None, time.time() - 88),
            (5, 'local-0f2a1c9e5b7d4a6f', 'tool',
             json.dumps({'exit_code': 0, 'output': 'ok'}), 'call_2', None, 'terminal',
             time.time() - 87),
            (6, 'local-0f2a1c9e5b7d4a6f', 'assistant',
             'Shipped it: all 285 tests pass and the branch is pushed to origin.',
             None, None, None, time.time() - 60),
        ])
    return path


def recording_context():
    """The smallest object ``register()`` needs; records the real callbacks it registers."""
    class Context:
        def __init__(self):
            self.hooks: dict[str, list] = {}
            self.commands: list = []
            self.cli_commands: list = []

        def register_hook(self, name, callback):
            self.hooks.setdefault(name, []).append(callback)

        def register_command(self, *args, **kwargs):
            self.commands.append(args[:1])

        def register_cli_command(self, *args, **kwargs):
            self.cli_commands.append(args[:1])

    return Context()


def host_payload(event: str) -> dict:
    """The hook kwargs the host fires, authored here rather than imported from core.

    The shapes are the published contract (``hermes_cli/hooks.py`` synthetic payloads
    and the Observer hooks page of the docs); the harness must not import a private
    core module, so the keys live here and each case overrides the identity it needs.
    """
    return dict(HOST_PAYLOADS.get(event) or {})


def wait_for_delivery(state_file: Path, trigger: str, deadline: float,
                      after: int = 0) -> dict | None:
    """The background thread's own record that it finished: proof it is off the hot path."""
    while time.time() < deadline:
        try:
            rows = json.loads(state_file.read_text()).get('deliveries') or []
        except (OSError, ValueError):
            rows = []
        for row in rows[after:]:
            if row.get('trigger') == trigger and row.get('destination') == 'local':
                return row
        time.sleep(0.05)
    return None


def ascii_receipt(public: dict, config) -> str:
    from importlib import import_module
    render = import_module(f'{PACKAGE}.v2.render')
    return render.public_text(public, theme='ascii', config=config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-agent', default=str(Path.home() / '.hermes' / 'hermes-agent'))
    parser.add_argument('--keep', action='store_true', help='keep the temporary home')
    parser.add_argument('--assets', default=str(ASSETS))
    args = parser.parse_args()

    if str(args.hermes_agent) not in sys.path:
        sys.path.insert(0, str(args.hermes_agent))

    plugin = load_plugin()
    from importlib import import_module
    triggers = import_module(f'{PACKAGE}.v2.triggers')
    config_mod = import_module(f'{PACKAGE}.v2.config')

    base = Path(tempfile.mkdtemp(prefix='receipts-triggers-'))
    home, cfg = base / 'home', base / 'cfg'
    home.mkdir(), cfg.mkdir()
    os.environ['HERMES_HOME'] = str(home)
    os.environ['HERMES_RECEIPTS_CONFIG_DIR'] = str(cfg)
    os.environ.pop('HERMES_KANBAN_DB', None)
    (cfg / 'receipts.yaml').write_text(RECEIPTS_YAML)
    seed_state_db(home)

    context = recording_context()
    plugin.register(context)
    registered = sorted(context.hooks)
    print(json.dumps({'step': 'register', 'hooks': registered}), flush=True)

    config = config_mod.load_config()
    if config is None:  # pragma: no cover - defensive
        raise SystemExit('the harness config did not load')
    out = Path(args.assets)
    out.mkdir(parents=True, exist_ok=True)
    facts: dict = {'hooks_registered': registered, 'config_source': config.source,
                   'receipts': {}, 'async_proof': {}}

    session = 'local-0f2a1c9e5b7d4a6f'
    shipped = 'Shipped it: all 285 tests pass and the branch is pushed to origin.'
    cases = [
        # (file stem, trigger, hook, payload, expected to fire, expected to render)
        ('task_complete', 'task_complete', 'kanban_task_completed',
         {**host_payload('kanban_task_completed'), 'task_id': 't_9390d1b3', 'board': 'edgeless',
          'assignee': 'kilo', 'run_id': 50471, 'profile_name': 'kilo', 'platform': 'discord',
          'summary': shipped}, True, True),
        ('unattended_session_end', 'unattended_session_end', 'on_session_end',
         {**host_payload('on_session_end'), 'session_id': session, 'platform': 'cron',
          'turn_id': 6, 'completed': True, 'interrupted': False}, True, True),
        # exceptions_only: an unbacked deploy claim is exactly what prints a receipt
        ('claim_check_caught', 'claim_check', 'transform_llm_output',
         {'response_text': 'Deployed to production and all 285 tests pass.',
          'session_id': session, 'platform': 'discord'}, True, True),
        # ... and a fully backed answer prints nothing at all
        ('claim_check_clean', 'claim_check', 'transform_llm_output',
         {'response_text': 'All 285 tests pass.', 'session_id': session,
          'platform': 'discord'}, False, False),
    ]

    for label, trigger, hook, payload, expect_fire, render in cases:
        # 1. the hot path: the registered callback returns at once
        before = len(json.loads(triggers.state_path().read_text()).get('deliveries', [])) \
            if triggers.state_path().is_file() else 0
        started = time.time()
        for callback in context.hooks.get(hook, []):
            callback(**payload)
        hot_ms = (time.time() - started) * 1000.0
        delivered = wait_for_delivery(triggers.state_path(), trigger, time.time() + WAIT_SECONDS,
                                      after=before)
        facts['async_proof'][label] = {'hook': hook, 'callback_returned_ms': round(hot_ms, 3),
                                       'background_delivery': bool(delivered),
                                       'expected_fire': expect_fire,
                                       'waited_seconds': WAIT_SECONDS}
        if not render:
            continue
        # 2. the same receipt, rendered deterministically for the card
        evidence = triggers.read_evidence(payload.get('session_id'))
        spec = config.triggers.spec(trigger)
        source = payload.get('summary') or payload.get('response_text') or evidence.answer
        claims = triggers.match_evidence(source, evidence)
        decision = triggers.decide(triggers.TriggerEvent(trigger, f'{label}-harness', text=source,
                                                        session_id=session),
                                   config.triggers, claims=claims)
        rendered = triggers.render_event(
            triggers.TriggerEvent(trigger, f'{label}-harness', text=source,
                                  session_id=payload.get('session_id'), platform='discord',
                                  profile='kilo', detail='harness'),
            claims, evidence, spec=spec, include=config.include, base_config=config)
        png = out / f'{label}.png'
        from importlib import import_module as _import
        _import(f'{PACKAGE}.v2.render').render_task_png(rendered.public, png,
                                                        theme=spec.theme or config.theme,
                                                        config=config)
        text = ascii_receipt(rendered.public, config)
        (out / f'{label}.txt').write_text(text + '\n')
        facts['receipts'][label] = {
            'trigger': trigger, 'hook': hook, 'payload_keys': sorted(payload),
            'decision': {'fire': decision.fire, 'reason': decision.reason},
            'completion': rendered.public['completion'],
            'criteria': [{'claim_kind': c.get('claim_kind'), 'verdict': c.get('verdict')}
                         for c in rendered.public['criteria']],
            'hidden_claims': rendered.public.get('hidden_claims'),
            'png': str(png), 'png_sha256': hashlib.sha256(png.read_bytes()).hexdigest()[:16],
            'ascii_lines': len(text.splitlines()),
        }

    facts['digest_cron'] = triggers.cron_line(config)
    facts['triggers'] = config.triggers.table()
    facts['include'] = config.include.describe()
    facts['state_file'] = str(triggers.state_path())
    # Every case must do exactly what its trigger's `when` says: the ones that fire
    # deliver, and the clean claim_check answer delivers nothing at all.
    mismatched = {label: row for label, row in facts['async_proof'].items()
                  if row['background_delivery'] != row['expected_fire']}
    facts['mismatched'] = sorted(mismatched)
    ok = not mismatched
    (out / 'rendered.json').write_text(json.dumps(facts, indent=2, sort_keys=True) + '\n')
    print(json.dumps(facts, indent=2, sort_keys=True))

    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)
    else:
        print(f'kept: {base}')
    print(json.dumps({'harness': 'ok' if ok else 'FAILED',
                      'off_hot_path': ok, 'assets': str(out)}))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
