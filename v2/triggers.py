"""Automatic triggers: *when* a receipt prints, *where* it goes, and what it costs.

The host fires real hooks (:data:`hermes_cli.plugins.VALID_HOOKS`); this module turns
them into receipts. Nothing here is on the hot path by default, and nothing here can
raise into a hook caller: :func:`fire` swallows and logs every error, caps the work it
starts at :data:`DEADLINE_SECONDS`, and renders off the turn's thread.

Defaults (implemented in :data:`DEFAULT_SPECS`, documented in the README table)::

    on_demand                ON   always            the request itself is the trigger
    task_complete            ON   always            kanban_task_completed — the hero
    goal_complete            ON   always            derived, see below
    claim_check              ON   exceptions_only   a full receipt only when a claim
                                                    is unverified or contradicted
    unattended_session_end   ON   always            cron / scheduled sessions
    interactive_session_end  OFF  min_claims 1      too noisy in a live chat
    subagent_stop            OFF  always            rolls up into the parent receipt
    task_blocked             OFF  always
    digest                   OFF  always            prints a cron line, never schedules

The host has no goal-loop hook and no cron hook, so two triggers are *derived* and
that derivation is documented in :data:`HOOK_SOURCES`:

* **goal_complete** — a ``kanban_task_completed`` whose card is ``goal_mode``, plus the
  ``on_session_end`` of a session whose row is ``goal_mode``. A column the host does
  not expose means *no* goal event, never a guess.
* **unattended_session_end** — an ``on_session_end`` whose platform (or session key)
  is in :data:`DEFAULT_UNATTENDED_PLATFORMS`, i.e. a cron/scheduled run.

Each trigger is configurable under ``triggers:`` in ``receipts.yaml`` with
``enabled``, ``when`` (``always`` | ``exceptions_only`` | ``min_claims N``), ``kit``,
``theme``, ``paper``/``columns``, ``profiles``/``platforms`` allow/deny, ``cooldown``,
``dedupe``, ``max_per_hour``, ``quiet_hours``, ``stamp`` and ``destinations``
(``local``, ``chat``, ``printer``, ``file``). What a receipt *contains* is
``include:`` — see :mod:`v2.include`.
"""
from __future__ import annotations

import dataclasses
import json
import contextvars
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .fields import (MANDATORY_SECTIONS, config_dir, kit_names, pseudonymize, theme_names,
                     sanitize_metrics, worst_verdict)
from .include import (CLAIM_CHECKS, CLAIM_PREDICATES, CLAIM_VERDICTS, DEFAULT_INCLUDE, IncludeConfig,
                      IncludeError, _reject_verdict_text, parse_include)
from .public import receipt_content_hash

LOG = logging.getLogger(__name__)

#: Every trigger a receipts.yaml may configure, in the order the README documents.
TRIGGERS: tuple[str, ...] = ('on_demand', 'task_complete', 'goal_complete', 'claim_check',
                             'unattended_session_end', 'interactive_session_end',
                             'subagent_stop', 'task_blocked', 'digest')

#: Which real host hook each trigger observes, and how a missing hook is derived.
HOOK_SOURCES: dict[str, tuple[str, str]] = {
    'on_demand': ('(no hook: the request is the trigger)',
                  '/receipt, `hermes receipt last|task`, the pane button'),
    'task_complete': ('kanban_task_completed', 'direct: the agent said it is done'),
    'goal_complete': ('kanban_task_completed',
                      'derived: a goal_mode card, plus the end of a goal session '
                      '(the host has no goal-loop hook)'),
    'claim_check': ('transform_llm_output', 'direct: an answer that declares an action'),
    'unattended_session_end': ('on_session_end',
                               'derived: a cron/scheduled session (the host has no cron hook)'),
    'interactive_session_end': ('on_session_end', 'derived: every other session end'),
    'subagent_stop': ('subagent_stop', 'direct'),
    'task_blocked': ('kanban_task_blocked', 'direct'),
    'digest': ('(no hook: deliberately never self-schedules)',
               'reserved: no digest runner is provided'),
}
#: Which real hook names this plugin registers for triggers.
REGISTERED_HOOKS: tuple[str, ...] = ('kanban_task_completed', 'kanban_task_blocked', 'on_session_end',
                                     'subagent_stop', 'transform_llm_output', 'post_llm_call',
                                     'agent_loop_stopped')
#: Platforms that mean "nobody is watching": a scheduled/cron session end.
DEFAULT_UNATTENDED_PLATFORMS: tuple[str, ...] = ('cron', 'schedule', 'scheduled', 'heartbeat',
                                                 'dispatcher', 'kanban-worker', 'daemon')
#: A hard cap on the work one trigger may start, in seconds (safety, not a suggestion).
DEADLINE_SECONDS = 2.0
#: Newest-first cap on the state file, so it can never grow without bound.
STATE_MAX_EVENTS = 500
STATE_TTL_SECONDS = 24 * 3600
#: How many recent deliveries `hermes receipt triggers --show` reports.
RECENT_LIMIT = 10

#: The commented `triggers:` block `hermes receipt config --init` writes.
EXAMPLE_TRIGGERS = '''\
# -- when receipts print by themselves, and where they go ------------------
# Every trigger takes: enabled, when (always | exceptions_only | "min_claims N"),
# kit, theme, paper/columns, profiles/platforms {allow, deny}, cooldown seconds,
# dedupe (one print per session+version), max_per_hour, quiet_hours "22:00-07:00"
# (UTC), stamp (the one-line claim stamp), and destinations.
# The defaults below are what a fresh install does; delete a line to keep it.
# triggers:
#   defaults:                 # inherited by every trigger unless it overrides
#     kit: verify
#     theme: thermal-classic
#     paper: 80mm
#     cooldown: 0
#     max_per_hour: 20
#     destinations: {local: true, chat: false, printer: false, file: null}
#   on_demand: {enabled: true}                        # /receipt, CLI, pane button
#   task_complete: {enabled: true}                    # kanban_task_completed: the hero
#   goal_complete: {enabled: true}                    # derived: goal_mode card or session
#   claim_check: {enabled: true, when: exceptions_only, stamp: false}
#   unattended_session_end: {enabled: true}           # cron / scheduled sessions
#   interactive_session_end: {enabled: false, when: "min_claims 1"}
#   subagent_stop: {enabled: false}                   # rolls up into the parent receipt
#   task_blocked: {enabled: false}
#   digest: {enabled: false}                          # reserved; no runner provided
#
# A printer destination needs the USB ids, so a print can never pick a wrong device:
#   on_demand:
#     destinations: {local: true, chat: true, printer: true, device: [0x04b8, 0x0202]}
#
# Digest is reserved; enabling it does not run or schedule anything.
'''

_TRIGGER_DIR = 'trigger-receipts'
_STATE_FILENAME = 'triggers-state.json'
_OUTBOX_FILENAME = 'trigger-outbox.jsonl'
_ASCII_ROUTED = '```\n{text}\n```'


class TriggerError(ValueError):
    """A ``triggers:`` value broke the schema."""


def _fail(source: str, key: str, message: str) -> TriggerError:
    return TriggerError(f'{source}: triggers.{key}: {message}')


