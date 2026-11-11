# Receipts v2 — Your agent says it's done. Get the receipt.

Architecture decision, 2026-11-04. Code target **Nov 9 EOD PT**; submission cutoff **Nov 11, 1PM PT**. This document is the build contract, not a claim that v2, its evaluation targets, or its physical print demonstration already exist.

## 1. Product and scope

Receipts is **proof that the agent did what it says it did**, within a declared evidence scope. It is not a session-metadata summary. The unit is a **claim checked against observed evidence**. The hero is the **completed task**: “done” is the claim that matters most. A task receipt shows the requested outcome, required acceptance criteria, evidence for each, and unresolved items. A successful tool invocation, a status update, or `kanban_complete` does not establish completion.

The same findings drill **down to individual replies** and roll **up to projects, agents/profiles, models, sources/gateways, sessions and time windows**. Atomic-action support and completion support have separate numerators. Ten successful writes cannot hide an unsupported “done.” Cost and tokens are a footer line, not the headline.

Visible qualification on EVERY supported completion: **“Declared required criteria supported for [artifact version] in [observation window]; not unrestricted proof of completeness.”** Missing criteria, ambiguous task linkage, or missing required outcome evidence leaves completion unverified. Optional requirements do not block completion; the receipt explicitly distinguishes them. External/delegated work is not called a lie.

Minimum Nov9 vertical slice: explicit local task manifest → evidence compiler → task PNG; existing Desktop current-session pane/button, with session isolation repaired; existing USB transport; same compiler produces monthly/project/profile/model/source reports via local CLI. Defer automatic Kanban interception, new Desktop picker/filter UI, remote outcome probes, live fleet API, advanced visuals, and new printer drivers. Do not defer evidence/privacy/evaluation gates.

## 2. Baseline and pre-prompt findings

Read v1 `__init__.py`, `receipt_core.py`, renderer/printer, dashboard/Desktop, tests, eval and hackathon docs. v1 at `89123a5c18bf8f25cd75f146e73971d9965ee86e` has nine tests and a real-session PNG. Existing 200-answer enriched sample: TP52 FP10 FN42 TN96, P=.8387 R=.5532. **.8387 is below .84**, not a newly passed gate. This measures answer-level detection, not verdict correctness. Verdict accuracy is unmeasured.

Current matcher conflates absent calls with failed outcomes, and treats generic successful classified calls too strongly. Existing historical reader respects user-turn boundaries. Public SDK has no per-reply button slot: keep pane, no core/DOM patch. Hook metadata lacks enough outcome detail by itself. Session schema observed on this host includes `source`, `model`, `profile_name`, `git_repo_root`, `cwd`; schemas vary. Use schema introspection, not hardcoded availability. Session-model attribution is not per-reply attribution.

The crude Hive probe (including 78% “pushed” unbacked and Kanban/Discord differences) is **unvalidated**, confounded by delegated work and language. Never use it as a lie-rate claim or demo result. Consult `crude-claim-probe.md` only as hypothesis input. A QMD prior-work query was attempted; source-of-truth grounding here is the repository and shared project notes, not an asserted KB consensus.

## 3. Fixed interfaces before build

Lane A lands these declarations in `v2/contracts.py` before consumers. No worker independently renames fields/enums. JSON exports use snake_case. `schema_version=2`; independently version detector and matcher rules. IDs use SHA256 of canonical composite identities, not public raw identifiers. Typed declarations below are prototypes, not implemented APIs:

```python
from typing import Literal, TypedDict
Verdict = Literal['supported', 'contradicted', 'unverified', 'not_evaluable']
Kind = Literal['tests','commit','push','send','deploy','write','run','done']
class Scope(TypedDict):
    source_id: str       # private stable per-realpath DB identity
    profile: str        # persisted profile_name preferred; unknown allowed
    session_id: str
    user_message_id: str
    final_message_id: str
class Attribution(TypedDict):
    model: str          # 'unknown' if absent
    source: str         # persisted session.source or 'unknown'
    certainty: Literal['exact','session_only','unknown']
class Claim(TypedDict):
    claim_id: str
    scope: Scope
    kind: Kind
    span_start: int
    span_end: int
    target_key: str | None
    task_key: str | None
    project_key: str | None
    detector_version: str
class Evidence(TypedDict):
    evidence_id: str
    scope: Scope
    call_id: str
    action: str
    target_key: str | None
    artifact_version: str | None
    observed_at: float
    level: Literal['invocation','process_result','outcome_readback']
    status: Literal['ok','error','unknown']
class Finding(TypedDict):
    claim_id: str
    verdict: Verdict
    reason_code: str
    evidence_ids: list[str]
    matcher_version: str
class Requirement(TypedDict):
    requirement_id: str
    public_label: str   # explicitly reviewed, not arbitrary raw task text
    required: bool
    evidence_predicate: str  # fixed supported predicate ID, never eval()/shell
    target_key: str | None
    artifact_version: str | None
class TaskReceipt(TypedDict):
    task_key: str
    declared_scope_label: str
    observation_start: float
    observation_end: float
    requirements: list[Requirement]
    criterion_findings: dict[str, Finding]
    completion: Finding
    unresolved_requirement_ids: list[str]
```

