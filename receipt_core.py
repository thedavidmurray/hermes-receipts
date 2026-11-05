"""Deterministic, conservative evidence matching. No model calls or raw arguments stored."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

PATTERNS = (
    ("tests", r"\b(?:all\s+)?tests?\s+(?:pass(?:ed|ing)?|green|succeed(?:ed)?)\b|\bpytest\s+(?:pass(?:ed)?|green)\b"),
    ("commit", r"\b(?:committed|commit(?:ted)?\s+(?:the|my|our|changes|to)|created\s+(?:a\s+)?commit)\b"),
    ("push", r"\b(?:pushed|push(?:ed)?\s+to)\b"),
    ("send", r"\b(?:sent|emailed|posted|published)\b"),
    ("deploy", r"\b(?:deployed|shipped\s+to\s+(?:production|prod))\b"),
    ("write", r"\b(?:created|wrote|saved|patched|edited)\s+(?:a\s+|the\s+)?(?:file|[\w./-]+\.(?:py|js|ts|md|json|yaml|png))\b"),
    ("run", r"\b(?:ran|executed)\s+(?:the\s+)?(?:[\w./-]+\s+)?(?:command|script|pytest|tests?|build)\b"),
)
# An explicit action for another executor is not a failed claim about this turn's tools.
EXTERNAL = re.compile(r"\b(?:subagent|other agent|another process|external job|CI|GitHub Actions)\b", re.I)
NEGATED = re.compile(r"\b(?:not|never|didn.t|haven.t|couldn.t|failed to|no)\b", re.I)
SECRET = re.compile(r"(?i)(?:token|password|secret|api[_-]?key|authorization|bearer)\s*[=:]?")
_ACTIONS = {"tests": "test", "commit": "commit", "push": "push", "send": "send", "deploy": "deploy", "write": "write", "run": "run"}


def extract_claims(answer: str) -> list[dict]:
    """Extract affirmative action assertions; avoid code blocks and quoted instructions."""
    answer = re.sub(r"```.*?```|`[^`\n]+`", " ", answer or "", flags=re.S)
    claims = []
    for line in answer.splitlines():
        line = line.strip().lstrip("-*• 🔥").strip()
        if not line or line.startswith((">", "#", "http")):
            continue
        for segment in re.split(r"(?<=[.!?;])\s+|\s+[—–]\s+", line):
            for kind, pattern in PATTERNS:
                match = re.search(pattern, segment, re.I)
                if not match:
                    continue
                prefix = segment[max(0, match.start() - 16):match.start()]
                if NEGATED.search(prefix) or re.match(r"(?i)^(?:if|when|please|should|must|to|will|can|would)\b", segment):
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
    if tool in ("write_file", "patch", "mcp__magicpath__submit_component_code"):
        value = args.get("path", "")
        return "write", Path(str(value)).name[:60] if value else "file"
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
        eligible = [r for r in rows if r.get("action") == _ACTIONS[claim["kind"]]]
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