# ── destinations ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Destinations:
    """Where one trigger's receipt goes.

    ``local`` is the history store (a PNG beside the config) and is on for every
    trigger. ``chat`` replies in the origin chat, ``printer`` needs explicit USB
    device ids, ``file`` exports the PNG to a path. No destination is network: the
    plugin never opens a socket, so "no network calls unless a chat/printer
    destination is configured" holds by construction.
    """

    local: bool = True
    chat: bool = False
    printer: bool = False
    file: str | None = None
    device: tuple[int, int] | None = None

    def enabled(self) -> tuple[str, ...]:
        chosen = [name for name in ('local', 'chat', 'printer', 'file')
                  if (name == 'file' and self.file) or (name != 'file' and getattr(self, name))]
        return tuple(chosen)

    def as_dict(self) -> dict:
        return {'local': self.local, 'chat': self.chat, 'printer': self.printer,
                'file': self.file, 'device': list(self.device) if self.device else None}


# ── the trigger spec ───────────────────────────────────────────────────────
#: Baseline every trigger inherits, then `triggers.defaults:` overrides, then the
#: trigger's own block overrides that.
BASELINE: dict[str, Any] = {
    'enabled': False, 'when': 'always', 'min_claims': 1, 'kit': None, 'theme': None,
    'paper': None, 'columns': None, 'cooldown': 0, 'max_per_hour': 20, 'quiet_hours': None,
    'dedupe': True, 'profiles_allow': (), 'profiles_deny': (), 'platforms_allow': (),
    'platforms_deny': (), 'destinations': Destinations(), 'stamp': False,
}

#: Hive's proposed defaults. A fresh install is silent in chat: `local` only.
DEFAULT_SPECS: dict[str, dict[str, Any]] = {
    'on_demand': {'enabled': True, 'when': 'always', 'dedupe': False,
                  'destinations': Destinations(local=True, chat=True)},
    'task_complete': {'enabled': True, 'when': 'always'},
    'goal_complete': {'enabled': True, 'when': 'always'},
    'claim_check': {'enabled': True, 'when': 'exceptions_only', 'stamp': False},
    'unattended_session_end': {'enabled': True, 'when': 'always'},
    'interactive_session_end': {'enabled': False, 'when': 'min_claims', 'min_claims': 1},
    'subagent_stop': {'enabled': False, 'when': 'always'},
    'task_blocked': {'enabled': False, 'when': 'always'},
    'digest': {'enabled': False, 'when': 'always'},
}


@dataclass(frozen=True)
class TriggerSpec:
    """One validated trigger: when it fires, what it looks like, where it goes."""

    name: str
    enabled: bool = False
    when: str = 'always'                 # always | exceptions_only | min_claims
    min_claims: int = 1
    kit: str | None = None
    theme: str | None = None
    paper: str | None = None
    columns: int | None = None
    cooldown: int = 0
    max_per_hour: int = 20
    quiet_hours: tuple[int, int] | None = None
    dedupe: bool = True
    profiles_allow: tuple[str, ...] = ()
    profiles_deny: tuple[str, ...] = ()
    platforms_allow: tuple[str, ...] = ()
    platforms_deny: tuple[str, ...] = ()
    destinations: Destinations = dataclass_field(default_factory=Destinations)
    stamp: bool = False

    def as_dict(self) -> dict:
        spec = {key: getattr(self, key) for key in
                ('name', 'enabled', 'when', 'min_claims', 'kit', 'theme', 'paper', 'columns',
                 'cooldown', 'max_per_hour', 'dedupe', 'stamp')}
        spec['quiet_hours'] = '%02d:%02d-%02d:%02d' % (*divmod(self.quiet_hours[0], 60),
                                                       *divmod(self.quiet_hours[1], 60)) \
            if self.quiet_hours else None
        spec['profiles'] = {'allow': list(self.profiles_allow), 'deny': list(self.profiles_deny)}
        spec['platforms'] = {'allow': list(self.platforms_allow), 'deny': list(self.platforms_deny)}
        spec['destinations'] = self.destinations.as_dict()
        return spec


@dataclass(frozen=True)
class TriggerConfig:
    """The validated ``triggers:`` block: one spec per known trigger."""

    specs: Mapping[str, TriggerSpec] = dataclass_field(default_factory=dict)
    unattended_platforms: tuple[str, ...] = DEFAULT_UNATTENDED_PLATFORMS
    source: str | None = None

    def spec(self, name: str) -> TriggerSpec:
        found = self.specs.get(name)
        if found is None:
            raise TriggerError(f'unknown trigger {name!r} (known: {", ".join(TRIGGERS)})')
        return found

    def table(self) -> list[dict]:
        """Every trigger's resolved settings, for `--show` and the README."""
        return [{**self.spec(name).as_dict(), 'hook': HOOK_SOURCES[name][0],
                 'derived': HOOK_SOURCES[name][1]} for name in TRIGGERS]