Module ownership/API signatures:
- A: `v2/store.py`, `v2/fleet.py`, `v2/contracts.py`; `iter_turns(roots, start, end, limits)`, `upsert_run(db, entities)`, `query_rollup(db, filters, group_by)`.
- B: `v2/detect.py`; `extract_claims(answer, scope, links) -> list[Claim]`; raw answer only in memory. Keep v1 wrapper compatible.
- C: `v2/evidence.py`, `v2/tasks.py`; `normalize_evidence(turn)`, `judge_claims(claims,evidence)`, `judge_task(requirements,findings,window)`. No criterion evaluated by arbitrary code from manifests.
- D: `v2/public.py`, `v2/render.py`, `v2/cli.py`; `to_public_receipt(private, reviewed_labels)`, `render_task_png(public, output)`, `render_report_png(public, output)`. Modify existing Desktop/API only here.
- Integration owns adapter wiring in `__init__.py`, release eval runners and final catalog pin. No shared-file races: build chain is A→B→C→D; integration has ALL four as direct parents.

Local CLI prototypes (implemented by D, frozen here):
```
hermes receipt task --manifest task.json --png task.png
hermes receipt fleet --roots <explicit-local-root> --start <ISO8601> --end <ISO8601> --report-db <path>
hermes receipt report --report-db <path> --group-by profile,model,source --png statement.png
```
Root paths accepted only by explicit local CLI, never web/chat arguments. Preserve v1 `last` and `verify`. No new `transform_llm_output` or gate required for v2.

## 4. Store, fleet backfill and grouping

Current-profile v2 tables in `plugin_db('receipt-printer')`, prefixed `v2_`; v1 ledger untouched. Explicit fleet CLI writes an external user-chosen report DB using same schema. Tables:
- `v2_sources(source_id PRIMARY KEY, schema_version, availability, cutoff, coverage_reason)`.
- `v2_turns(turn_key PRIMARY KEY, source_id, profile, session_id, user_id, final_id, final_time, model, model_certainty, source, project_key)`.
- `v2_claims(claim_id PRIMARY KEY, turn_key, kind, span_start, span_end, target_key, task_key, detector_version)`.
- `v2_evidence(evidence_id PRIMARY KEY, turn_key, call_id, action, target_key, artifact_version, observed_at, level, status)`.
- `v2_findings(claim_id, matcher_version, verdict, reason_code, evidence_ids_json, PRIMARY KEY(claim_id,matcher_version))`.
- `v2_task_links(task_key,claim_id,provenance)` and `v2_requirements(task_key,requirement_id,required,predicate,target_key,artifact_version,reviewed_public_label)`.

Persist no raw answer, command, argument, result, secret, raw target or arbitrary error text. Target fingerprints are keyed locally, keys never exported. Deterministic upserts include source, profile, session, turn, span/kind and versions. A report selects exactly one detector/matcher version: re-import never double counts. Index turn `(profile,session_id,final_time)`, `(model,source,final_time)`, claims `(task_key,kind)`.

Discover default `~/.hermes/state.db` plus named `~/.hermes/profiles/*/state.db` under explicit allowed root; dedupe real paths, account for multiplexed DBs using persisted profile_name. Never inspect configs/credentials for attribution. Unknown remains visible. Explicit task manifest or persisted structured task ID is required; project uses explicit mapping or recorded git_repo_root (privately fingerprinted), not title guessing.

**MVP uses streaming read transactions, NOT raw DB snapshots.** For every source: URI `mode=ro`, `PRAGMA query_only=ON`, introspect tables/columns, bounded consistent transaction then release. Never `immutable=1` on active WAL, never checkpoint/repair/create source files. SQLite RO WAL may need SHM creation; test no-write boundary. If unavailable without writes, record unavailable and request an operator-provided consistent snapshot; no unsafe fallback. No checkpoint/copy of just a live .db. Each source has its own cutoff; fleet report is not a globally atomic snapshot. Complete assistant finals selected without keyword prefilter; exclude display-only/compacted records if schema marks them. Missing/truncated/ambiguous history cannot establish contradiction or success.

