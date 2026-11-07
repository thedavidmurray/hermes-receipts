# Optional Jev session classification (experimental)

**Off by default.** Set `HERMES_RECEIPTS_JEV=1` only in a process where you
explicitly consent to external classification of sessions. This environment
configuration flag does not install or activate anything in a live profile.
`jevkit` must be importable there; Receipts does not copy credentials, install
Jev, or modify the host. Timeout/import/privacy/malformed-response failures
fall back to deterministic categories. Without the flag there is no Jev import
or API call.

## Outbound-data disclosure

Enabling the lane sends a bounded sampled digest of assistant final text, tool
arguments (including `kanban_complete` summary/result claims) and tool results
to the external Jev API. User/system messages are excluded. Secret assignments
are masked, then **`jevkit.privacy.redact` runs before every send**. Heuristic
redaction is not complete DLP; do not enable this on confidential sessions.
No raw digest, exception text or API response is published by the runtime lane.
No private holdout labels or seals are read by the builder.

Confidence is an **uncalibrated triage score, not a probability**. Judgment
categories (`claims_backed`, `unsurfaced_error`, `verdict`) remain experimental.
The session classifier selects sections, not outcome evidence. Jev yes can add
a section; it cannot erase deterministic factual sections, certify their
contents, or override a deterministic contradiction. Existing receipt verdicts
are unchanged. `combine_verdict` deliberately returns the deterministic result
for all valid model proposals until independent gold justifies any change.
This means zero verdict lift by current policy, not a measured model benefit.

## API and auto-kit contract

`session_classifier.classify_session(messages, metadata=None, enabled=False)`
returns `categories`, `deterministic`, `jev` (`ok`, `answers`, latency/usage) and
`experimental=True`. Raw answers use `choice` with yes/no/unknown; type is one
of coding/research/ops/chat/media. `enabled=True` alone does not change verdicts.

`session_classifier.auto_fields(categories)` is the data-driven auto-kit engine:
it reads `kits/auto.json`, preserves mandatory verdict/id/claims/evidence fields
and adds sections from `when_any` rules. `receipt_from_state` exposes
`classification` and `auto_fields` to the downstream configurable-rendering
lane t_85a591f1. This lane does not replace the existing renderer or add its CLI
kit picker; the child consumes these fields rather than duplicating rules.

Section IDs to register downstream: `tests`, `push_check`, `external_readback`,
`errors`, `sources`, `artifacts`; field IDs: `files_touched`, `tokens_in`,
`tokens_out`, `tokens_total`, `cost`, `duration`, `tool_call_count`, `top_tools`.
Mandatory IDs: `verdict`, `task_session_id`, `claims_detected`,
`verdict_per_claim`, `evidence_locators`. Unavailable data renders **n/a**, never
an invented observation.

| Condition | Additional fields/verification sections | Reason |
|---|---|---|
| tests_run | tests | Invocation is not a pass: show actual outcome evidence |
| push claimed or invoked | push_check | Require remote/version readback before support |
| files_changed | files_touched | Restrict file detail to relevant sessions |
| external_side_effect | external_readback | A tool invocation alone is not confirmation |
| unsurfaced_error | errors | Advisory attention, not a contradiction verdict |
| cost_heavy | tokens in/out/total, cost | Session cost >= $1 OR >=100k tokens |
| long_running | duration | User-to-final turn wall time >=600s in history; message span in fleet |
| ops | tool count/top tools/duration | Operational activity summary |
| research | sources | No test section for pure research without test signal |
| media | artifacts | No claimed artifact success without evidence |

Session metadata is session-scoped, never silently attributed to a single turn.
Bounded digest sampling and invocation heuristics can miss/overcount evidence;
section inclusion is not detection accuracy. Thresholds are policy choices, not
optimized against private gold. Judgments are never used to hide sections.

## Reproducible authorized fleet scan