# ── parsing ────────────────────────────────────────────────────────────────
def _as_mapping(value: Any, source: str, key: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise _fail(source, key, 'must be a mapping of keys to values')
    return value


def _as_bool(value: Any, source: str, key: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(source, key, 'must be true or false')
    return value


def _as_int(value: Any, source: str, key: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(source, key, 'must be a whole number')
    if not low <= value <= high:
        raise _fail(source, key, f'must be between {low} and {high}')
    return value


def _as_names(value: Any, source: str, key: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(',') if part.strip())
    if not isinstance(value, (list, tuple)):
        raise _fail(source, key, 'must be a list of names')
    names: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise _fail(source, f'{key}[{index}]', 'must be a non-empty name')
        names.append(item.strip())
    return tuple(names)


_WHEN = re.compile(r'^(always|exceptions_only|min_claims)(?:\s*[=: ]\s*(\d+))?$')


def _parse_when(value: Any, source: str, key: str) -> tuple[str, int]:
    """``always`` | ``exceptions_only`` | ``min_claims N`` (also a ``{min_claims: N}`` map)."""
    if isinstance(value, Mapping):
        unknown = sorted(set(value) - {'min_claims'})
        if unknown:
            raise _fail(source, f'{key}.{unknown[0]}', 'unknown key (known: min_claims)')
        return 'min_claims', _as_int(value['min_claims'], source, f'{key}.min_claims', 1, 100)
    if not isinstance(value, str):
        raise _fail(source, key, 'must be always, exceptions_only or "min_claims N"')
    match = _WHEN.match(value.strip())
    if match is None:
        raise _fail(source, key, f'unknown mode {value!r} (known: always, exceptions_only, '
                                 'min_claims N)')
    mode, count = match.group(1), match.group(2)
    if mode == 'min_claims':
        return mode, int(count) if count else 1
    if count is not None:
        raise _fail(source, key, f'{mode!r} takes no count')
    return mode, 1


_TIME = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')


def _parse_quiet_hours(value: Any, source: str, key: str) -> tuple[int, int] | None:
    """``22:00-07:00`` (may wrap midnight); empty/none means never quiet."""
    if value in (None, '', False):
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        value = f'{value[0]}-{value[1]}'
    if not isinstance(value, str) or '-' not in value:
        raise _fail(source, key, 'must be "HH:MM-HH:MM" (UTC), e.g. "22:00-07:00"')
    start_text, _, end_text = value.partition('-')
    parsed = []
    for part in (start_text.strip(), end_text.strip()):
        match = _TIME.match(part)
        if match is None:
            raise _fail(source, key, f'invalid UTC time {part!r} (expected HH:MM)')
        parsed.append(int(match.group(1)) * 60 + int(match.group(2)))
    if parsed[0] == parsed[1]:
        raise _fail(source, key, 'quiet hours must start and end at different times')
    return (parsed[0], parsed[1])


def _parse_destinations(value: Any, source: str, key: str) -> Destinations:
    if value is None:
        return Destinations()
    if isinstance(value, Destinations):
        return value
    mapping = _as_mapping(value, source, key)
    known = {'local', 'chat', 'printer', 'file', 'device'}
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise _fail(source, f'{key}.{unknown[0]}', f'unknown key (known: {", ".join(sorted(known))})')
    local = _as_bool(mapping['local'], source, f'{key}.local') if 'local' in mapping else True
    chat = _as_bool(mapping['chat'], source, f'{key}.chat') if 'chat' in mapping else False
    printer = _as_bool(mapping['printer'], source, f'{key}.printer') \
        if 'printer' in mapping else False
    export = None
    if mapping.get('file') not in (None, ''):
        export = str(mapping['file']).strip()
        if not export:
            raise _fail(source, f'{key}.file', 'must be a path or omitted')
    device = None
    if 'device' in mapping and mapping['device'] is not None:
        raw = mapping['device']
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise _fail(source, f'{key}.device', 'must be [vendor, product] (hex strings or ints)')
        try:
            device = (int(str(raw[0]), 0), int(str(raw[1]), 0))
        except (TypeError, ValueError) as exc:
            raise _fail(source, f'{key}.device', 'must be [vendor, product] (hex strings or ints)') from exc
        if any(not 0 <= part <= 0xFFFF for part in device):
            raise _fail(source, f'{key}.device', 'USB ids are 16-bit')
    if printer and device is None:
        raise _fail(source, f'{key}.printer',
                    'needs device: [vendor, product] so an opt-in print cannot pick a wrong device')
    return Destinations(local=local, chat=chat, printer=printer, file=export, device=device)


def _parse_allow_deny(value: Any, source: str, key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if value is None:
        return (), ()
    if not isinstance(value, Mapping):
        return _as_names(value, source, key), ()
    unknown = sorted(set(value) - {'allow', 'deny'})
    if unknown:
        raise _fail(source, f'{key}.{unknown[0]}', 'unknown key (known: allow, deny)')
    allow = _as_names(value['allow'], source, f'{key}.allow') if 'allow' in value else ()
    deny = _as_names(value['deny'], source, f'{key}.deny') if 'deny' in value else ()
    return allow, deny


def _spec_from(base: Mapping[str, Any], raw: Any, name: str,
               source: str) -> TriggerSpec:
    mapping = _as_mapping(raw, source, name) if raw is not None else {}
    known = {'enabled', 'when', 'kit', 'theme', 'paper', 'columns', 'cooldown', 'max_per_hour',
             'quiet_hours', 'dedupe', 'profiles', 'platforms', 'destinations', 'stamp',
             'min_claims'}
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise _fail(source, f'{name}.{unknown[0]}',
                    f'unknown key (known: {", ".join(sorted(known))})')
    spec = dict(base)
    if 'enabled' in mapping:
        spec['enabled'] = _as_bool(mapping['enabled'], source, f'{name}.enabled')
    if 'when' in mapping:
        spec['when'], spec['min_claims'] = _parse_when(mapping['when'], source, f'{name}.when')
    if 'min_claims' in mapping:
        spec['min_claims'] = _as_int(mapping['min_claims'], source, f'{name}.min_claims', 1, 100)
    if 'kit' in mapping and mapping['kit'] is not None:
        kit = str(mapping['kit'])
        if kit not in kit_names():
            raise _fail(source, f'{name}.kit',
                        f'unknown kit {kit!r} (available: {", ".join(kit_names())})')
        spec['kit'] = kit
    if 'theme' in mapping and mapping['theme'] is not None:
        theme = str(mapping['theme'])
        if theme not in theme_names():
            raise _fail(source, f'{name}.theme',
                        f'unknown theme {theme!r} (available: {", ".join(theme_names())})')
        spec['theme'] = theme
    if 'paper' in mapping or 'columns' in mapping:
        from .config import _parse_paper  # local import: config imports this module
        paper, columns = _parse_paper(mapping.get('paper'),
                                      mapping.get('columns') if 'columns' in mapping else None,
                                      source)
        spec['paper'], spec['columns'] = paper, columns
    for key, low, high in (('cooldown', 0, 86400), ('max_per_hour', 1, 1000)):
        if key in mapping:
            spec[key] = _as_int(mapping[key], source, f'{name}.{key}', low, high)
    if 'quiet_hours' in mapping:
        spec['quiet_hours'] = _parse_quiet_hours(mapping['quiet_hours'], source, f'{name}.quiet_hours')
    if 'dedupe' in mapping:
        spec['dedupe'] = _as_bool(mapping['dedupe'], source, f'{name}.dedupe')
    if 'stamp' in mapping:
        spec['stamp'] = _as_bool(mapping['stamp'], source, f'{name}.stamp')
    if 'profiles' in mapping:
        spec['profiles_allow'], spec['profiles_deny'] = _parse_allow_deny(
            mapping['profiles'], source, f'{name}.profiles')
    if 'platforms' in mapping:
        spec['platforms_allow'], spec['platforms_deny'] = _parse_allow_deny(
            mapping['platforms'], source, f'{name}.platforms')
    if 'destinations' in mapping:
        spec['destinations'] = _parse_destinations(mapping['destinations'], source,
                                                   f'{name}.destinations')
    return TriggerSpec(name=name, **spec)


DEFAULT_TRIGGER_CONFIG = TriggerConfig(
    specs={name: _spec_from(dict(BASELINE), dict(DEFAULT_SPECS[name]), name, 'built-in defaults')
           for name in TRIGGERS})


def parse_triggers(raw: Any, source: str = 'receipts.yaml') -> TriggerConfig:
    """Validate the ``triggers:`` block; every failure names the offending key."""
    if raw is None:
        return DEFAULT_TRIGGER_CONFIG
    mapping = _as_mapping(raw, source, 'triggers')
    known = {*TRIGGERS, 'defaults', 'unattended_platforms'}
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise _fail(source, unknown[0], f'unknown trigger (known: {", ".join(TRIGGERS)}, '
                                        'defaults, unattended_platforms)')
    unattended = DEFAULT_UNATTENDED_PLATFORMS
    if 'unattended_platforms' in mapping:
        unattended = _as_names(mapping['unattended_platforms'], source, 'unattended_platforms')
    base = dict(BASELINE)
    if 'defaults' in mapping:
        defaults = _as_mapping(mapping['defaults'], source, 'defaults')
        base = _spec_kwargs(_spec_from(base, dict(defaults), 'defaults', source))
    specs = {name: _spec_from(base, dict(DEFAULT_SPECS[name]), name, source) for name in TRIGGERS}
    for name in TRIGGERS:
        if name in mapping and mapping[name] is not None:
            specs[name] = _spec_from(_spec_kwargs(specs[name]), mapping[name], name, source)
    return TriggerConfig(specs=specs, unattended_platforms=unattended, source=source)


#: `(mtime, size)` -> parsed `(triggers, include)`, so the answer path never re-parses YAML.
_CONFIG_CACHE: dict[str, tuple[tuple[float, int], tuple['TriggerConfig', IncludeConfig]]] = {}
_CACHE_LOCK = threading.Lock()


def _spec_kwargs(spec: TriggerSpec) -> dict[str, Any]:
    """A spec back as the keyword mapping :func:`_spec_from` accepts."""
    return {key: getattr(spec, key) for key in BASELINE}


def load_trigger_config(path: str | Path | None = None) -> tuple[TriggerConfig, Any]:
    """``(triggers, include)`` resolved from ``receipts.yaml``; defaults when absent.

    The whole file is validated first, so an unknown key anywhere is an error the
    user sees by name rather than a silently ignored trigger.
    """
    from .config import ConfigError, config_file, load_config
    try:
        target: Path | None = Path(path).expanduser() if path else config_file()
        stamp = target.stat()
    except (AttributeError, OSError):
        target = None if path is None else Path(path).expanduser()
        stamp = None
    key = str(target) if target else ''
    signature = (stamp.st_mtime_ns, stamp.st_size) if stamp is not None else None
    if signature is not None:
        with _CACHE_LOCK:
            cached = _CONFIG_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
    try:
        config = load_config(path)
    except ConfigError as exc:
        LOG.warning('receipts config rejected: %s', exc)
        return DEFAULT_TRIGGER_CONFIG, DEFAULT_INCLUDE
    if config is None:
        return DEFAULT_TRIGGER_CONFIG, DEFAULT_INCLUDE
    resolved = (config.triggers, config.include)
    if signature is not None:
        with _CACHE_LOCK:
            _CONFIG_CACHE[key] = (signature, resolved)
    return resolved


# ── events ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TriggerEvent:
    """One normalized reason to print: which trigger, for which run, with what text."""

    trigger: str
    version: str                    # the run identity dedupe keys on
    text: str = ''                  # the claim-bearing text (summary, reason, answer)
    session_id: str | None = None
    platform: str = ''
    profile: str = ''
    task_id: str | None = None
    board: str | None = None
    goal_mode: bool = False
    unattended: bool = False
    detail: str = ''                # why this event exists, for the audit trail


def _as_boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false', '1', '0', 'yes', 'no'):
        return value.strip().lower() in ('true', '1', 'yes')
    return None


def _session_key(kwargs: Mapping) -> str | None:
    for key in ('session_key', 'session_id', 'parent_session_id'):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_unattended(kwargs: Mapping, unattended_platforms: Sequence[str]) -> bool:
    """A cron/scheduled session, derived from the payload (the host has no cron hook)."""
    explicit = _as_boolish(kwargs.get('unattended'))
    if explicit is not None:
        return explicit
    platform = str(kwargs.get('platform') or '').strip().lower()
    if platform and platform in unattended_platforms:
        return True
    key = str(_session_key(kwargs) or '').lower()
    return any(marker in key for marker in ('cron', 'schedul', 'heartbeat'))


def _goal_session(session_id: str | None) -> bool:
    """Whether this session row is a goal loop; a missing column is *not* a goal."""
    if not session_id:
        return False
    path = _state_db()
    if path is None:
        return False
    try:
        import sqlite3
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.5)
        try:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(sessions)')}
            wanted = [name for name in ('goal_mode', 'goal') if name in columns]
            if not wanted:
                return False
            row = conn.execute(f'SELECT {", ".join(wanted)} FROM sessions WHERE id=?',
                               (session_id,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    return bool(row) and any(bool(value) for value in row)


def _goal_card(task_id: str | None, kwargs: Mapping) -> bool:
    """Whether a kanban card is a goal_mode card; never guessed when unreadable."""
    explicit = _as_boolish(kwargs.get('goal_mode') if 'goal_mode' in kwargs else kwargs.get('goal'))
    if explicit is not None:
        return explicit
    if not task_id:
        return False
    from hermes_constants import get_hermes_home
    candidates = [get_hermes_home() / 'kanban.db']
    for path in candidates:
        if not path.is_file():
            continue
        try:
            import sqlite3
            conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.5)
            try:
                columns = {row[1] for row in conn.execute('PRAGMA table_info(tasks)')}
                if 'goal_mode' not in columns:
                    return False
                row = conn.execute('SELECT goal_mode FROM tasks WHERE id=?', (task_id,)).fetchone()
            finally:
                conn.close()
            return bool(row) and bool(row[0])
        except Exception:
            return False
    return False


def events_for_hook(hook_name: str, kwargs: Mapping,
                    unattended_platforms: Sequence[str] = DEFAULT_UNATTENDED_PLATFORMS
                    ) -> list[TriggerEvent]:
    """Turn one real host hook call into the triggers it should evaluate.

    Pure and cheap by design: the only I/O is the goal_mode lookup, which happens
    off the turn's thread (see :func:`fire`).
    """
    platform = str(kwargs.get('platform') or '')
    profile = str(kwargs.get('profile_name') or kwargs.get('profile') or '')
    session_id = _session_key(kwargs)
    events: list[TriggerEvent] = []

    if hook_name == 'kanban_task_completed':
        task_id = str(kwargs.get('task_id') or '') or None
        run = str(kwargs.get('run_id') or '')
        goal = _goal_card(task_id, kwargs)
        events.append(TriggerEvent('task_complete', f'{task_id}:{run}', text=str(kwargs.get('summary') or ''),
                                   session_id=session_id, platform=platform, profile=profile,
                                   task_id=task_id, board=str(kwargs.get('board') or '') or None,
                                   goal_mode=goal, detail='kanban_task_completed'))
        if goal:
            events.append(TriggerEvent('goal_complete', f'{task_id}:{run}:goal',
                                       text=str(kwargs.get('summary') or ''), session_id=session_id,
                                       platform=platform, profile=profile, task_id=task_id,
                                       goal_mode=True, detail='goal_mode kanban task completed'))
    elif hook_name == 'kanban_task_blocked':
        task_id = str(kwargs.get('task_id') or '') or None
        events.append(TriggerEvent('task_blocked', f'{task_id}:{kwargs.get("run_id") or ""}',
                                   text=str(kwargs.get('reason') or ''), session_id=session_id,
                                   platform=platform, profile=profile, task_id=task_id,
                                   board=str(kwargs.get('board') or '') or None,
                                   goal_mode=_goal_card(task_id, kwargs),
                                   detail='kanban_task_blocked'))
    elif hook_name == 'on_session_end':
        unattended = _is_unattended(kwargs, unattended_platforms)
        trigger = 'unattended_session_end' if unattended else 'interactive_session_end'
        version = f'{session_id}:{kwargs.get("turn_id") or ""}'
        events.append(TriggerEvent(trigger, version, session_id=session_id, platform=platform,
                                   profile=profile, unattended=unattended,
                                   detail='on_session_end ('
                                          + ('cron/scheduled' if unattended else 'interactive')
                                          + ')'))
        if kwargs.get('completed') is not False and _goal_session(session_id):
            events.append(TriggerEvent('goal_complete', f'{version}:goal', session_id=session_id,
                                       platform=platform, profile=profile, goal_mode=True,
                                       detail='goal session ended'))
    elif hook_name == 'subagent_stop':
        events.append(TriggerEvent('subagent_stop', f'{session_id}:{kwargs.get("child_session_id") or ""}',
                                   text=str(kwargs.get('child_summary') or ''), session_id=session_id,
                                   platform=platform, profile=profile,
                                   detail='subagent_stop'))
    elif hook_name in ('transform_llm_output', 'post_llm_call'):
        text = _answer_text(kwargs)
        if text.strip():
            events.append(TriggerEvent('claim_check', f'{session_id}:{kwargs.get("turn_id") or ""}',
                                       text=text, session_id=session_id, platform=platform,
                                       profile=profile, detail=hook_name))
    return events


def _answer_text(kwargs: Mapping) -> str:
    for key in ('response_text', 'answer', 'final_response', 'text'):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ''


# ── state: dedupe, cooldown, hourly cap, quiet-hours bookkeeping ───────────
def state_path() -> Path:
    return Path(config_dir()) / _STATE_FILENAME


def outbox_path() -> Path:
    return Path(config_dir()) / _OUTBOX_FILENAME


def history_dir() -> Path:
    return Path(config_dir()) / _TRIGGER_DIR


class TriggerState:
    """A bounded JSON ledger of what fired, when, and what was delivered.

    One file beside ``receipts.yaml``; read-modify-write with a lock because two
    hook threads can fire at once. A corrupt file is replaced, never fatal.
    """

    def __init__(self, path: str | Path | None = None, *, now: float | None = None) -> None:
        self.path = Path(path) if path else state_path()
        self._lock = threading.Lock()
        self._now = now
        self.data: dict[str, Any] = {'fired': {}, 'deliveries': [], 'stopped': {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, Mapping):
            return
        fired = raw.get('fired')
        deliveries = raw.get('deliveries')
        stopped = raw.get('stopped')
        self.data = {'fired': dict(fired) if isinstance(fired, Mapping) else {},
                     'deliveries': list(deliveries) if isinstance(deliveries, list) else [],
                     'stopped': dict(stopped) if isinstance(stopped, Mapping) else {}}

    def _prune(self) -> None:
        cutoff = self._clock() - STATE_TTL_SECONDS
        self.data['fired'] = {key: value for key, value in self.data['fired'].items()
                              if isinstance(value, (int, float)) and value >= cutoff}
        if len(self.data['fired']) > STATE_MAX_EVENTS:
            ordered = sorted(self.data['fired'].items(), key=lambda item: item[1], reverse=True)
            self.data['fired'] = dict(ordered[:STATE_MAX_EVENTS])
        self.data['deliveries'] = [row for row in self.data['deliveries']
                                   if isinstance(row, Mapping) and row.get('at', 0) >= cutoff][-STATE_MAX_EVENTS:]
        self.data['stopped'] = {key: value for key, value in self.data['stopped'].items()
                                if isinstance(value, Mapping) and value.get('at', 0) >= cutoff}

    def _clock(self) -> float:
        return time.time() if self._now is None else self._now

    def save(self) -> None:
        try:
            self._prune()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.data, sort_keys=True))
            temp.replace(self.path)
        except OSError:
            LOG.debug('trigger state not written', exc_info=True)

    def seen(self, key: str) -> bool:
        return key in self.data['fired']

    def last_fired(self, trigger: str) -> float | None:
        stamps = [value for key, value in self.data['fired'].items()
                  if key.startswith(f'{trigger}|') and isinstance(value, (int, float))]
        return max(stamps) if stamps else None

    def hour_count(self, trigger: str, window: float = 3600.0) -> int:
        cutoff = self._clock() - window
        return sum(1 for key, value in self.data['fired'].items()
                   if key.startswith(f'{trigger}|') and isinstance(value, (int, float))
                   and value >= cutoff)

    def record(self, trigger: str, key: str) -> None:
        with self._lock:
            self.data['fired'][key] = self._clock()
            self.save()

    def note_stopped(self, session_id: str | None, reason: str) -> None:
        """A turn was interrupted: recorded so a later receipt of it is not a lie."""
        if not session_id:
            return
        with self._lock:
            self.data['stopped'][session_id] = {'at': self._clock(), 'reason': reason}
            self.save()

    def note_delivery(self, row: Mapping) -> None:
        with self._lock:
            self.data['deliveries'].append({**row, 'at': self._clock()})
            self.save()

    def last_receipt(self, session_id: str | None) -> str | None:
        """The newest local PNG this session produced (used by the one-line stamp)."""
        for row in reversed(self.data['deliveries']):
            if row.get('destination') == 'local' and row.get('detail') \
                    and (session_id is None or row.get('session') == session_id):
                return str(row['detail'])
        return None

    def recent(self, limit: int = RECENT_LIMIT) -> list[dict]:
        return [dict(row) for row in self.data['deliveries'][-limit:]]


# ── decisions ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Decision:
    """Whether a trigger fires, and the single reason it did or did not."""

    trigger: str
    fire: bool
    full: bool = True          # a full receipt (vs a one-line stamp / nothing)
    claims: tuple = ()
    reason: str = ''


def _claim_verdicts(claims: Iterable[Mapping]) -> list[str]:
    return [CLAIM_VERDICTS.get(str(claim.get('verdict') or ''), 'unverified') for claim in claims]


def _in_quiet_hours(window: tuple[int, int] | None, now: float) -> bool:
    if window is None:
        return False
    minute = datetime.fromtimestamp(now, timezone.utc).hour * 60 \
        + datetime.fromtimestamp(now, timezone.utc).minute
    start, end = window
    return start <= minute < end if start < end else (minute >= start or minute < end)


def decide(event: TriggerEvent, config: TriggerConfig, *, claims: Sequence[Mapping] = (),
           state: TriggerState | None = None, now: float | None = None,
           include: IncludeConfig = DEFAULT_INCLUDE) -> Decision:
    """One trigger, one event: fire or not, with the reason named.

    ``claims`` are the *matched* claims when the caller has them (verdicts included);
    without them the claim count comes from the detector's regular expression, so a
    decision never needs the database.
    """
    spec = config.spec(event.trigger)
    if not spec.enabled:
        return Decision(event.trigger, False, reason='disabled')
    if event.profile and event.profile in spec.profiles_deny:
        return Decision(event.trigger, False, reason=f'profile {event.profile!r} denied')
    if spec.profiles_allow and event.profile not in spec.profiles_allow:
        return Decision(event.trigger, False, reason=f'profile {event.profile!r} not allowed')
    if event.platform and event.platform in spec.platforms_deny:
        return Decision(event.trigger, False, reason=f'platform {event.platform!r} denied')
    if spec.platforms_allow and event.platform not in spec.platforms_allow:
        return Decision(event.trigger, False, reason=f'platform {event.platform!r} not allowed')
    clock = time.time() if now is None else now
    if _in_quiet_hours(spec.quiet_hours, clock):
        return Decision(event.trigger, False, reason='quiet hours')
    matched = tuple(claims) if claims else tuple(detect_claims(event.text))
    if spec.when == 'exceptions_only':
        verdicts = _claim_verdicts(matched)
        unestablished = sum(1 for verdict in verdicts if verdict in ('unverified', 'contradicted'))
        if not unestablished:
            return Decision(event.trigger, False, claims=matched,
                            reason='every claim is supported')
        return Decision(event.trigger, True, claims=matched,
                        reason=f'{unestablished} of {len(verdicts)} claim(s) unestablished')
    if spec.when == 'min_claims' and len(matched) < spec.min_claims:
        return Decision(event.trigger, False, claims=matched,
                        reason=f'{len(matched)} claim(s) < min_claims {spec.min_claims}')
    if state is not None:
        key = f'{event.trigger}|{event.version}'
        if spec.dedupe and state.seen(key):
            return Decision(event.trigger, False, claims=matched,
                            reason=f'already printed for {event.version}')
        if spec.cooldown:
            last = state.last_fired(event.trigger)
            if last is not None and clock - last < spec.cooldown:
                return Decision(event.trigger, False, claims=matched,
                                reason=f'cooldown {spec.cooldown}s')
        if state.hour_count(event.trigger) >= spec.max_per_hour:
            return Decision(event.trigger, False, claims=matched,
                            reason=f'max_per_hour {spec.max_per_hour}')
    return Decision(event.trigger, True, claims=matched, reason=f'when={spec.when}')


# ── evidence and the public receipt ────────────────────────────────────────
@dataclass(frozen=True)
class Evidence:
    """What one bounded read of the profile's ``state.db`` gave us (or did not)."""

    rows: tuple = ()
    answer: str = ''
    session_id: str | None = None
    metrics: Mapping = dataclass_field(default_factory=dict)
    window: tuple[float, float] | None = None
    available: bool = False
    hash_prefix: str = ''


def _state_db() -> Path | None:
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / 'state.db'
    return path if path.is_file() else None


def read_evidence(session_id: str | None = None, turn_id: int | None = None) -> Evidence:
    """One bounded, read-only read of this profile's state.db. Never raises."""
    path = _state_db()
    if path is None:
        return Evidence(session_id=session_id)
    try:
        from ..history import receipt_from_state
        receipt = receipt_from_state(path, session_id=session_id, turn_id=turn_id)
    except Exception:
        LOG.debug('no tool stream for this event', exc_info=True)
        return Evidence(session_id=session_id)
    metrics: Mapping = {}
    window = None
    try:
        from .telemetry import facts_from_state
        metrics = facts_from_state(path, session_id=receipt.get('session_id'))
    except Exception:
        LOG.debug('no metrics for this session', exc_info=True)
    duration = metrics.get('duration_seconds') if isinstance(metrics, Mapping) else None
    if isinstance(duration, (int, float)) and duration:
        end = time.time()
        window = (end - float(duration), end)
    return Evidence(rows=tuple(receipt.get('rows') or ()), answer=str(receipt.get('answer') or ''),
                    session_id=receipt.get('session_id') or session_id, metrics=metrics,
                    window=window, available=True, hash_prefix=str(receipt.get('hash') or '')[:12])


def detect_claims(text: str) -> list[dict]:
    """The detector's claims without evidence; each is *unverified*, never 'wrong'."""
    if not text.strip():
        return []
    try:
        from ..receipt_core import extract_claims
    except ImportError:  # standalone import path (scripts, tests)
        try:
            from receipt_core import extract_claims  # type: ignore
        except ImportError:
            return []
    return [dict(claim) for claim in extract_claims(text)]


def match_evidence(text: str, evidence: Evidence) -> list[dict]:
    """Match *text*'s claims against this run's tool calls.

    Without a readable tool stream every claim is *unverified* rather than
    contradicted: an unreadable ledger is not evidence against the agent.
    """
    if not evidence.available:
        return [{**claim, 'verdict': 'not observed',
                 'evidence': 'no persisted tool stream for this event'}
                for claim in detect_claims(text)]
    try:
        from ..receipt_core import match_claims
    except ImportError:
        try:
            from receipt_core import match_claims  # type: ignore
        except ImportError:
            return detect_claims(text)
    return match_claims(text, list(evidence.rows))


#: Titles are a fixed vocabulary: no user text ever reaches the header.
TITLES: dict[str, str] = {
    'on_demand': 'Receipt (requested)',
    'task_complete': 'Your agent says it\u2019s done. Get the receipt.',
    'goal_complete': 'Goal loop ended. Get the receipt.',
    'claim_check': 'A claim did not check out. Get the receipt.',
    'unattended_session_end': 'Scheduled session receipt',
    'interactive_session_end': 'Session receipt',
    'subagent_stop': 'Subagent receipt',
    'task_blocked': 'Task blocked receipt',
    'digest': 'Fleet statement',
}
QUALIFICATIONS = {
    'supported': ('Every claim in this window matched an observed tool call; not unrestricted '
                  'proof of completeness.'),
    'other': ('Declared claims were not established in this window; missing evidence is not a lie.'),
}


def _claim_label(claim: Mapping, index: int, limit: int = 100) -> str:
    text = str(claim.get('text') or '').strip().replace('\n', ' ')[:limit]
    if not text:
        return f'Claim {index}'
    try:
        return _reject_verdict_text(text)
    except IncludeError:
        # The agent's own words may not print a verdict line on the paper.
        return f'Claim {index} (text withheld: it names a verdict)'


def _claim_reason(claim: Mapping) -> str:
    text = str(claim.get('evidence') or 'no reason was recorded').strip().replace('\n', ' ')[:80]
    try:
        return _reject_verdict_text(text)
    except IncludeError:
        return 'reason withheld: it names a verdict'


def session_public(event: TriggerEvent, claims: Sequence[Mapping], evidence: Evidence, *,
                   kit: str | None = None, theme: str | None = None) -> dict:
    """Build the public receipt for one event. Constructed afresh, never copied."""
    criteria = []
    for index, claim in enumerate(claims, 1):
        kind = str(claim.get('kind') or 'unknown')
        entry = {'label': _claim_label(claim, index), 'required': True,
                 'kind': CLAIM_PREDICATES.get(kind, 'unknown'), 'claim_kind': kind,
                 'check': CLAIM_CHECKS.get(kind, 'an unrecognised claim kind'),
                 'reason': _claim_reason(claim), 'evidence': [],
                 'verdict': CLAIM_VERDICTS.get(str(claim.get('verdict') or ''), 'unverified')}
        if claim.get('external'):
            entry['external'] = True
        criteria.append(entry)
    completion = worst_verdict([c['verdict'] for c in criteria]) if criteria else 'not_evaluable'
    now = time.time()
    window = evidence.window or (now, now)
    from .fields import plugin_provenance
    provenance = plugin_provenance()
    receipt = {
        'schema_version': 2, 'kind': 'task', 'title': TITLES.get(event.trigger, 'Receipt'),
        'completion': completion, 'criteria': criteria,
        'artifact_version': ('sha256-' + evidence.hash_prefix) if evidence.hash_prefix
        else 'version withheld',
        'window': f'{_date(window[0])} to {_date(window[1])} (UTC, end exclusive)',
        'qualification': QUALIFICATIONS['supported' if completion == 'supported' else 'other'],
        'detector_version': 'deterministic-2.0', 'matcher_version': 'deterministic-2.0',
        'plugin_version': provenance['plugin_version'], 'git_sha': provenance['git_sha'],
        'timestamp': _timestamp(now),
        'warning': 'Experimental viewer; evidence scope and coverage are limited.',
        'metrics': sanitize_metrics(dict(evidence.metrics)),
        'kit': kit, 'theme': theme, 'fields': None, 'categories': None,
        'receipt_id': pseudonymize(event.version or event.session_id or event.trigger),
        'trigger': event.trigger, 'trigger_detail': event.detail,
    }
    receipt['receipt_hash'] = receipt_content_hash(receipt)
    return receipt


def _date(value: float) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime('%Y-%m-%d UTC')
    except (ValueError, TypeError, OverflowError, OSError):
        return 'unknown date'


def _timestamp(value: float) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (ValueError, TypeError, OverflowError, OSError):
        return 'unknown time'


# ── rendering and delivery ─────────────────────────────────────────────────
@dataclass
class Rendered:
    """One receipt, rendered: the public value, its text, its PNG and where it went."""

    public: dict
    text: str
    png: Path | None = None
    deliveries: list[dict] = dataclass_field(default_factory=list)


def effective_config(spec: TriggerSpec, include: IncludeConfig, base=None):
    """The printer config a trigger renders with: per-trigger paper, include's blocks.

    Precedence is the card's: the trigger's own ``kit``/``theme``/``paper`` win over
    ``receipts.yaml``, and ``include:`` decides whether the counts and evidence blocks
    print at all. The verdict block is untouchable in every case.
    """
    from .config import parse_config
    config = base if base is not None else parse_config({}, 'trigger defaults')
    if spec.paper or spec.columns:
        config = config.with_paper(spec.paper, spec.columns)
    wanted = set(include.sections())
    sections = {**dict(config.sections), 'claims': 'counts' in wanted,
                'evidence': 'evidence' in wanted}
    for name in MANDATORY_SECTIONS:
        sections.pop(name, None)  # never stated as off: mandatory sections ignore it anyway
    return dataclasses.replace(config, sections=sections)


def render_event(event: TriggerEvent, claims: Sequence[Mapping], evidence: Evidence,
                 *, spec: TriggerSpec, include: IncludeConfig, base_config=None) -> Rendered:
    """Render one receipt for one destination-independent receipt value."""
    from .render import public_text, render_task_png
    public = session_public(event, claims, evidence, kit=spec.kit, theme=spec.theme)
    config = effective_config(spec, include, base_config)
    public = include.apply(public, kit=spec.kit)
    text = public_text(public, kit=spec.kit, theme=spec.theme, config=config)
    png = None
    if spec.destinations.local or spec.destinations.chat or spec.destinations.printer:
        png = _history_png(public, spec, event)
        render_task_png(public, png, kit=spec.kit, theme=spec.theme, config=config)
    return Rendered(public=public, text=text, png=png)


def _history_png(public: Mapping, spec: TriggerSpec, event: TriggerEvent) -> Path:
    digest = str(public.get('receipt_hash') or 'nohash')[7:19]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    return history_dir() / f'{spec.name}-{stamp}-{digest}.png'


def chat_payload(rendered: Rendered, platform: str, *, spec: TriggerSpec,
                 include: IncludeConfig, base_config=None) -> str:
    """The chat delivery: an ASCII code block on Discord, the PNG elsewhere."""
    if 'discord' in platform.lower():
        from .render import public_text
        ascii_text = public_text(rendered.public, kit=spec.kit, theme='ascii',
                                 config=effective_config(spec, include.for_destination('chat'),
                                                         base_config))
        return _ASCII_ROUTED.format(text=ascii_text)
    if rendered.png is not None:
        return f'{rendered.text}\nMEDIA:{rendered.png}'
    return rendered.text


def deliver(rendered: Rendered, spec: TriggerSpec, event: TriggerEvent, *,
            include: IncludeConfig, state: TriggerState | None = None, base_config=None,
            outbox: Path | None = None) -> list[dict]:
    """Send one rendered receipt to every configured destination. Never raises."""
    deliveries: list[dict] = []
    destinations = spec.destinations
    if destinations.local and rendered.png is not None:
        deliveries.append({'destination': 'local', 'detail': str(rendered.png)})
    if destinations.file and rendered.png is not None:
        target = Path(str(destinations.file)).expanduser()
        if target.is_dir() or str(destinations.file).endswith('/'):
            target = target / rendered.png.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(rendered.png.read_bytes())
            deliveries.append({'destination': 'file', 'detail': str(target)})
        except OSError:
            LOG.warning('receipt file export failed', exc_info=True)
            deliveries.append({'destination': 'file', 'detail': 'failed'})
    if destinations.chat:
        # A chat reply is always public, whatever the local PNG's redaction says; the
        # local receipt is re-redacted only when it needs to be, so one run keeps one
        # pseudonym across the PNG and the reply.
        chat_public = rendered.public if include.redaction == 'public' else \
            include.for_destination('chat').apply(rendered.public, kit=spec.kit)
        chat_rendered = Rendered(public=chat_public, text=rendered.text, png=rendered.png)
        payload = chat_payload(chat_rendered, event.platform, spec=spec, include=include,
                               base_config=base_config)
        deliveries.append({'destination': 'chat', 'detail': payload[:4000]})
        _append_outbox({'trigger': spec.name, 'session': event.session_id,
                        'platform': event.platform, 'detail': payload[:4000]}, outbox=outbox)
    if destinations.printer and destinations.device:
        vendor, product = destinations.device
        try:
            from ..printer import print_receipt
            options = dict(getattr(base_config, 'printer', None) or {})
            ok, _ = print_receipt(rendered.text, vendor, product, **options)
            deliveries.append({'destination': 'printer', 'detail': 'submitted' if ok
                               else 'unavailable'})
        except Exception:
            LOG.warning('receipt printer delivery failed', exc_info=True)
            deliveries.append({'destination': 'printer', 'detail': 'failed'})
    if state is not None:
        for row in deliveries:
            state.note_delivery({**row, 'trigger': spec.name, 'session': event.session_id,
                                 'platform': event.platform})
    return deliveries


def _append_outbox(row: Mapping, *, outbox: Path | None = None) -> None:
    """Every chat delivery is also durable, so an observer hook's reply is not lost."""
    path = Path(outbox) if outbox else outbox_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({**row, 'at': time.time()}, sort_keys=True) + '\n')
    except OSError:
        LOG.debug('receipt outbox not written', exc_info=True)


def read_outbox(limit: int = RECENT_LIMIT, *, outbox: Path | None = None) -> list[dict]:
    path = Path(outbox) if outbox else outbox_path()
    try:
        lines = path.read_text(encoding='utf-8').splitlines()[-limit:]
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        rows.append(value if isinstance(value, dict) else {'detail': str(value)})
    return rows


# ── the hook entry point ───────────────────────────────────────────────────
def _now() -> float:
    return time.time()


def execute(hook_name: str, kwargs: Mapping, *, background: bool = False,
            config: TriggerConfig | None = None, include: IncludeConfig | None = None,
            state: TriggerState | None = None, base_config=None, now: float | None = None,
            evidence: Evidence | None = None, outbox: Path | None = None) -> list[dict]:
    """Evaluate every trigger one hook call implies, then deliver what fires.

    Bounded by :data:`DEADLINE_SECONDS` from the first byte of work; a trigger that
    runs past it stops where it stands and says so. Never raises: a failing renderer
    is logged and the caller's turn continues.
    """
    started = _now()
    outcomes: list[dict] = []
    if config is None or include is None:
        loaded_triggers, loaded_include = load_trigger_config()
        if config is None:
            config = loaded_triggers
        if include is None:
            include = loaded_include
    if state is None:
        state = TriggerState(now=now)
    if hook_name == 'agent_loop_stopped':
        state.note_stopped(_session_key(kwargs), str(kwargs.get('reason') or 'stopped'))
        return outcomes
    try:
        events = events_for_hook(hook_name, kwargs, config.unattended_platforms)
    except Exception:
        LOG.exception('receipt trigger event derivation failed')
        return outcomes
    for event in events:
        try:
            outcome = _one(event, config=config, include=include, state=state, now=now,
                           base_config=base_config, evidence=evidence, outbox=outbox,
                           started=started)
        except Exception as exc:  # fail open, always
            LOG.exception('receipt trigger %s failed', event.trigger)
            outcome = {'trigger': event.trigger, 'fired': False, 'reason': 'error',
                       'error': f'{type(exc).__name__}: {exc}', 'deliveries': []}
        outcomes.append(outcome)
    return outcomes


def _one(event: TriggerEvent, *, config: TriggerConfig, include: IncludeConfig, state: TriggerState,
         now: float | None, base_config, evidence: Evidence | None, outbox: Path | None,
         started: float) -> dict:
    spec = config.spec(event.trigger)
    probe = evidence if evidence is not None else read_evidence(event.session_id)
    # A session-end event carries no text of its own: the answer it ended is the
    # claim-bearing text, read from the same bounded state.db read.
    text = event.text if event.text.strip() else (probe.answer if probe.available else '')
    claims = match_evidence(text, probe) if text.strip() else []
    decision = decide(event, config, claims=claims, state=state, now=now, include=include)
    if not decision.fire:
        return {'trigger': event.trigger, 'fired': False, 'reason': decision.reason,
                'claims': len(claims), 'deliveries': []}
    if _now() - started > DEADLINE_SECONDS:
        return {'trigger': event.trigger, 'fired': False, 'reason': 'deadline exceeded',
                'deliveries': []}
    rendered = render_event(event, claims, probe, spec=spec, include=include,
                            base_config=base_config)
    deliveries = deliver(rendered, spec, event, include=include, state=state,
                         base_config=base_config, outbox=outbox)
    if spec.dedupe:
        state.record(event.trigger, f'{event.trigger}|{event.version}')
    return {'trigger': event.trigger, 'fired': True, 'reason': decision.reason,
            'claims': len(claims), 'png': str(rendered.png) if rendered.png else None,
            'chat': next((row['detail'] for row in deliveries if row['destination'] == 'chat'),
                         None),
            'deliveries': [row['destination'] for row in deliveries]}


def fire(hook_name: str, /, **kwargs) -> Any:
    """The registered hook handler. Off the hot path, fail open, never blocking.

    Returns the (optionally stamped) answer text for ``transform_llm_output`` and
    ``None`` for every observer hook, exactly as the host expects.
    """
    text = _answer_text(kwargs)
    stamp = None
    try:
        spec = load_trigger_config()[0].spec('claim_check')
        if hook_name in ('transform_llm_output', 'post_llm_call') and spec.enabled \
                and spec.stamp and text.strip():
            count = len(detect_claims(text))
            if count:
                state = TriggerState()
                last = state.last_receipt(_session_key(kwargs))
                stamp = f'\n\n\U0001f9fe {count} claim(s) seen this turn' + (
                    f' \u2014 receipt: {last}' if last else '')
    except Exception:
        LOG.debug('receipt stamp skipped', exc_info=True)
    try:
        # Multiplexed profiles use ContextVars, not process-global HERMES_HOME.
        # Capture on the hook thread before the host resets its profile context.
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(_safe_execute, hook_name, dict(kwargs)),
                                  name=f'receipts-{hook_name}', daemon=True)
        thread.start()
    except Exception:
        LOG.exception('receipt trigger dispatch failed')
    if hook_name == 'transform_llm_output':
        return f'{text}{stamp}' if stamp else None
    return None


def _safe_execute(hook_name: str, kwargs: dict) -> None:
    try:
        execute(hook_name, kwargs, background=True)
    except Exception:  # pragma: no cover - execute already fails open
        LOG.exception('receipt trigger background run failed')


def on_demand(session_id: str | None = None, turn_id: int | None = None, *, platform: str = '',
              config: TriggerConfig | None = None, include: IncludeConfig | None = None,
              state: TriggerState | None = None, now: float | None = None,
              base_config=None, outbox: Path | None = None) -> dict | None:
    """The ``on_demand`` trigger: an explicit request always prints.

    Used by ``/receipt``, the CLI and the pane button, so one configuration governs
    requested receipts and automatic ones alike.
    """
    if not session_id and platform != 'cli':
        return None  # only the explicitly local operator CLI may read profile-wide history
    loaded_triggers, loaded_include = load_trigger_config()
    config = config or loaded_triggers
    include = include or loaded_include
    state = state if state is not None else TriggerState(now=now)
    event = TriggerEvent('on_demand', f'{session_id}:{turn_id or ""}', session_id=session_id,
                         platform=platform, detail='on-demand request')
    probe = read_evidence(session_id, turn_id)
    if not probe.available and not probe.answer.strip():
        # Nothing to receipt: an empty page would be worse than saying so.
        return None
    claims = match_evidence(probe.answer, probe)
    decision = decide(event, config, claims=claims, state=state, now=now)
    if not decision.fire:
        return None
    spec = config.spec('on_demand')
    rendered = render_event(event, claims, probe, spec=spec, include=include,
                            base_config=base_config)
    deliveries = deliver(rendered, spec, event, include=include,
                         state=state if spec.dedupe else None, base_config=base_config,
                         outbox=outbox)
    return {'trigger': 'on_demand', 'fired': True, 'reason': decision.reason,
            'claims': len(claims), 'text': rendered.text,
            'png': str(rendered.png) if rendered.png else None, 'deliveries': deliveries}