Resource limits: single worker, 500-row pages, 30-day default window, explicit all-time opt-in, default max100k finals/source and 5-minute total budget, cancellation; exceeding limits records partial coverage/cursor. No model calls or background polling. Test quiescent fixtures incl DB/WAL/SHM byte hashes and file creation audit; do not infer no writes from unstable hashes on live databases.

UTC `[start,end)` throughout, optional PT presentation. Source/model unknown buckets retained; never infer historical model from today's config. Session costs/tokens counted once separately, not repeated per turn or attributed to mixed-model replies.

Query sketch (parameterized values; group_by from enum allowlist, never SQL interpolation from user input):
```sql
SELECT t.profile, t.model, t.source,
       c.kind='done' AS completion_claim,
       f.verdict, COUNT(*) AS n
FROM v2_claims c JOIN v2_turns t ON t.turn_key=c.turn_key
JOIN v2_findings f ON f.claim_id=c.claim_id
WHERE t.final_time>=:start AND t.final_time<:end
  AND c.detector_version=:detector AND f.matcher_version=:matcher
GROUP BY t.profile,t.model,t.source,completion_claim,f.verdict;
```
Session drilldown filters `source_id/profile/session_id`, replies by `turn_key`; project/task filter their explicit keys. Completion report groups task completion findings once per task/version/window rather than every repeated “done” utterance. Raw claim counts and unique task denominator are shown separately.

## 5. Evidence policy and honesty statement

Same user-turn evidence is default. Cross-turn task evidence requires explicit task, target AND artifact-version linkage; no automatic borrowing from other agents, profiles, future turns, or matching filenames. Current readback cannot retrospectively prove a historical state. Optional external probe is deferred, never default network.

- `supported`: evidence supports the **specific** proposition at its stated level/scope.
- `contradicted`: explicit applicable outcome evidence contradicts it; failed attempt alone cannot contradict later success.
- `unverified`: insufficient/missing evidence (including no call); NOT “false” or “lie.”
- `not_evaluable`: claim/target/attribution outside evaluator capability, ambiguous/truncated/external stream; count and explain.

Wrapper `execute_code` exit0 is not proof of nested tool effects; compound-shell status may hide failures. A recorded write is invocation/process evidence, not content readback. “Tests passed” requires identified test process/result; broader criterion additionally binds artifact version. A local commit is not a remote push. Matching wrong target/version is never sufficient. v2 uses words/4-way symbols rather than v1's overloaded ✗. Preserve and disclose v1 semantics on legacy receipts; gate stays off for v2 by default.

Monthly PNG: thermal statement with scoped task/criterion example, then counts and evidence-backed rates by selected dimensions. **Rate = supported / (supported + contradicted + unverified)**; not_evaluable is excluded but adjacent count/coverage mandatory. Done/task and action rates separate. No detected claims => N/A; n<10 => counts only/insufficient sample. Wilson95 interval describes detected-claim sample uncertainty only, not missed claims/model uncertainty/causality. “Honesty” means **observed evidence-backed claim rate, not lie rate**. Show complete/partial fleet coverage, unknown attribution, dates and versions. No model leaderboard for sparse/mixed attribution.

## 6. Recall and verdict evaluation

Targets, NOT achieved results: detection P≥.84, R≥.75; verdict macroF1≥.85, support precision≥.95, contradiction precision≥.90. Failures ship at most an explicitly experimental viewer; do not call v2 accepted, invent numbers, or tune on holdout.

Old200 is development only. Review FN categories to validate target hypothesis: passive voice, bullet shorthand, inflections/paraphrases, clause-local negation. First-class done is new, report it separately rather than inflating seven-class recall. Remove code/quotes/plans/imperatives/external-report false positives; match original full-text offsets.

Freeze 240 NEW session-disjoint full answers before tuning: 120 seeded random eligible finals,120 seeded hard/enriched stratum. Reserve entire sessions, record hashes/selection seed/exclusions, no keyword-only negative sampling. Seven-class answer-level P/R on random stratum is primary; enriched stratum separately, never pooled as population precision. Claim-level one-to-one same-kind and overlapping gold-span matching, maximum bipartite assignment, unmatched prediction FP/gold FN. Done-specific detection report separate. Publish confusion counts/denominators/intervals; small positive counts may be underpowered, say so.

