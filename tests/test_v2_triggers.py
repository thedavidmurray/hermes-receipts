"""Lane: automatic triggers and include/exclude settings (never the verdict).

Covers every default Hive proposed, every per-trigger override, dedupe, cooldown,
quiet hours, profile/platform deny lists, ``exceptions_only``, the derived
goal/unattended events, the failure-injection path (a raising renderer must not break
the turn), and the ``include:`` settings — including the invariant that no include
configuration can move the verdict block.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('receipt_trigger_package', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

from receipt_trigger_package.v2 import fields, include as include_mod, triggers
from receipt_trigger_package.v2.config import ConfigError, EXAMPLE_CONFIG, parse_config
from receipt_trigger_package.v2.render import public_text
from receipt_trigger_package.v2.triggers import (DEFAULT_SPECS, DEFAULT_TRIGGER_CONFIG, TRIGGERS,
                                                 Destinations, TriggerError, TriggerEvent,
                                                 TriggerState, decide, events_for_hook, execute)
from receipt_trigger_package.v2.include import IncludeConfig, IncludeError, parse_include

ANSWER = 'All tests pass. Pushed to main.'


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every test gets its own config dir and HERMES_HOME, so nothing leaks either way."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_RECEIPTS_CONFIG_DIR', str(tmp_path / 'cfg'))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    fields.reload()
    yield home
    fields.reload()


def _state_db(home: Path, *, goal: bool = False, answer: str = ANSWER) -> Path:
    """A real, minimal Hermes state.db: one turn that ran pytest successfully."""
    path = home / 'state.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT,
          role TEXT, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
          timestamp REAL);
          CREATE TABLE sessions(id TEXT PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER,
          estimated_cost_usd REAL, model TEXT, profile_name TEXT, started_at REAL, goal_mode INTEGER);''')
        conn.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
                     ('s1', 1000, 250, 0.0312, 'gpt-5.3-codex', 'kilo', 10, 1 if goal else 0))
        conn.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', [
            (1, 's1', 'user', 'Ship it', None, None, None, 10),
            (2, 's1', 'assistant', '', None, json.dumps([{'id': 'c1', 'function': {
                'name': 'terminal', 'arguments': json.dumps({'command': 'pytest -q'})}}]), None, 11),
            (3, 's1', 'tool', json.dumps({'exit_code': 0, 'output': '245 passed'}), 'c1', None,
             'terminal', 12),
            (4, 's1', 'assistant', answer, None, None, None, 13),
        ])
    return path


def _config(**raw):
    """A TriggerConfig from a raw mapping: the same path receipts.yaml takes."""
    return triggers.parse_triggers(raw.get('triggers') if 'triggers' in raw else None,
                                   'receipts.yaml')


def _spec(name: str, **override):
    return triggers.parse_triggers({name: override}, 'receipts.yaml').spec(name)


def _run(hook, payload, config=None, include=None, tmp_path=None, **kwargs):
    return execute(hook, payload, background=False,
                   config=config or DEFAULT_TRIGGER_CONFIG,
                   include=include or IncludeConfig(), **kwargs)


# ── defaults ───────────────────────────────────────────────────────────────
def test_every_default_hive_proposed_is_implemented():
    """The nine triggers, their enabled flags, their `when` and their destinations."""
    table = {spec.name: spec for spec in DEFAULT_TRIGGER_CONFIG.specs.values()}
    assert set(table) == set(TRIGGERS)
    assert [name for name in TRIGGERS if table[name].enabled] == [
        'on_demand', 'task_complete', 'goal_complete', 'claim_check', 'unattended_session_end']
    assert table['claim_check'].when == 'exceptions_only'
    assert table['interactive_session_end'].when == 'min_claims'
    assert table['interactive_session_end'].min_claims == 1
    assert table['on_demand'].dedupe is False
    assert table['claim_check'].stamp is False
    assert table['digest'].enabled is False
    # A fresh install is quiet: only the request itself replies in chat.
    assert table['on_demand'].destinations.chat is True
    chatting = [name for name in TRIGGERS if table[name].destinations.chat]
    assert chatting == ['on_demand']
    assert all(table[name].destinations.local for name in TRIGGERS)


def test_defaults_table_names_the_real_hook_and_the_derivation():
    for row in DEFAULT_TRIGGER_CONFIG.table():
        assert row['hook'] and row['derived']
    table = {row['name']: row for row in DEFAULT_TRIGGER_CONFIG.table()}
    assert table['task_complete']['hook'] == 'kanban_task_completed'
    assert 'goal_mode' in table['goal_complete']['derived'] and \
        'no goal-loop hook' in table['goal_complete']['derived']
    assert 'no cron hook' in table['unattended_session_end']['derived']
    assert 'never self-schedules' in table['digest']['hook'] and \
        '--cron' in table['digest']['derived']


def test_triggers_reject_an_unknown_key_by_name():
    with pytest.raises(TriggerError, match=r'triggers\.task_complete\.frobnicate'):
        triggers.parse_triggers({'task_complete': {'frobnicate': 1}}, 'receipts.yaml')
    with pytest.raises(TriggerError, match='unknown trigger'):
        triggers.parse_triggers({'task_complet': {'enabled': True}}, 'receipts.yaml')
    with pytest.raises(TriggerError, match=r'triggers\.task_complete\.when'):
        triggers.parse_triggers({'task_complete': {'when': 'sometimes'}}, 'receipts.yaml')
    with pytest.raises(TriggerError, match=r'triggers\.task_complete\.quiet_hours'):
        triggers.parse_triggers({'task_complete': {'quiet_hours': '25:00-07:00'}})
    with pytest.raises(TriggerError, match='unknown kit'):
        triggers.parse_triggers({'task_complete': {'kit': 'nope'}})
    with pytest.raises(TriggerError, match='unknown theme'):
        triggers.parse_triggers({'task_complete': {'theme': 'nope'}})


def test_a_printer_destination_requires_device_ids():
    with pytest.raises(TriggerError, match='needs device'):
        triggers.parse_triggers({'on_demand': {'destinations': {'printer': True}}})
    spec = _spec('on_demand', destinations={'printer': True, 'device': [0x04b8, 0x0202]})
    assert spec.destinations.device == (0x04b8, 0x0202)
    assert _spec('on_demand', destinations={'local': True, 'chat': True}).destinations.chat


# ── overrides, one per configurable knob ───────────────────────────────────
def test_every_knob_is_configurable():
    spec = triggers.parse_triggers({'defaults': {'kit': 'full', 'theme': 'dot-matrix',
                                                 'paper': '58mm'},
                                    'task_complete': {'enabled': True, 'when': 'min_claims 3',
                                                      'cooldown': 90, 'max_per_hour': 4,
                                                      'quiet_hours': '22:00-07:00',
                                                      'dedupe': False, 'stamp': True,
                                                      'kit': 'cost', 'theme': 'ascii',
                                                      'paper': '80mm',
                                                      'profiles': {'allow': ['kilo']},
                                                      'platforms': {'deny': ['cli']},
                                                      'destinations': {'local': True,
                                                                       'chat': True,
                                                                       'file': '/tmp/r.png'}}},
                                   'receipts.yaml').spec('task_complete')
    assert (spec.enabled, spec.when, spec.min_claims) == (True, 'min_claims', 3)
    assert (spec.kit, spec.theme, spec.paper, spec.columns) == ('cost', 'ascii', '80mm', 48)
    assert (spec.cooldown, spec.max_per_hour, spec.dedupe, spec.stamp) == (90, 4, False, True)
    assert spec.quiet_hours == (22 * 60, 7 * 60)
    assert spec.profiles_allow == ('kilo',) and spec.platforms_deny == ('cli',)
    assert spec.destinations.file == '/tmp/r.png'
    # inherits the shared defaults it did not override
    assert _spec('digest', quiet_hours=None).quiet_hours is None


def test_when_accepts_every_documented_spelling():
    assert _spec('task_complete', when='always').when == 'always'
    assert _spec('task_complete', when='exceptions_only').when == 'exceptions_only'
    assert _spec('task_complete', when='min_claims 4').min_claims == 4
    assert _spec('task_complete', when='min_claims').min_claims == 1
    assert _spec('task_complete', when={'min_claims': 2}).min_claims == 2
    assert _spec('task_complete', min_claims=5).min_claims == 5


# ── events: the derivation table ───────────────────────────────────────────
def test_task_complete_is_derived_from_the_real_kanban_hook():
    events = events_for_hook('kanban_task_completed', {
        'task_id': 't_1', 'board': 'edgeless', 'assignee': 'kilo', 'run_id': 9,
        'profile_name': 'kilo', 'summary': 'shipped'})
    assert [event.trigger for event in events] == ['task_complete']
    assert events[0].version == 't_1:9'
    assert events[0].text == 'shipped'


def test_goal_complete_is_derived_from_a_goal_card_and_a_goal_session(tmp_path, monkeypatch):
    card = events_for_hook('kanban_task_completed', {'task_id': 't_1', 'run_id': 1,
                                                     'goal_mode': True, 'summary': 'done'})
    assert [event.trigger for event in card] == ['task_complete', 'goal_complete']
    # and from a real goal session, read through the sessions.goal_mode column
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = _state_db(tmp_path, goal=False)
    plain = events_for_hook('on_session_end', {'session_id': 's1', 'platform': 'cli'})
    assert [event.trigger for event in plain] == ['interactive_session_end']
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE sessions SET goal_mode=1')
    ends = events_for_hook('on_session_end', {'session_id': 's1', 'platform': 'cli'})
    assert [event.trigger for event in ends] == ['interactive_session_end', 'goal_complete']
    # a card that cannot be read is never promoted to a goal
    assert [event.trigger for event in events_for_hook(
        'kanban_task_completed', {'task_id': 't_missing', 'run_id': 1})] == ['task_complete']


def test_unattended_and_interactive_session_ends_are_separated():
    cron = events_for_hook('on_session_end', {'session_id': 's', 'platform': 'cron'})
    assert [event.trigger for event in cron] == ['unattended_session_end']
    assert cron[0].unattended is True
    cli = events_for_hook('on_session_end', {'session_id': 's', 'platform': 'cli'})
    assert [event.trigger for event in cli] == ['interactive_session_end']
    key = events_for_hook('on_session_end', {'session_key': 'cron-nightly', 'platform': 'gateway'})
    assert [event.trigger for event in key] == ['unattended_session_end']
    custom = events_for_hook('on_session_end', {'platform': 'scheduler'},
                             ('scheduler',))
    assert custom[0].trigger == 'unattended_session_end'


def test_no_hook_and_no_event_can_fire_the_digest():
    """The digest never self-schedules: no hook derives it, and the cron line is printed."""
    for hook in triggers.REGISTERED_HOOKS:
        assert all(event.trigger != 'digest' for event in events_for_hook(hook, {}))
    off = triggers.cron_line(_config())
    assert 'OFF' in off and 'nothing scheduled' in off
    on = triggers.cron_line(_config(triggers={'digest': {'enabled': True}}))
    assert on.startswith('# receipts digest: enabled') and '0 8 * * *' in on


def test_agent_loop_stopped_records_and_never_prints(tmp_path):
    state = TriggerState(path=tmp_path / 'state.json')
    outcomes = execute('agent_loop_stopped', {'session_key': 'local-1', 'reason': '/stop'},
                       background=False, state=state)
    assert outcomes == []
    assert state.data['stopped']['local-1']['reason'] == '/stop'


# ── decisions ──────────────────────────────────────────────────────────────
def _matched(verdict: str, kind: str = 'tests') -> list[dict]:
    return [{'kind': kind, 'text': 'all tests pass', 'verdict': verdict, 'evidence': 'x'}]


def test_disabled_trigger_never_fires():
    decision = decide(TriggerEvent('task_complete', 'v1'),
                      _config(triggers={'task_complete': {'enabled': False}}))
    assert decision.fire is False and decision.reason == 'disabled'


def test_exceptions_only_fires_only_on_unestablished_claims():
    config = _config(triggers={'claim_check': {'enabled': True, 'when': 'exceptions_only'}})
    supported = decide(TriggerEvent('claim_check', 'v1'), config, claims=_matched('✓'))
    assert supported.fire is False and 'supported' in supported.reason
    for verdict in ('✗', 'not observed'):
        decision = decide(TriggerEvent('claim_check', 'v1'), config, claims=_matched(verdict))
        assert decision.fire is True and 'unestablished' in decision.reason


def test_min_claims_counts_claims_without_reading_the_database():
    config = _config(triggers={'interactive_session_end': {'enabled': True,
                                                           'when': 'min_claims 2'}})
    one = decide(TriggerEvent('interactive_session_end', 'v1', text='All tests pass.'), config)
    assert one.fire is False and 'min_claims 2' in one.reason
    two = decide(TriggerEvent('interactive_session_end', 'v1',
                              text='All tests pass. Pushed to main.'), config)
    assert two.fire is True and len(two.claims) == 2


def test_profile_and_platform_allow_deny():
    deny_profile = _config(triggers={'task_complete': {'enabled': True,
                                                       'profiles': {'deny': ['draft']}}})
    assert decide(TriggerEvent('task_complete', 'v', profile='draft'), deny_profile).fire is False
    assert decide(TriggerEvent('task_complete', 'v', profile='kilo'), deny_profile).fire is True
    allow_platform = _config(triggers={'task_complete': {'enabled': True,
                                                         'platforms': {'allow': ['discord']}}})
    assert decide(TriggerEvent('task_complete', 'v', platform='cli'), allow_platform).fire is False
    assert decide(TriggerEvent('task_complete', 'v', platform='discord'), allow_platform).fire


def test_dedupe_cooldown_and_hourly_cap(tmp_path):
    state = TriggerState(path=tmp_path / 'state.json')
    config = _config(triggers={'task_complete': {'enabled': True}})
    event = TriggerEvent('task_complete', 't:1')
    now = time.time()
    assert decide(event, config, state=state, now=now).fire is True
    state.record('task_complete', 'task_complete|t:1')
    again = decide(event, config, state=state, now=now + 1)
    assert again.fire is False and 'already printed' in again.reason
    # a different run of the same trigger is not deduped
    assert decide(TriggerEvent('task_complete', 't:2'), config, state=state, now=now + 1).fire

    cooled = _config(triggers={'task_complete': {'enabled': True, 'cooldown': 60}})
    fresh = TriggerState(path=tmp_path / 'cool.json')
    fresh.record('task_complete', 'task_complete|t:0')
    blocked = decide(TriggerEvent('task_complete', 't:3'), cooled, state=fresh, now=now)
    assert blocked.fire is False and 'cooldown 60s' in blocked.reason
    assert decide(TriggerEvent('task_complete', 't:3'), cooled, state=fresh,
                  now=now + 61).fire

    capped = _config(triggers={'task_complete': {'enabled': True, 'max_per_hour': 2,
                                                 'dedupe': False}})
    hourly = TriggerState(path=tmp_path / 'cap.json')
    for index in range(2):
        assert decide(TriggerEvent('task_complete', f'run{index}'), capped, state=hourly,
                      now=now).fire
        hourly.record('task_complete', f'task_complete|run{index}')
    over = decide(TriggerEvent('task_complete', 'run9'), capped, state=hourly, now=now)
    assert over.fire is False and 'max_per_hour 2' in over.reason


def test_quiet_hours_suppress_a_trigger_that_would_otherwise_fire():
    config = _config(triggers={'task_complete': {'enabled': True, 'quiet_hours': '22:00-07:00'}})
    from datetime import datetime, timezone
    night = datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc).timestamp()
    day = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp()
    assert decide(TriggerEvent('task_complete', 'v'), config, now=night).fire is False
    assert decide(TriggerEvent('task_complete', 'v'), config, now=night).reason == 'quiet hours'
    assert decide(TriggerEvent('task_complete', 'v'), config, now=day).fire is True


# ── execution: real receipts, real PNGs ────────────────────────────────────
def test_task_complete_renders_a_png_and_a_receipt(_isolated, tmp_path):
    _state_db(_isolated)
    config = _config(triggers={'task_complete': {'enabled': True,
                                                 'destinations': {'local': True, 'chat': True}}})
    outcomes = _run('kanban_task_completed', {'task_id': 't_1', 'run_id': 3, 'profile_name': 'kilo',
                                              'summary': 'Shipped it: all tests pass.',
                                              'platform': 'discord'}, config)
    assert len(outcomes) == 1 and outcomes[0]['fired'] is True
    png = Path(outcomes[0]['png'])
    assert png.read_bytes().startswith(b'\x89PNG\r\n') and Image.open(png).width > 100
    assert 'COMPLETION: ' in outcomes[0]['chat'] and outcomes[0]['chat'].startswith('```')
    assert outcomes[0]['deliveries'] == ['local', 'chat']


def test_session_end_receipt_uses_the_answer_it_ended(_isolated):
    _state_db(_isolated)
    config = _config(triggers={'unattended_session_end': {'enabled': True},
                               'interactive_session_end': {'enabled': True, 'when': 'min_claims 1'}})
    cron = _run('on_session_end', {'session_id': 's1', 'platform': 'cron', 'turn_id': 4}, config)
    assert cron[0]['fired'] is True and cron[0]['claims'] == 2
    quiet = _run('on_session_end', {'session_id': 's1', 'platform': 'cli', 'turn_id': 9},
                 config, evidence=triggers.read_evidence('s1', 4))
    # an empty chat is not a receipt: min_claims 1 holds for the interactive trigger
    empty = _run('on_session_end', {'session_id': 'nope', 'platform': 'cli'}, config)
    assert empty[0]['fired'] is False
    assert quiet[0]['claims'] == 2


def test_a_raising_renderer_fails_open(_isolated, monkeypatch):
    """The turn must survive any trigger failure: the hook path swallows and logs it."""
    _state_db(_isolated)
    config = _config(triggers={'task_complete': {'enabled': True}})
    monkeypatch.setattr(triggers, 'render_event', lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('renderer exploded')))
    outcomes = _run('kanban_task_completed', {'task_id': 't', 'run_id': 1, 'summary': 'done'},
                    config)
    assert outcomes[0]['fired'] is False and 'renderer exploded' in outcomes[0]['error']


def test_hooks_registered_by_the_plugin_fail_open_and_accept_var_kwargs():
    registered = {}

    class Ctx:
        def register_hook(self, name, callback):
            registered.setdefault(name, []).append(callback)

        def register_command(self, *a, **k):
            pass

        def register_cli_command(self, *a, **k):
            pass

    plugin.register(Ctx())
    for hook in ('kanban_task_completed', 'kanban_task_blocked', 'on_session_end', 'subagent_stop',
                 'transform_llm_output', 'post_llm_call', 'agent_loop_stopped'):
        callbacks = registered[hook]
        assert callbacks
        for callback in callbacks:
            assert 'kwargs' in callback.__code__.co_varnames and callback.__code__.co_flags
    handler = registered['kanban_task_blocked'][-1]
    assert handler() is None          # no payload, no exception
    assert handler(task_id='t', reason='stuck') is None


def test_fire_never_raises_even_when_the_engine_does(monkeypatch):
    monkeypatch.setattr(triggers, 'execute', lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('boom')))
    assert triggers.fire('kanban_task_completed', task_id='t', summary='x') is None
    assert triggers.fire('transform_llm_output', response_text='all tests pass') is None


def test_the_stamp_is_one_line_and_off_by_default(_isolated):
    _state_db(_isolated)
    default = _config()
    assert default.spec('claim_check').stamp is False
    assert triggers.fire('transform_llm_output', response_text='All tests pass.') is None
    with_stamp = _config(triggers={'claim_check': {'enabled': True, 'stamp': True}})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv('HERMES_RECEIPTS_CONFIG_DIR', str(_isolated))
    try:
        assert with_stamp.spec('claim_check').stamp is True
        stamped = _stamp_with(with_stamp)
        assert stamped is not None and stamped.count('\n') == 2 and 'claim(s) seen' in stamped
    finally:
        monkeypatch.undo()


def _stamp_with(config) -> str | None:
    """The stamp path with an explicit config, so the test never re-reads a path."""
    state = TriggerState()
    state.note_delivery({'destination': 'local', 'detail': '/tmp/x.png', 'session': 's1'})
    state.save()
    original = triggers.load_trigger_config
    triggers.load_trigger_config = lambda *a, **k: (config, IncludeConfig())
    try:
        return triggers.fire('transform_llm_output', response_text='All tests pass.',
                             session_id='s1')
    finally:
        triggers.load_trigger_config = original


def test_the_deadline_stops_a_slow_trigger(_isolated, monkeypatch):
    _state_db(_isolated)
    config = _config(triggers={'task_complete': {'enabled': True}})
    clock = {'t': 1000.0}

    def slow(*args, **kwargs):
        clock['t'] += 5.0
        return 'slow'

    monkeypatch.setattr(triggers, '_now', lambda: clock['t'])
    monkeypatch.setattr(triggers, 'match_evidence', slow)
    outcomes = _run('kanban_task_completed', {'task_id': 't', 'run_id': 1, 'summary': 'done'},
                    config)
    assert outcomes[0]['fired'] is False and outcomes[0]['reason'] == 'deadline exceeded'


def test_triggers_make_no_network_calls_by_construction():
    source = Path(triggers.__file__).read_text()
    for forbidden in ('import socket', 'import urllib', 'import requests', 'http.client'):
        assert forbidden not in source
    assert 'print_receipt' in source  # the one opt-in local device path


def test_state_file_is_private_only_to_the_history_store(_isolated, tmp_path):
    _state_db(_isolated)
    private = parse_include({'redaction': 'private'})
    config = _config(triggers={'task_complete': {'enabled': True,
                                                 'destinations': {'local': True, 'chat': True}}})
    raw = 'local-0123456789abcdef0123456789abcdef'
    outcomes = _run('kanban_task_completed', {'task_id': 't', 'run_id': 1, 'summary': 'Shipped it.',
                                              'session_id': raw, 'platform': 'discord'},
                    config, include=private)
    assert outcomes[0]['fired'] is True
    assert raw not in outcomes[0]['chat']      # the reply is always public
    # `private` keeps the raw id for the local history store, and nowhere else
    public = triggers.session_public(TriggerEvent('task_complete', 'v', session_id=raw),
                                     [], triggers.Evidence(session_id=raw, available=True,
                                                           metrics={'session': raw}), )
    assert private.apply(public, )['metrics']['session'] == raw
    assert private.for_destination('chat').apply(public)['metrics']['session'].startswith('sha256-')


# ── include / exclude ──────────────────────────────────────────────────────
def _public(claims=None, **overrides):
    claims = claims if claims is not None else [
        {'kind': 'tests', 'text': 'all tests pass', 'verdict': '✓', 'evidence': 'terminal #c1 (ok)'},
        {'kind': 'push', 'text': 'pushed to main', 'verdict': '✗', 'evidence': 'no matching tool call observed'},
        {'kind': 'send', 'text': 'sent the report', 'verdict': 'not observed',
         'evidence': 'outside this agent\'s tool stream', 'external': True},
    ]
    event = TriggerEvent('task_complete', 't:1', session_id='s1')
    evidence = triggers.Evidence(rows=(), answer=ANSWER, session_id='s1', available=True,
                                 metrics={'session': 's1', 'model': 'gpt-5.3-codex',
                                          'cost_usd': 0.02, 'tokens_in': 10, 'tokens_total': 20,
                                          'tool_calls': 12, 'duration_seconds': 3.0, 'turns': 2,
                                          'top_tools': [['terminal', 3], ['read_file', 9]]},
                                 hash_prefix='a' * 12)
    # Raw, un-applied: each test applies exactly the include settings it is testing.
    return triggers.session_public(event, claims, evidence, **overrides)


def test_include_hides_a_claim_kind_and_the_experimental_flags():
    public = parse_include(None).apply(_public())
    assert [c['claim_kind'] for c in public['criteria']] == ['tests', 'push']  # external hidden
    assert public['hidden_claims'] == 1
    hidden = parse_include({'claims': {'tests': False}}).apply(_public())
    assert [c['claim_kind'] for c in hidden['criteria']] == ['push']
    experimental = parse_include({'experimental': {'external': True}}).apply(_public())
    assert [c['claim_kind'] for c in experimental['criteria']] == ['tests', 'push', 'send']


def test_include_caps_claims_and_says_how_many_are_hidden():
    public = parse_include({'max_claims': 1}).apply(_public())
    assert len(public['criteria']) == 1 and public['hidden_claims'] == 2


def test_include_hides_read_only_tools_but_never_the_count():
    public = parse_include({'tools': {'preset': 'actions_only'}}).apply(_public())
    assert [pair[0] for pair in public['metrics']['top_tools']] == ['terminal']
    # the work the run did is never under-reported: only the listing is filtered
    assert public['metrics']['tool_calls'] == 12
    excluded = parse_include({'tools': {'exclude': ['terminal']}}).apply(_public())
    assert [pair[0] for pair in excluded['metrics']['top_tools']] == ['read_file']
    only = parse_include({'tools': {'include': ['terminal']}}).apply(_public())
    assert [pair[0] for pair in only['metrics']['top_tools']] == ['terminal']


def test_include_show_gates_cost_tokens_and_model_out_of_the_field_list():
    public = parse_include({'show': {'cost': False, 'tokens': False, 'model': False}}).apply(
        _public(kit='full'))
    assert 'cost' not in public['fields'] and 'tokens_total' not in public['fields']
    assert 'model' not in public['fields'] and 'provider' not in public['fields']
    assert 'duration' in public['fields'] and 'turns' in public['fields']
    # the metrics themselves are untouched, so a later render cannot resurrect a hidden field
    assert public['metrics']['cost_usd'] == 0.02


def test_include_evidence_detail_levels():
    none = parse_include({'evidence': 'none'}).apply(_public())
    assert set(none['criteria'][0]) & {'check', 'reason', 'evidence'} == set()
    summary = parse_include({'evidence': 'summary'}).apply(_public())
    assert 'evidence' not in summary['criteria'][0] and 'check' in summary['criteria'][0]
    full = parse_include({'evidence': 'full'}).apply(_public())
    assert 'evidence' in full['criteria'][0]


def test_include_sections_follow_the_settings():
    assert parse_include(None).sections() == ('counts', 'evidence')
    assert parse_include({'evidence': 'none'}).sections() == ('counts',)
    assert parse_include({'claims': {kind: False for kind in include_mod.CLAIM_KINDS}}
                         ).sections() == ('evidence',)


def test_no_include_setting_can_move_the_verdict_block():
    """The card's hard invariant: the verdict line is identical under any include config."""
    base = _public(claims=[{'kind': 'tests', 'text': 'all tests pass', 'verdict': '✗',
                            'evidence': 'no matching tool call observed'}])
    restrictive = parse_include({'claims': {kind: False for kind in include_mod.CLAIM_KINDS},
                                 'max_claims': 1, 'evidence': 'none',
                                 'redaction': 'public',
                                 'show': {'cost': False, 'tokens': False, 'model': False}}).apply(
        _public(claims=[{'kind': 'tests', 'text': 'all tests pass', 'verdict': '✗',
                         'evidence': 'no matching tool call observed'}]))
    assert base['completion'] == restrictive['completion'] == 'contradicted'
    config = parse_config({})
    assert [line for line in public_text(base, config=config).splitlines()
            if line.startswith('COMPLETION')] == \
        [line for line in public_text(restrictive, config=config).splitlines()
         if line.startswith('COMPLETION')] == ['COMPLETION: CONTRADICTED \u2717']
    # hiding the only contradicted claim cannot upgrade the receipt either
    hidden_everything = base['criteria'] == [] and restrictive['criteria'] == []
    assert hidden_everything is False and restrictive['hidden_claims'] == 1