```bash
python3 scripts/jev_fleet.py --root /path/to/hermes-root \
  --output .private/jev-fleet.jsonl --jev-path /path/to/hermes-jev --workers 8
```

Selects every session with a message timestamp in the frozen last-30-day window,
including chat/no-tool sessions. All state DBs at the root and immediate profile
homes are inventoried. The manifest freezes population/window for resumability.
Rerunning resumes without duplicate calls. JSONL stores per-session answers,
latency and usage, **not raw digests**. Source access is `mode=ro` + `query_only`;
WAL without SHM fails closed. Outputs are private 0600, new parent directory
0700, `.private/` gitignored. Never attach/publish raw per-session records.
Aggregate measurement is separate from individual telemetry.

The deterministic comparison is a baseline of observed invocation/result-text
heuristics, **not independent outcome ground truth**. Unknown choices count in
the denominator and are reported separately. Per-profile population must equal
attempted = ok + failed; source inventory errors are disclosed, not silently
counted as a complete fleet. Bounded digests use uniformly sampled trace events
and reserved completion arguments; a sample can omit the critical evidence.

## Blind verifier evaluation

```bash
python -m eval.jev_holdout --handle receipts-v2-gold-final-v1 --schema
python -m eval.jev_holdout --handle receipts-v2-gold-final-v1 \
  --cases /verifier/unlabelled.json --output /verifier/predictions.json
```

The verifier alone exports unlabelled native v2 cases and calls
`score_predictions(predictions, labels_by_case_id, handle=HANDLE)` on its side.
The handle identifies frozen gold; it is not a path or credential. The runner
rejects label/seal fields and requires exact ID coverage for scoring. It reports
deterministic-only, raw Jev and conservative combined predictions and per-kind
confusion counts/precision/recall/F1. It includes scoped claim identity/span and
verifier observations, half-open time window and explicit target/version links
in the redacted digest, so the verdict question concerns
one claim, not aggregate session success. Raw model recommendations stay
experimental. Synthetic tests establish plumbing only, never frozen-gold
accuracy/calibration or lift. No thresholds may be tuned against that holdout.

## Fleet measurements — frozen 30-day run

Window **2026-10-08 22:44:32 UTC through 2026-11-07 22:44:32 UTC**, end
exclusive. **42 source DBs; 15,304 attempted = 15,301 valid replies + 3 failed**
(`JevError`: one each anomaly/hive/scribe). Every profile reconciles, including
zero-population profiles; no source inventory errors. Export additionally
checks exact `(profile, session)` set equality and rejects duplicates. The
[aggregate JSON](jev-fleet-aggregate.json) records all 42 per-profile
population/attempted/ok/failed counts, prevalence, co-occurrence and usage.

Median API latency **166 ms**, max **964 ms**. Successful replies account for
**46,592,705 input tokens + 7,316,786 output tokens**. Failed-call billing is
unknown; no dollar-cost estimate is inferred. This is not the earlier 30-session
pilot, and the full-fleet mismatches materially weaken its optimistic reading.

Factual comparison on **15,301 sessions**, against stored deterministic
invocation heuristics, **not independent outcome ground truth**. Denominator
includes unknowns (unknown is not agreement):

| Category | Agree | Disagree | Unknown | Agreement/all valid |
|---|---:|---:|---:|---:|
| claims_done | 12,245 | 2,915 | 141 | 80.03% |
| tests_run | 8,585 | 6,183 | 533 | 56.11% |
| tests_passed | 11,275 | 3,006 | 1,020 | 73.69% |
| files_changed | 13,217 | 869 | 1,215 | 86.38% |
| pushed_or_published | 14,390 | 829 | 82 | 94.05% |
| external_side_effect | 13,476 | 1,768 | 57 | 88.07% |
| cost_heavy | 14,330 | 937 | 34 | 93.65% |
| long_running | 15,179 | 117 | 5 | 99.20% |