Verdict gold≥120:90 synthetic controlled cases+30 reviewed real evidence cases, including≥30 completion claims. Two independent labels of complete inputs and policy, disagreements adjudicated with rationale by independent reviewer; tuner never adjudicates its own test. Four-way matrix, macroF1, support/contradiction precision AND support/abstention coverage, done-specific performance, missing-label accounting. Abstaining on everything cannot meet probe acceptance.

**Capacity assignment:** dedicated verifier gold-label card starts concurrently; verifier performs primary labeling, invokes isolated critic as independent secondary labeler (actual profile confirmed), then reviewer adjudicates with evidence log; no detector implementation access until gold freeze. This strengthens round2's Kilo-primary proposal to avoid tuner label leakage. Private labels/raw text in separate 0700 workspace, not repo/public artifacts; only reviewed synthetic fixtures, aggregate scores, rules and sanitised manifest committed. No labels/answer snippets through board comments. Deliver frozen gold by Nov6; missing independent capacity is a surfaced blocker, not pretend independence. Same-provider judge contexts are independent conversations, not independent models or human truth.

Mandatory adversarial suite: wrong target/version/profile; failure then retry; absent/late/future result; nested/compound wrappers; external Kanban reporting another agent's commit; unassociated done; missing required criteria; truncated history. **Zero false support and zero false contradiction on this fixture suite.** Final evaluator runs independently, not merely a self-reported test summary.

## 7. OUTCOME VERIFICATION PROBE

Integration builds a controlled scratch task with explicit required criteria: exact artifact bytes/digest, specified test passing against that digest, and commit reachable from a **local bare Git remote**. No network needed. Public labels reviewed: “Artifact bytes match”, “Tests pass for this version”, “Commit present in remote”.

Run variants: successful wrapper but missing artifact; wrong target; stale tests; failed then repaired test; absent push; legitimate explicitly linked multitur n evidence; no criteria. Independent verifier reads actual files and bare remote refs and compares oracle to criterion findings/task completion. Only final fully satisfied required scope may show supported done; negative cases cannot. Rerun identical sources: no duplicate findings, identical canonical results, no source DB writes. Generate task and statement PNGs; inspect both pixels and metadata with secret canaries. A digest is reproducibility/integrity, not signature/attestation.

Print user-invoked to real USB only if available: observer confirms physical paper, not just submission ACK. No hardware => PNG fallback explicitly stated, physical demo unverified. This planning task does not execute the future outcome probe or claim print success.

## 8. Presentation, privacy, catalog

One allowlisted `PublicReceipt` supplies PNG and printer: pseudonyms, verdict enums, criterion aliases deliberately reviewed for sharing, coarse dates, counts, versions and warnings. No arbitrary raw strings, paths/session IDs/URLs/target labels/argument digests/errors. Meaningful synthetic demo labels preapproved; real public criterion labels require deliberate review, never automatic task-text export. Private drilldown stays local. Bound image height/paginate; redact filenames/logs/PNG metadata as well as pixels. No raw snapshots in MVP; normalized private data can still be sensitive, document deletion/output retention and pseudonym-map separation.

Desktop existing pane only: “Get the receipt” for active session, reject missing session rather than silently choose latest, invalidate stale async result on session switch; backend validates requested final belongs active session/profile. Current-profile backend only, fleet CLI inaccessible remotely. Auth router integration tests plus actual disposable Desktop/browser observation required for changed UI. No SDK/core patch.

Rule13 disclosure updated when implemented: explicit fleet local opt-in outside reads (including sensitive in-memory conversations/args), sanitised stored entities/report PNG retention, no inference/network/export, explicit USB access, permissions and incomplete coverage. Legacy v1 exports contain excerpts/basenames/IDs: mark legacy PRIVATE/review-before-share; do not imply new allowlist retroactively sanitizes v1. Optional output stamp remains off: `transform_llm_output` is first-non-None-wins and may suppress/be suppressed by competitors. Never rely on stamp to generate receipt, never enable completion blocking for this release. No approval bypass. Exact release SHA only in catalog after validate/doctor; David owns PR/X/form submission.

## 9. Demo ≤90 seconds

0–10: “Your agent says it's done. Get the receipt.” Open a task marked complete.
10–25: Press existing pane button / local task receipt command; explicit required criteria and observation scope.
25–35: Unverified push criterion: agent's done claim not established. No-call ≠ lie.
35–50: Print thermal receipt; observer sees paper. If hardware absent, explicitly use PNG fallback, never fake print.
50–65: Execute genuine repair, independently read remote/artifact/test version, regenerate supported scoped task receipt.
65–80: Monthly profile/model/source card rolls the same claims up; project grouping shown; unknowns/denominators visible.
80–90: Actual measured detector/verdict scores and limits. Tokens/$ one footer line.
Prefer a reviewed real Kanban card only if criteria/version evidence complete and safe; controlled fixture is an explicitly labelled demo, not fictional real-session telemetry.

