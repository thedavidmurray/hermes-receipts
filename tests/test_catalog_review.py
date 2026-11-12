"""Regressions for session authorization and multiplexed profile isolation."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('receipt_review_package', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from receipt_review_package.v2 import fields, triggers
from receipt_review_package.v2.config import ConfigError, parse_config


def seed(home, answer='PROFILE B PRIVATE ANSWER', *, goal=True):
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / 'state.db') as db:
        db.executescript('''
          CREATE TABLE sessions(id TEXT PRIMARY KEY, input_tokens INTEGER,
            output_tokens INTEGER, estimated_cost_usd REAL);
          CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL);
          INSERT INTO sessions VALUES ('A',0,0,0),('B',0,0,0);
          INSERT INTO messages VALUES (1,'A','user','No answer yet',NULL,NULL,NULL,1);
          INSERT INTO messages VALUES (2,'B','user','Private question',NULL,NULL,NULL,2);
        ''')
        db.execute("INSERT INTO messages VALUES (3,'B','assistant',?,NULL,NULL,NULL,3)", (answer,))
    with sqlite3.connect(home / 'kanban.db') as db:
        db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, goal_mode INTEGER)')
        db.execute('INSERT INTO tasks VALUES (?,?)', ('task', int(goal)))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    for key in ('HERMES_RECEIPTS_CONFIG_DIR', 'HERMES_RECEIPTS_JEV',
                'HERMES_RECEIPTS_STAMP', 'HERMES_RECEIPTS_GATE', 'HERMES_KANBAN_DB'):
        monkeypatch.delenv(key, raising=False)


def test_explicit_id_cannot_cross_session(tmp_path):
    home = tmp_path / 'home'
    seed(home)
    with pytest.raises(ValueError, match='no persisted'):
        plugin.receipt_from_state(home / 'state.db', session_id='A', turn_id=3)
    assert not triggers.read_evidence('A', 3).available
    assert triggers.on_demand('A', 3) is None
    reply = plugin._slash('3', session_id='A')
    assert reply.startswith('Receipt unavailable')
    assert 'PRIVATE' not in reply and 'MEDIA:' not in reply
    assert plugin.receipt_from_state(home / 'state.db', session_id='B', turn_id=3)['final_id'] == 3
    assert triggers.on_demand(None, 3, platform='discord') is None


@pytest.mark.parametrize('fallback', ['empty', 'error'])
def test_slash_fallback_never_uses_another_sessions_answer(tmp_path, monkeypatch, fallback):
    seed(tmp_path / 'home')
    def unavailable(*args, **kwargs):
        if fallback == 'error':
            raise RuntimeError('renderer failed')
        return None
    monkeypatch.setattr(triggers, 'on_demand', unavailable)
    for arg in ('', '3'):
        reply = plugin._slash(arg, session_id='A')
        assert reply.startswith('Receipt unavailable')
        assert 'PRIVATE' not in reply and 'MEDIA:' not in reply
    # Even a missing caller session must never become a profile-global lookup.
    assert plugin._slash('3').startswith('Receipt unavailable')


def test_missing_current_profile_db_does_not_fall_back(tmp_path, monkeypatch):
    other = tmp_path / 'other'
    seed(other)
    monkeypatch.setenv('HERMES_HOME', str(other))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(other / 'kanban.db'))
    token = set_hermes_home_override(tmp_path / 'missing')
    try:
        assert triggers._state_db() is None
        assert not triggers.read_evidence('B').available
        assert not triggers._goal_card('task', {})
    finally:
        reset_hermes_home_override(token)


def test_worker_keeps_profile_context_and_reads_own_databases(tmp_path, monkeypatch):
    homes = [tmp_path / 'one', tmp_path / 'two']
    for number, home in enumerate(homes):
        seed(home, answer=f'answer {number}', goal=bool(number))
        for kind in ('kits', 'themes'):
            directory = home / 'receipt-printer' / kind
            directory.mkdir(parents=True)
            (directory / 'custom.json').write_text(json.dumps({'name': f'profile-{number}',
                'fields': ['model'], 'ink': '#222620'}))
    monkeypatch.setenv('HERMES_HOME', str(homes[0]))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(homes[0] / 'kanban.db'))
    barrier = threading.Barrier(3)
    seen = []
    errors = []
    threads = []
    real_thread = threading.Thread
    def thread_factory(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        threads.append(thread)
        return thread
    def execute(hook, payload, **kwargs):
        try:
            barrier.wait(timeout=5)  # caller resets context before either worker reads
            seen.append((triggers.read_evidence('B').answer, triggers._goal_card('task', {}),
                         str(fields.config_dir()),
                         sorted(name for name in fields.kit_names() if name.startswith('profile-')),
                         sorted(name for name in fields.theme_names() if name.startswith('profile-'))))
        except BaseException as exc:
            errors.append(exc)
    monkeypatch.setattr(triggers.threading, 'Thread', thread_factory)
    monkeypatch.setattr(triggers, 'execute', execute)
    for home in homes:
        token = set_hermes_home_override(home)
        try:
            fields.reload()  # warm this profile's cache before its async hook
            triggers.fire('on_session_end', session_id='B')
        finally:
            reset_hermes_home_override(token)
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not errors
    assert sorted(seen) == [(f'answer {i}', bool(i), str(home / 'receipt-printer'),
                            [f'profile-{i}'], [f'profile-{i}'])
                            for i, home in enumerate(homes)]


def test_profile_switch_refreshes_assets_without_explicit_reload(tmp_path):
    for number in (0, 1, 0):
        home = tmp_path / str(number)
        directory = home / 'receipt-printer' / 'kits'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'custom.json').write_text(json.dumps({'name': f'profile-{number}',
                                                          'fields': ['model']}))
        token = set_hermes_home_override(home)
        try:
            assert fields.kit_fields(f'profile-{number}') == ['model']
            assert f'profile-{1-number}' not in fields.kit_names()
            assert triggers.state_path().parent == home / 'receipt-printer'
            assert triggers.history_dir().parent == home / 'receipt-printer'
        finally:
            reset_hermes_home_override(token)


def test_legacy_config_only_resolves_under_active_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    default_legacy = tmp_path / '.hermes' / 'receipts'
    default_legacy.mkdir(parents=True)
    active = tmp_path / 'active'
    token = set_hermes_home_override(active)
    try:
        assert fields.config_dir() == active / 'receipt-printer'
        legacy = active / 'receipts'
        legacy.mkdir(parents=True)
        assert fields.config_dir() == legacy
    finally:
        reset_hermes_home_override(token)


def test_yaml_runtime_switches_override_deprecated_env(tmp_path, monkeypatch):
    directory = tmp_path / 'home' / 'receipt-printer'
    directory.mkdir(parents=True)
    (directory / 'receipts.yaml').write_text('runtime: {jev: false, stamp: false, gate: observe}\n')
    monkeypatch.setenv('HERMES_RECEIPTS_JEV', '1')
    monkeypatch.setenv('HERMES_RECEIPTS_STAMP', '1')
    monkeypatch.setenv('HERMES_RECEIPTS_GATE', 'block')
    assert plugin._runtime_setting('jev') is False
    assert plugin._runtime_setting('stamp') is False
    assert plugin._runtime_setting('gate') == 'observe'
    (directory / 'receipts.yaml').write_text('runtime: {jev: true, stamp: true, gate: block}\n')
    assert plugin._runtime_setting('jev') is True
    assert plugin._runtime_setting('stamp') is True
    assert plugin._runtime_setting('gate') == 'block'
    (directory / 'receipts.yaml').unlink()
    assert plugin._runtime_setting('jev') is True
    assert plugin._runtime_setting('stamp') is True
    assert plugin._runtime_setting('gate') == 'block'
    monkeypatch.delenv('HERMES_RECEIPTS_JEV')
    monkeypatch.delenv('HERMES_RECEIPTS_STAMP')
    monkeypatch.delenv('HERMES_RECEIPTS_GATE')
    assert plugin._runtime_setting('jev') is False
    assert plugin._runtime_setting('stamp') is False
    assert plugin._runtime_setting('gate') == 'observe'


@pytest.mark.parametrize('runtime', [{'unknown': True}, {'jev': 'true'}, {'stamp': 1}, {'gate': 'yes'}])
def test_runtime_config_is_strict(runtime):
    with pytest.raises(ConfigError, match='runtime'):
        parse_config({'runtime': runtime})


def test_manifest_cli_refuses_test_commands(tmp_path, monkeypatch, capsys):
    from receipt_review_package.v2 import cli, evidence
    def forbidden(*args, **kwargs):
        pytest.fail('a manifest must never cause a subprocess test run')
    monkeypatch.setattr(evidence.subprocess, 'run', forbidden)
    manifest = tmp_path / 'task.json'
    manifest.write_text(json.dumps({
        'claim': {'kind': 'done', 'task_key': 'task', 'scope': {}},
        'requirements': [{'requirement_id': 'tests', 'target_key': 'artifact',
                          'artifact_version': 'abc', 'evidence_predicate': 'tests_pass'}],
        'window': [0, 10],
        'probes': [{'requirement_id': 'tests', 'argv': ['python', '-V']}],
    }))
    parser = argparse.ArgumentParser()
    plugin._cli_setup(parser)
    args = parser.parse_args(['task', '--manifest', str(manifest),
                              '--png', str(tmp_path / 'never.png')])
    with pytest.raises(ValueError, match='tests need trusted in-process runner'):
        cli._task(args)
    assert plugin._cli_handler(args) == 1
    assert 'Receipt unavailable' in capsys.readouterr().out
    assert not (tmp_path / 'never.png').exists()


def test_disclosures_match_cli_and_library_behavior():
    readme = (ROOT / 'README.md').read_text()
    catalog = (ROOT / 'plugin-catalog' / 'receipt-printer.yaml').read_text()
    assert 're-runs' not in readme and 're-runs' not in catalog
    for document in (readme, catalog):
        assert 'run_bound_test' in document and 'rev-list --all' in document
        assert 'rev-parse --short=12 HEAD' in document
        assert 'refuses test criteria' in document