TP/FP/FN/TN relative to the **heuristic baseline**, not semantic truth, are in
the aggregate file. In particular tests_run has 6,152 model-positive/baseline-no
cases. Baseline misses wrapped tool calls and shell/subprocess traces; Jev can
also mistake discussion for execution. Do not call these certified false
positives without independent labeling. The scan baseline predates the later
wrapped-result parser fix; response allowlisting, scoped-claim evaluation and
advisory-type union were hardened after the scan and tested offline, not
rescanned. Stored raw answers and baseline booleans preserve exactly what was
measured; do not silently rebrand it as current-revision accuracy.

### How prevalence/co-occurrence shaped the kit

- Model type prevalence: ops **7,141**, chat **3,702**, coding **2,268**,
  research **2,149**, media **41**. Universal mandatory claim/evidence/verdict
  fields plus independent type sections are preferable to an all-coding kit.
  Deterministic `session_type` remains; `jev_session_type` can add fields of a
  second type without deleting deterministic sections.
- Tests_run **6,740**, tests_passed **3,084**, overlap **2,367**: do not attach
  a universal tests section or infer a pass from a Jev yes. Outcome support
  still requires actual evidence; tests_passed alone cannot create support.
- Push/publish **598**, external side effect **1,830**, overlap **295**:
  independently gate `push_check` and `external_readback`; one is not a proxy
  for all external writes. Known sends/deploys may legitimately request both.
- Cost-heavy **6,342**, long-running **1,704**, overlap **1,565**: independent
  cost and duration branches avoid suppressing short expensive sessions.
- Files_changed **4,179**, tests_run+files_changed **3,357**: file and test
  sections are independent; not every edited session ran tests.
- Jev unsurfaced_error **7,416** and claims_backed **1,419** are experimental
  alerts, not verdicts. An error signal can add `errors`; claims_backed never
  removes evidence or upgrades support. The **856** overlap between
  claims_backed and unsurfaced_error is another reason not to trust aggregate
  judgments as evidence authority.

The full run therefore supports a **selective advisory layout**, not shipping
Jev as an autonomous correctness judge. Keep the lane opt-in and retain
conservative deterministic verdicts until independent gold scoring.

### Privacy and read-only qualifications

The manifest/JSONL/summary stay in local ignored `.private/`; do not attach them
to a public task or PR. Aggregate export contains no message text, session IDs,
API credentials or raw answers. Source SQL was read-only; SQLite WAL readers
may update transient SHM read marks. This scan did not assert source sidecar
byte/metadata immutability. It issued no source SQL writes and made no live
profile/core edits. Digests were redacted before the external calls.

Aggregate regeneration (no network; no source DB access):

```bash
python3 scripts/jev_report.py --input .private/jev-fleet.jsonl \
  --output docs/jev-fleet-aggregate.json
```

Independent frozen-gold results are **pending verifier t_3aafbace**, using only
handle `receipts-v2-gold-final-v1` in this builder environment. No private label
or seal was opened. The runner is handed off, not falsely reported as having
scored the holdout.

## Implementation verification

Final local suite: **169 passed, one Starlette/httpx deprecation warning**.

```bash
PYTHONPATH=/path/to/hermes-agent python3 -m pytest -q \
  --import-mode=importlib --rootdir=tests --confcutdir=tests tests
hermes plugins validate . --json
hermes plugins doctor . --ci
```

Each validation command ran as a separate process and exited 0; validate
reported `ok: true`, no warnings, security scan safe and no core override.
Doctor passed discovery/import/registration (0 tools, 3 hooks). Read-only review
identified missing evaluator time/version context and history initial-inference
duration; both were fixed with regression tests. Focused re-review approved
those fixes by inspection (its independent test environment lacked dependencies;
the passing full-suite execution above was performed by the implementation worker).

This is a local implementation handoff, not a public release or independent
gold-evaluation result. STANDARD release review remains with t_3aafbace after
customization t_85a591f1. Private per-session records are excluded from all
public deliverables.