## 10. Execution lanes and risk gates

One Kilo profile-day each, sequential Nov5–8; Nov9 half-day integration + independent release review. Capacity is conditional: Kilo also has demo-reel; coordinator reassesses **Nov5 noon PT**, prioritize receipts, cut polish not truth, record schedule slip if capacity unavailable. No live profile or Hermes-core changes. Every lane keeps nine original tests, new regressions, disposable validate+doctor green. Docs/screenshots alone do not satisfy build cards. Code changes require independent release review, production activation not authorized.

A inventory/store/grouping/contracts; B deterministic detector/dev eval; C outcome/task matcher/verdict; D public PNG/current-pane/printer/docs. Separate verifier label card starts immediately after plan and owns frozen gold; integration depends on all four builders and gold. Separate final verifier release card depends on integration. Reproducible v1 test gate: `PYTHONPATH=/path/to/hermes-agent /path/to/hermes-agent/.venv/bin/python -m pytest -q --import-mode=importlib --confcutdir=tests tests/test_receipts.py`. Validate and doctor a clean staged plugin copy under a disposable HERMES_HOME, outside the source tree (placing the temporary home inside the scanned tree caused recursive scanning/timeouts). Baseline independently rerun: nine tests passed; `cmd_validate(staged_path, install_deps=False)` and `cmd_plugin_doctor(staged_path, ci=True)` passed using the installed Hermes Python entry points, without bootstrap or live-profile changes. No overlapping same-directory writers; verifier read-only or isolated private workspace. Deliver human-facing PNG + summary, **never .md/.zip**; source/docs remain repo artifacts.

## Execution graph

Confirmed specialist profiles: `kilo`, `verifier`, `critic`. Build cards route to **kilo**, not the architecture profile `edgeless-cc`.

|Lane|Card|Direct prerequisites|
|---|---|---|
|A contracts/store/fleet|t_206a8a20|planning t_acad9db0|
|B detector|t_47a5ff18|A|
|C outcome/task matcher|t_8889e05f|B|
|D public PNG/pane/print|t_27f2ac71|C|
|Independent frozen gold|t_85c07673|planning t_acad9db0|
|Integration/eval|t_c34db710|A, B, C, D, independent gold|
|Independent release verification|t_3aafbace|integration|

## 11. Autoreason A/B/AB — two rounds, stopped at cap

Used `software-development/autoreason-moa`; exact `autoreason` resolver unavailable. Independent B author (`deleg_81bcfb98`) read repo; coordinator condensed B (not an untouched independent submission). Fresh isolated judges, all same subscription model: independence of contexts, NOT model diversity. Unequal lengths remain a bias; judges explicitly told ignore detail volume and penalize scope. Operator task-first rubric given identically. Titles masked; wording may reveal synthesis, so blindness is label/authorship masking, not guaranteed content anonymity. Round2 common incumbent retains title; only revision identities masked. Audit candidate/permutation packets in `planning/`.

Borda2/1/0, no ties:

|Round|Judge|Blind mapping X/Y/Z|Raw best→worst|A|B|AB|
|---|---|---|---|---:|---:|---:|
|1|Hackathon win|AB/A/B|Y>X>Z|2|0|1|
|1|Ship Nov9|B/AB/A|Y>Z>X|1|0|2|
|1|Evidence/privacy|A/B/AB|Z>Y>X|0|1|2|
|1|TOTAL|||3|1|5|
|2|Hackathon win|B/A/AB|Z>X>Y|0|1|2|
|2|Ship Nov9|AB/B/A|X>Y>Z|0|1|2|
|2|Evidence/privacy|A/AB/B|Y>X>Z|1|0|2|
|2|TOTAL|||1|2|6|

Round1 AB won5/3/1. Defects: unbounded done wording, unassigned labeling capacity, final UI lane overload, raw snapshot crash residue. Round2 A=incumbent, B=streaming CLI-only reduction (cuts required demo surfaces), AB=streaming task-first minimum slice with assigned label capacity and retained existing button/printer. Fresh judges unanimously chose AB6/B2/A1. Fixed scope qualification and snapshot removal, explicit minimum slice/deferrals; label ownership further separated from tuner as noted above. Stop after two rounds, no endless optimization. Transcripts: `deleg_6294364f` round1, `deleg_03cd46e2` round2. Final caveat: target performance, printer hardware and calendar capacity remain empirical gates, not solved by voting.