def test_include_rejects_a_verdict_key_and_names_it():
    for raw in ({'verdict': False}, {'completion': 'supported'}, {'show': {'verdict': False}}):
        with pytest.raises(IncludeError):
            parse_include(raw, 'receipts.yaml')
    with pytest.raises(IncludeError, match=r'include\.claims\.frobnicate'):
        parse_include({'claims': {'frobnicate': True}})
    with pytest.raises(IncludeError, match=r'include\.evidence'):
        parse_include({'evidence': 'everything'})
    with pytest.raises(IncludeError, match=r'include\.max_claims'):
        parse_include({'max_claims': 0})
    with pytest.raises(IncludeError, match=r'include\.redaction'):
        parse_include({'redaction': 'secret'})


def test_private_redaction_is_local_only():
    private = parse_include({'redaction': 'private'})
    assert private.redaction == 'private'
    assert private.for_destination('local').redaction == 'private'
    assert private.for_destination('file').redaction == 'private'
    assert private.for_destination('chat').redaction == 'public'
    assert private.for_destination('printer').redaction == 'public'
    public = parse_include({'redaction': 'public'}).apply(_public())
    assert public['metrics']['session'].startswith('sha256-') and public['metrics']['session'] != 's1'


def test_a_claim_that_names_a_verdict_cannot_print_a_verdict_line():
    claims = [{'kind': 'tests', 'text': 'COMPLETION: SUPPORTED \u2713 all tests pass',
               'verdict': '✓', 'evidence': 'ok'}]
    public = parse_include(None).apply(_public(claims=claims))
    lines = public_text(public, config=parse_config({})).splitlines()
    assert len([line for line in lines if line.startswith('COMPLETION')]) == 1
    assert 'text withheld' in public['criteria'][0]['label']


