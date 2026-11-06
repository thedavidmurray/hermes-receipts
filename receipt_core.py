"""Deterministic, conservative evidence matching. No model calls or raw arguments stored."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

PATTERNS = (
    ("tests", r"\b(?:all\s+|\d+(?:/\d+)?\s+)?tests?(?:\s+\+\s+[\w\s]+)?\s+(?:pass(?:ed|ing)?|green|succeed(?:ed)?)\b|\bpytest\s+(?:pass(?:ed)?|green)\b"),
    ("commit", r"\b(?:committed|commit(?:ted)?\s+(?:the|my|our|changes|to)|created\s+(?:a\s+)?commit)\b"),
    ("push", r"\b(?:pushed|push(?:ed)?\s+to)\b"),
    ("send", r"\b(?:sent|emailed|posted|published)\b"),
    ("deploy", r"\b(?:deployed|shipped\s+to\s+(?:production|prod))\b"),
    ("write", r"\b(?:created|wrote|saved|patched|edited|added|updated|generated|drafted|fixed)\s+(?:(?:a|the|an|two|three)\s+)?(?:file|task|card|note|report|draft|script|page|module|scaffold|[\w./-]+\.(?:py|js|ts|md|json|yaml|png))\b|\b(?:file|note|report|draft|[\w./-]+\.(?:py|js|ts|md|json|yaml|png))\s+(?:written|created|generated|rewritten)\b"),
    ("run", r"\b(?:ran|executed)\s+(?:the\s+|daily\s+)?(?:[\w./-]+\s+)?(?:command|script|pytest|tests?|build|enrichment|reporter)\b"),
)
# An explicit action for another executor is not a failed claim about this turn's tools.
EXTERNAL = re.compile(r"\b(?:subagent|other agent|another process|external job|CI|GitHub Actions)\b", re.I)
NEGATED = re.compile(r"\b(?:not|never|nothing|neither|none|zero|0\s+(?:new|other|additional)?|didn.t|haven.t|hasn.t|wasn.t|weren.t|couldn.t|failed to|without|no)\b", re.I)
OTHER_ACTOR = re.compile(r"\b(?:you|your)\b", re.I)
SECRET = re.compile(r"(?i)(?:token|password|secret|api[_-]?key|authorization|bearer)\s*[=:]?")
_ACTIONS = {"tests": "test", "commit": "commit", "push": "push", "send": "send", "deploy": "deploy", "write": "write", "run": "run"}


def extract_claims(answer: str) -> list[dict]:
    """Extract affirmative action assertions; avoid code blocks and quoted instructions."""
    answer = re.sub(r"```.*?```", " ", answer or "", flags=re.S)
    answer = re.sub(r"`([^`\n]+)`", lambda m: m[1] if re.search(r"\.(?:py|js|ts|md|json|yaml|png)\b", m[1]) else " ", answer)
    claims = []
    for line in answer.splitlines():
        line = line.strip().lstrip("-*• 🔥").strip()
        if not line or line.startswith((">", "#", "http")):
            continue
        for segment in re.split(r"(?<=[.!?;])\s+|\s+[—–]\s+|\s+(?:but|then|and\s+I)\s+", line, flags=re.I):
            for kind, pattern in PATTERNS:
                match = re.search(pattern, segment, re.I)
                if not match:
                    continue
                prefix = segment[:match.start()]
                if (NEGATED.search(prefix) or OTHER_ACTOR.search(prefix)
                        or re.match(r"(?i)^(?:if|when|please|should|must|to|will|can|would)\b", segment)):
                    continue
                claims.append({"kind": kind, "text": segment[:100], "external": bool(EXTERNAL.search(segment))})
    return claims


def digest(args: object) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def classify(tool: str, args: object) -> tuple[str, str]:
    """Only keep action type and basename, never command text or sensitive values."""
    args = args if isinstance(args, dict) else {}
    command = str(args.get("command") or args.get("code") or "")[:4096]
    lower = command.lower()
    if tool in ("terminal", "execute_code"):
        for action, pattern in (("test", r"\b(?:pytest|unittest|npm test|pnpm test|cargo test|go test)\b"),
                                ("push", r"\bgit\s+(?:-c\s+\S+\s+)*push\b"),
                                ("commit", r"\bgit\s+(?:-c\s+\S+\s+)*commit\b"),
                                ("deploy", r"\b(?:wrangler deploy|vercel deploy|fly deploy|kubectl apply)\b"),
                                ("send", r"\b(?:gh pr create|gws gmail send|sendmail)\b")):
            if re.search(pattern, lower):
                return action, action + " command"
        return "run", "command"
    if tool in ("write_file", "patch", "kanban_create", "kanban_comment", "mcp__magicpath__submit_component_code"):
        value = args.get("path", "")
        return "write", Path(str(value)).name[:60] if value else ("task" if tool.startswith("kanban_") else "file")
    if any(w in tool.lower() for w in ("send", "publish", "post_message")):
        return "send", "outbound tool"
    return "other", tool[:40]


def _success(row: dict) -> bool:
    return row.get("status") == "ok"


def match_claims(answer: str, rows: list[dict]) -> list[dict]:
    claims = extract_claims(answer)
    for claim in claims:
        if claim["external"]:
            claim.update(verdict="not observed", evidence="outside this agent's tool stream")
            continue
        action = _ACTIONS[claim['kind']]
        if claim['kind'] == 'run' and re.search(r'\b(?:pytest|tests?)\b', claim['text'], re.I):
            action = 'test'
        eligible = [r for r in rows if r.get('action') == action]
        if claim['kind'] == 'write':
            # Do not let writing an unrelated file (or creating a card) validate this claim.
            target = re.search(r"[\w./-]+\.(?:py|js|ts|md|json|yaml|png)\b", claim['text'], re.I)
            if target:
                basename = Path(target.group()).name.lower()
                eligible = [r for r in eligible if str(r.get('label', '')).lower() == basename]
            elif re.search(r"\b(?:task|card)\b", claim['text'], re.I):
                eligible = [r for r in eligible if r.get('tool') == 'kanban_create']
            elif re.search(r"\bfile\b", claim['text'], re.I):
                eligible = [r for r in eligible if r.get('tool') in ('write_file', 'patch')]
        # A tool invocation is proof of an invocation, not proof of the claimed outcome.
        good = [r for r in eligible if _success(r)]
        if good:
            row = good[-1]
            claim.update(verdict="✓", evidence=f"{row['tool']} #{row.get('call_id', '')[:8]} ({row['status']})")
        elif eligible:
            claim.update(verdict="✗", evidence=f"{eligible[-1]['tool']}: {eligible[-1].get('status', 'unknown')}")
        else:
            claim.update(verdict="✗", evidence="no matching tool call observed")
    return claims


def ledger_hash(rows: list[dict]) -> str:
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(canonical).hexdigest()
