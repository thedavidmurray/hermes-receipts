"""Deterministic affirmative claim detection, using spans in the original answer.

No tool calls, raw answer persistence, task-title inference or evidence verdicts here.
Links are already-resolved private keys from an explicit manifest/structured ID.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from .contracts import Claim, Kind, Scope, claim_key

DETECTOR_VERSION = 'deterministic-2.0'

# The patterns match a proposition, not just a keyword in a tool command.
_PATTERNS: tuple[tuple[Kind, str], ...] = (
    ('tests', r'\b(?:[\w./-]+\s+)?tests?(?:\s+(?:were|have been|are))?\s+(?:all\s+)?(?:pass(?:ed|ing)?|succeed(?:ed)?|green|run\s+and\s+passed)\b|\b(?:passed|passing|green)\s+(?:all\s+)?tests?\b|\bpytest\s+(?:passed|green)\b'),
    ('commit', r'\b(?:committ?ed|commit\s+(?:created|made|pushed)|created\s+(?:a\s+)?commit|changes?\s+(?:were|have been)\s+committed|commit\s+is\s+on\s+\w+)\b'),
    ('push', r'\b(?:pushed|changes?\s+(?:were|have been)\s+pushed|push\s+(?:succeeded|completed))\b'),
    ('send', r'\b(?:sent|emailed|posted|published|delivered|shared|sent\s+out|(?:email|message|post|report)\s+(?:was|were|has been)\s+(?:sent|posted|published|delivered))\b'),
    ('deploy', r'\b(?:deployed|released\s+to\s+(?:production|prod)|shipped\s+(?:(?:the\s+)?release\s+)?to\s+(?:production|prod)|(?:site|app|service|release)\s+(?:was|has been)\s+deployed)\b'),
    ('write', r'\b(?:created|wrote|saved|patched|edited|added|updated|generated|drafted|fixed|implemented|modified|verified)\s+(?:(?:a|the|an|new|two|three)\s+)?(?:(?:kanban|new|existing)\s+)?(?:files?|tasks?|cards?|notes?|reports?|drafts?|scripts?|pages?|modules?|scaffolds?|patch|changes?|tests?|[\w./-]+\.(?:py|js|ts|md|json|yaml|png))\b|\b(?:built|created|added|updated|implemented)\s+(?:(?:a|an|the|new)\s+)?(?:(?:CLI|vault|local|web)\s+)?(?:dashboard|integration|entry|workflow|pipeline|feature|endpoint|widget|component)\b|\b(?:files?|notes?|reports?|drafts?|[\w./-]+\.(?:py|js|ts|md|json|yaml|png))\s+(?:(?:was|were|has been|is)\s+)?(?:written|created|generated|updated|saved|fixed)\b'),
    ('run', r'\b(?:ran|executed|invoked)\s+(?:(?:the|daily)\s+)?(?:[\w./-]+\s+)?(?:command|script|pytest|tests?|build|enrichment|reporter)\b|\brun\s+completed\b|\b(?:script|build|command|tests?)\s+(?:was|were|has been)\s+(?:run|executed)\b'),
    ('done', r'\b(?:task|work|job|request|implementation|delivery)\s+(?:(?:is|was|has been)\s+)?(?:done|complete(?:d)?|finished)\b|\b(?:completed|finished)\s+(?:the\s+)?(?:task|work|job|request|implementation|delivery)\b|\ball\s+required\s+work\s+(?:is|was|has been)\s+(?:done|complete(?:d)?|finished)\b|\bdone\s*[:—-]\s*(?=(?:task|work|job|request)\s+(?:is\s+)?(?:done|complete(?:d)?|finished)\b)|^\W*done\b'),
)
_RULES = tuple((kind, re.compile(pattern, re.I)) for kind, pattern in _PATTERNS)
_FENCED = re.compile(r'```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)', re.S)
_INLINE = re.compile(r'`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|(?<!\w)\x27[^\x27\n]*\x27(?!\w)')
_BREAK = re.compile(r'[.!?;](?=\s|$)|\n+|\s+[—–]\s+|,\s*(?=(?:but|however|then|yet)\b)|\s+(?:but|however|then|yet|and\s+I)\s+', re.I)
_NEGATOR_BEFORE_YET = re.compile(r"(?:\b(?:not|never)|n['’]t)\s*$", re.I)
_NEGATION = re.compile(r'\b(?:not|never|nothing|neither|none|zero|0\s+(?:new|other|additional)?|no|\w+n[\x27’]t|didn[\x27’]?t|haven[\x27’]?t|hasn[\x27’]?t|wasn[\x27’]?t|weren[\x27’]?t|couldn[\x27’]?t|failed\s+to|without)\b', re.I)
_OTHER_ACTOR = re.compile(r'\b(?:you|your)\b', re.I)
_FUTURE = re.compile(r'\b(?:will|would|should|must|might|may|going\s+to|plan(?:ning)?\s+to|intend\s+to|need\s+to|about\s+to|still\s+need\s+to)\b', re.I)
_SKIP_LINE = re.compile(r'^\s*(?:>|#{1,6}\s|https?://|(?:plan|todo|next|upcoming|future|example|hypothetical|instructions?)\s*:)', re.I)
_EXTERNAL = re.compile(r'\b(?:subagent|other\s+agent|another\s+agent|someone\s+else|external\s+job|CI|GitHub\s+Actions|according\s+to|report(?:s|ed)?\s+that)\b', re.I)
_CONDITIONAL = re.compile(r'^\s*(?:if|when|once|unless|please|to|try\s+to|can\s+you|you\s+should)\b', re.I)
_IMPERATIVE = re.compile(r'^\s*(?:please\s+)?(?:run|push|commit|deploy|send|publish|write|create|execute|test)\b(?!\s+(?:completed|succeeded))', re.I)
_INCOMPLETE = re.compile(r'\b(?:nearly|almost|partially|not\s+yet|in\s+progress|pending|still|for\s+now)\b', re.I)


def _mask(answer: str) -> str:
    """Preserve every offset, including newlines, while hiding unasserted text."""
    masked = list(answer)
    for regex in (_FENCED, _INLINE):
        for match in regex.finditer(answer):
            for pos in range(*match.span()):
                if answer[pos] != '\n':
                    masked[pos] = ' '
    return ''.join(masked)


def _clauses(text: str):
    start = 0
    for boundary in _BREAK.finditer(text):
        # "not yet pushed" is one negated proposition, not two clauses.
        # Keep the same rule for the comma form ("not, yet pushed").
        if (re.match(r',?\s*yet\b', text[boundary.start():], re.I)
                and _NEGATOR_BEFORE_YET.search(text[start:boundary.start()])):
            continue
        if start < boundary.start():
            yield start, boundary.start()
        start = boundary.end()
    if start < len(text):
        yield start, len(text)


def extract_claims(answer: str, scope: Scope, links: Mapping[str, str | None] | None = None) -> list[Claim]:
    """Return versioned, offset-stable claims; links must be explicit pre-resolved keys.

    `links` may supply `task_key`, `project_key`, `target_key` only. A title,
    filename or nearby prose is never used to derive a task/target identity.
    """
    if not answer:
        return []
    links = links or {}
    clean = _mask(answer)
    claims: list[Claim] = []
    # Full-text offsets include CRLF and all stripped presentation markers.
    offset = 0
    for line in clean.splitlines(keepends=True):
        if _SKIP_LINE.match(line):
            offset += len(line)
            continue
        for start, end in _clauses(line):
            clause = line[start:end]
            if not clause.strip() or _EXTERNAL.search(clause) or _CONDITIONAL.match(clause) or _IMPERATIVE.match(clause):
                continue
            found_done = False
            for raw_kind, regex in _RULES:
                kind = cast(Kind, raw_kind)
                for hit in regex.finditer(clause):
                    if kind == 'done' and found_done:
                        continue
                    prefix = clause[:hit.start()]
                    # Negation/future modality is local to this clause. 'Tests failed,
                    # then tests passed' is handled by the clause boundary.
                    if _NEGATION.search(prefix) or _OTHER_ACTOR.search(prefix) or _FUTURE.search(prefix):
                        continue
                    if kind == 'done' and (_INCOMPLETE.search(clause) or re.search(r'\bdone\s+(?:planning|researching|drafting)\b', clause, re.I)):
                        continue
                    if kind == 'done':
                        found_done = True
                    absolute_start, absolute_end = offset + start + hit.start(), offset + start + hit.end()
                    claims.append(Claim(
                        claim_id=claim_key(scope, kind, absolute_start, absolute_end, DETECTOR_VERSION),
                        scope=scope.copy(), kind=kind, span_start=absolute_start, span_end=absolute_end,
                        target_key=links.get('target_key'), task_key=links.get('task_key'),
                        project_key=links.get('project_key'), detector_version=DETECTOR_VERSION,
                    ))
        offset += len(line)
    return sorted(claims, key=lambda claim: (claim['span_start'], claim['span_end'], claim['kind']))