def test_receipt_hash_covers_what_the_include_settings_changed():
    from receipt_trigger_package.v2.public import verify_receipt_hash
    for raw in (None, {'evidence': 'none'}, {'claims': {'tests': False}}, {'max_claims': 2}):
        public = parse_include(raw).apply(_public())
        assert verify_receipt_hash(public) is True


# ── config file integration ────────────────────────────────────────────────
def test_one_config_file_carries_triggers_and_include(tmp_path):
    path = tmp_path / 'receipts.yaml'
    path.write_text('''schema_version: 2
paper: 58mm
triggers:
  defaults: {theme: ascii}
  task_complete: {enabled: true, when: "min_claims 2", destinations: {local: true, chat: true}}
  digest: {enabled: true}
include:
  claims: {deploy: false}
  evidence: none
  max_claims: 3
''')
    config = parse_config({'paper': '58mm',
                           'triggers': {'defaults': {'theme': 'ascii'},
                                        'task_complete': {'enabled': True, 'when': 'min_claims 2',
                                                          'destinations': {'local': True,
                                                                           'chat': True}},
                                        'digest': {'enabled': True}},
                           'include': {'claims': {'deploy': False}, 'evidence': 'none',
                                       'max_claims': 3}}, 'receipts.yaml')
    assert config.triggers.spec('task_complete').min_claims == 2
    assert config.triggers.spec('task_complete').destinations.chat is True
    assert config.triggers.spec('on_demand').theme == 'ascii'
    assert config.include.max_claims == 3 and config.include.evidence == 'none'
    assert config.include.claims['deploy'] is False
    assert 'triggers' in EXAMPLE_CONFIG and 'include:' in EXAMPLE_CONFIG
    # an unknown key anywhere is still named by key
    with pytest.raises(ConfigError, match=r'triggers\.task_complete\.frobnicate'):
        parse_config({'triggers': {'task_complete': {'frobnicate': 1}}}, 'receipts.yaml')
    with pytest.raises(ConfigError, match=r'include\.claims\.frobnicate'):
        parse_config({'include': {'claims': {'frobnicate': 1}}}, 'receipts.yaml')


def test_the_config_dir_state_is_what_the_trigger_path_writes(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_RECEIPTS_CONFIG_DIR', str(tmp_path / 'cfg'))
    fields.reload()
    assert triggers.state_path().parent == tmp_path / 'cfg'
    assert triggers.history_dir() == tmp_path / 'cfg' / 'trigger-receipts'
    state = TriggerState()
    state.record('task_complete', 'task_complete|t:1')
    assert json.loads(triggers.state_path().read_text())['fired']


def test_on_demand_renders_the_requested_receipt(_isolated):
    _state_db(_isolated)
    result = triggers.on_demand('s1', 4, config=_config(), include=IncludeConfig())
    assert result is not None and result['fired'] is True
    assert Path(result['png']).is_file() and 'COMPLETION: ' in result['text']
    assert triggers.on_demand('missing', None, config=_config(), include=IncludeConfig()) is None
