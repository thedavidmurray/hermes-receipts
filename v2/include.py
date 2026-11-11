"""What is *in* a receipt: ``include:`` settings, which can never move the verdict.

``include:`` in ``receipts.yaml`` chooses how much of one observed run a receipt
shows: which claim kinds, which experimental flags, which tools are listed, how much
evidence detail, the redaction level, the claim cap, and whether cost, token and
model fields print at all. Three rules make that safe:

* **The verdict block is not an include setting.** :meth:`IncludeConfig.apply`
  recomputes ``completion`` over *every* claim in the run — including the claims the
  settings hide — and records ``hidden_claims``, so hiding a contradicted claim can
  never make a receipt look better. A key that names a verdict is rejected by name.
* **A receipt never under-reports work.** Tool include/exclude filters change which
  tools are *listed*; the ``TOOL CALLS`` count stays the true count of the run.
* **``private`` redaction is local-only.** A raw session id may print on the local
  PNG (the history store); chat and printer destinations are always rendered public.
  :meth:`IncludeConfig.for_destination` enforces that, so a config cannot leak a raw
  identifier into a chat reply by asking for ``private``.

Every validation failure names the offending key, e.g.::

    receipts.yaml: include.claims.frobnicate: unknown claim kind (known: ...)
    receipts.yaml: include.verdict: the verdict block is not an include setting
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Mapping

from .fields import (CONFIDENTIAL_METRICS, DEFAULT_KIT, VERDICT_GLYPHS, VERDICT_WORDS, kit_fields,
                     pseudonymize, select_fields, worst_verdict)
from .public import receipt_content_hash

#: An identifier that already left the process as a pseudonym. Redacting one twice
#: would give the same run two different labels, so it is passed through.
_PSEUDONYM = re.compile(r'sha256-[0-9a-f]{12}\Z')

#: Claim kinds a receipt may show or hide (the detector's own vocabulary).
CLAIM_KINDS: tuple[str, ...] = ('tests', 'commit', 'push', 'send', 'deploy', 'write', 'run')
#: Experimental claim flags. Off by default: neither is proof of anything yet.
EXPERIMENTAL_FLAGS: tuple[str, ...] = ('external', 'jev')
EVIDENCE_DETAILS: tuple[str, ...] = ('none', 'summary', 'full')
REDACTIONS: tuple[str, ...] = ('public', 'private')
TOOL_PRESETS: tuple[str, ...] = ('all', 'actions_only')
#: Tools that only read. ``actions_only`` hides them, so a receipt lists what the run
#: changed rather than every file it looked at. The count is never filtered.
READ_ONLY_TOOLS = frozenset({
    'read_file', 'search_files', 'list_dir', 'glob', 'grep', 'find_files', 'view_image',
    'web_search', 'web_extract', 'todo_list', 'session_search', 'skill_view', 'skills_list',
    'tool_search', 'tool_describe', 'memory', 'clarify', 'vision_analyze',
})
DEFAULT_MAX_CLAIMS = 12
MAX_CLAIM_CAP = 200
#: Fields gated by ``show:``; the verdict fields are deliberately absent.
SHOW_FIELDS: dict[str, tuple[str, ...]] = {
    'cost': ('cost', 'cost_scope'),
    'tokens': ('tokens_in', 'tokens_out', 'tokens_total'),
    'model': ('model', 'provider'),
}
#: v1 claim verdict -> the verdict vocabulary the (immutable) verdict block speaks.
CLAIM_VERDICTS: dict[str, str] = {'✓': 'supported', '✗': 'contradicted', 'not observed': 'unverified'}
#: The predicate each claim kind was checked with, in the v2 check vocabulary.
CLAIM_PREDICATES: dict[str, str] = {'tests': 'tests_pass', 'commit': 'remote_commit',
                                    'push': 'remote_commit', 'write': 'artifact_digest',
                                    'send': 'unknown', 'deploy': 'unknown', 'run': 'unknown'}
#: What each claim kind was checked against, for the ``summary`` evidence level.
CLAIM_CHECKS: dict[str, str] = {
    'tests': 'a recorded test run in this turn',
    'commit': 'the commit this turn created',
    'push': 'the remote push this turn made',
    'write': 'the file bytes this turn wrote',
    'send': 'the outbound tool invocation this turn made',
    'deploy': 'the deploy command this turn ran',
    'run': 'the command this turn ran',
}
#: Built-in defaults for a fresh install: everything the run observed, public, capped.
DEFAULTS: dict[str, Any] = {
    'claims': {kind: True for kind in CLAIM_KINDS},
    'experimental': {flag: False for flag in EXPERIMENTAL_FLAGS},
    'tools': {'preset': 'all', 'include': [], 'exclude': []},
    'redaction': 'public',
    'show': {'cost': True, 'tokens': True, 'model': True},
    'max_claims': DEFAULT_MAX_CLAIMS,
    'evidence': 'summary',
}


class IncludeError(ValueError):
    """An ``include:`` value broke the schema or named the verdict block."""


def _fail(source: str, key: str, message: str) -> IncludeError:
    return IncludeError(f'{source}: include.{key}: {message}')


def _verdict_named(raw: Mapping) -> str | None:
    """The first key that names the verdict block, if any.

    ``include:`` is about content, never about the verdict: the completion line and
    its colours print for every setting.
    """
    for key in raw:
        lowered = str(key).lower()
        if 'verdict' in lowered or 'completion' in lowered:
            return str(key)
    return None


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


def _as_choice(value: Any, source: str, key: str, known: Iterable[str]) -> str:
    if not isinstance(value, str) or value not in known:
        raise _fail(source, key, f'unknown value {value!r} (known: {", ".join(known)})')
    return value


def _as_names(value: Any, source: str, key: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(',') if part.strip())
    if not isinstance(value, (list, tuple)):
        raise _fail(source, key, 'must be a list of names')
    names: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise _fail(source, f'{key}[{index}]', 'must be a non-empty tool name')
        names.append(item.strip())
    return tuple(names)


@dataclass(frozen=True)
class IncludeConfig:
    """Validated ``include:`` settings. Immutable; one instance per receipt."""

    claims: Mapping[str, bool] = dataclass_field(default_factory=lambda: dict(DEFAULTS['claims']))
    experimental: Mapping[str, bool] = dataclass_field(
        default_factory=lambda: dict(DEFAULTS['experimental']))
    tool_preset: str = 'all'
    tools_include: tuple[str, ...] = ()
    tools_exclude: tuple[str, ...] = ()
    redaction: str = 'public'
    show: Mapping[str, bool] = dataclass_field(default_factory=lambda: dict(DEFAULTS['show']))
    max_claims: int = DEFAULT_MAX_CLAIMS
    evidence: str = 'summary'
    source: str | None = None

    # -- claim and tool selection -------------------------------------------
    def keep_claim(self, claim: Mapping) -> bool:
        """Whether one detected claim is shown: its kind, then its experimental flag."""
        kind = str(claim.get('kind') or '')
        if kind in self.claims and not self.claims.get(kind, True):
            return False
        if claim.get('external') and not self.experimental.get('external', False):
            return False
        if claim.get('jev') and not self.experimental.get('jev', False):
            return False
        return True

    def keep_tool(self, name: str) -> bool:
        """Whether one tool name is listed in ``TOP TOOLS``."""
        if self.tools_include and name not in self.tools_include:
            return False
        if name in self.tools_exclude:
            return False
        if self.tool_preset == 'actions_only' and name in READ_ONLY_TOOLS:
            return False
        return True

    def sections(self) -> tuple[str, ...]:
        """The optional sections this setting asks for: counts and/or evidence."""
        chosen = []
        if any(self.claims.get(kind, True) for kind in CLAIM_KINDS):
            chosen.append('counts')
        if self.evidence != 'none':
            chosen.append('evidence')
        return tuple(chosen)

    def for_destination(self, destination: str) -> 'IncludeConfig':
        """This setting as it must be rendered for *destination*.

        A chat reply or a printed page always leaves the process public, whatever
        ``redaction`` says: ``private`` is a local-PNG-only choice.
        """
        if destination in ('local', 'file') or self.redaction == 'public':
            return self
        return dataclasses.replace(self, redaction='public')

    # -- application --------------------------------------------------------
    def apply(self, public: Mapping, *, kit: str | None = None) -> dict:
        """A copy of *public* with hidden detail removed and the verdict recomputed.

        The completion verdict is the worst of the receipt's own completion and every
        criterion in the run, so a hidden contradicted claim can only make the
        printed verdict *worse*, never better.
        """
        receipt = dict(public)
        criteria = [c for c in public.get('criteria') or [] if isinstance(c, Mapping)]
        shown = [c for c in criteria if self._keep_criterion(c)][: self.max_claims]
        detail: list[dict] = []
        for criterion in shown:
            entry = dict(criterion)
            if self.evidence == 'none':
                for key in ('check', 'reason', 'evidence'):
                    entry.pop(key, None)
            elif self.evidence == 'summary':
                entry.pop('evidence', None)
            detail.append(entry)
        receipt['criteria'] = detail
        receipt['completion'] = worst_verdict([public.get('completion'),
                                               *[str(c.get('verdict')) for c in criteria]])
        # Applied to an already-applied receipt, the count only ever grows: hidden
        # claims are never silently forgotten by a second set of settings.
        receipt['hidden_claims'] = int(public.get('hidden_claims') or 0) + (len(criteria) - len(shown))
        receipt['included'] = self.describe()
        receipt['metrics'] = self._metrics(public, kit)
        receipt['fields'] = self._fields(public, kit)
        receipt['receipt_hash'] = receipt_content_hash(receipt)
        return receipt

    def _keep_criterion(self, criterion: Mapping) -> bool:
        kind = str(criterion.get('claim_kind') or criterion.get('kind') or '')
        if kind in self.claims and not self.claims.get(kind, True):
            return False
        return not (criterion.get('external') and not self.experimental.get('external', False))

    def _metrics(self, public: Mapping, kit: str | None) -> dict:
        metrics = dict(public.get('metrics') or {})
        top = metrics.get('top_tools')
        if isinstance(top, list):
            metrics['top_tools'] = [pair for pair in top
                                    if isinstance(pair, (list, tuple)) and pair
                                    and self.keep_tool(str(pair[0]))]
        if self.redaction == 'public':
            # One place, one rule: a public receipt never carries a raw identifier.
            for key, rule in CONFIDENTIAL_METRICS.items():
                value = metrics.get(key)
                if value is None:
                    continue
                if rule == 'pseudonym':
                    if isinstance(value, str) and _PSEUDONYM.fullmatch(value):
                        continue
                    metrics[key] = pseudonymize(value)
                else:
                    metrics[key] = 'withheld'
        return metrics

    def _fields(self, public: Mapping, kit: str | None) -> tuple[str, ...]:
        selected = select_fields(public, kit)
        if not selected:
            selected = kit_fields(kit or public.get('kit') or DEFAULT_KIT, public.get('categories'))
        hidden = {name for key, names in SHOW_FIELDS.items() if not self.show.get(key, True)
                  for name in names}
        if self.evidence != 'full':
            # Nothing to list at ``none``/``summary``: a field printing "n/a" for data
            # the setting deliberately dropped is noise, not information.
            hidden.add('evidence_locators')
        return tuple(name for name in selected if name not in hidden)

    # -- introspection ------------------------------------------------------
    def describe(self) -> dict:
        return {'claims': dict(self.claims), 'experimental': dict(self.experimental),
                'tools': {'preset': self.tool_preset, 'include': list(self.tools_include),
                          'exclude': list(self.tools_exclude)},
                'redaction': self.redaction, 'show': dict(self.show),
                'max_claims': self.max_claims, 'evidence': self.evidence}


DEFAULT_INCLUDE = IncludeConfig()


def parse_include(raw: Any, source: str = 'receipts.yaml') -> IncludeConfig:
    """Validate the ``include:`` block; every failure names the offending key."""
    if raw is None:
        return DEFAULT_INCLUDE
    mapping = _as_mapping(raw, source, 'include')
    named = _verdict_named(mapping)
    if named is not None:
        raise _fail(source, named, 'the verdict block is not an include setting: it prints '
                                   'for every configuration')
    known = {'claims', 'experimental', 'tools', 'redaction', 'show', 'max_claims', 'evidence'}
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise _fail(source, unknown[0], f'unknown key (known: {", ".join(sorted(known))})')

    claims = dict(DEFAULTS['claims'])
    if 'claims' in mapping:
        for kind, value in _as_mapping(mapping['claims'], source, 'claims').items():
            if kind not in CLAIM_KINDS:
                raise _fail(source, f'claims.{kind}',
                            f'unknown claim kind (known: {", ".join(CLAIM_KINDS)})')
            claims[str(kind)] = _as_bool(value, source, f'claims.{kind}')

    experimental = dict(DEFAULTS['experimental'])
    if 'experimental' in mapping:
        for flag, value in _as_mapping(mapping['experimental'], source, 'experimental').items():
            if flag not in EXPERIMENTAL_FLAGS:
                raise _fail(source, f'experimental.{flag}',
                            f'unknown flag (known: {", ".join(EXPERIMENTAL_FLAGS)})')
            experimental[str(flag)] = _as_bool(value, source, f'experimental.{flag}')

    preset, include_tools, exclude_tools = 'all', (), ()
    if 'tools' in mapping:
        tools = _as_mapping(mapping['tools'], source, 'tools')
        tool_unknown = sorted(set(tools) - {'preset', 'include', 'exclude'})
        if tool_unknown:
            raise _fail(source, f'tools.{tool_unknown[0]}',
                        'unknown key (known: preset, include, exclude)')
        if 'preset' in tools:
            preset = _as_choice(tools['preset'], source, 'tools.preset', TOOL_PRESETS)
        if 'include' in tools:
            include_tools = _as_names(tools['include'], source, 'tools.include')
        if 'exclude' in tools:
            exclude_tools = _as_names(tools['exclude'], source, 'tools.exclude')

    redaction = 'public'
    if 'redaction' in mapping:
        redaction = _as_choice(mapping['redaction'], source, 'redaction', REDACTIONS)

    show = dict(DEFAULTS['show'])
    if 'show' in mapping:
        for key, value in _as_mapping(mapping['show'], source, 'show').items():
            if key not in SHOW_FIELDS:
                raise _fail(source, f'show.{key}',
                            f'unknown key (known: {", ".join(sorted(SHOW_FIELDS))})')
            show[str(key)] = _as_bool(value, source, f'show.{key}')

    max_claims = DEFAULT_MAX_CLAIMS
    if 'max_claims' in mapping:
        max_claims = _as_int(mapping['max_claims'], source, 'max_claims', 1, MAX_CLAIM_CAP)

    evidence = 'summary'
    if 'evidence' in mapping:
        evidence = _as_choice(mapping['evidence'], source, 'evidence', EVIDENCE_DETAILS)

    return IncludeConfig(claims=claims, experimental=experimental, tool_preset=preset,
                         tools_include=include_tools, tools_exclude=exclude_tools,
                         redaction=redaction, show=show, max_claims=max_claims,
                         evidence=evidence, source=source)


def _reject_verdict_text(value: str) -> str:
    """Guard used by the receipt builder: user text may not name the verdict."""
    upper = value.upper()
    for verdict, word in VERDICT_WORDS.items():
        if word in upper or value.count(VERDICT_GLYPHS[verdict]):
            raise IncludeError('claim text may not contain the verdict word or glyph '
                               f'{word!r}: the verdict block is not configurable')
    return value


EXAMPLE_INCLUDE = '''\
# -- what is in a receipt (never the verdict) ------------------------------
# include:
#   claims: {tests: true, commit: true, push: true, send: true, deploy: true,
#            write: true, run: true}
#   experimental: {external: false, jev: false}   # both off by default
#   tools:              # what TOP TOOLS lists; TOOL CALLS keeps the true count
#     preset: actions_only   # all | actions_only (hide read-only tools)
#     include: []            # only these tool names, when set
#     exclude: [read_file]
#   redaction: public   # public | private (private is local PNG only)
#   show: {cost: true, tokens: true, model: true}
#   max_claims: 12
#   evidence: summary   # none | summary | full
'''
