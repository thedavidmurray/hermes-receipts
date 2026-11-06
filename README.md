# Receipts for Hermes 🧾

**Your agent says it's done. Get the receipt.**

Receipts checks what an agent claims against observed evidence—not a session-metadata summary. The completed task is the hero: did the declared required outcomes actually happen? Individual reply claims are the evidence units; the v2 plan rolls them up to projects and per-agent/per-model evidence-backed rates, with cost and tokens only a footer.

**Available today (v1 release):** turn-by-turn thermal-paper receipts for what Hermes *observed*. Ledger hooks see each tool call; historical receipts read this profile's existing `state.db` immediately on install. No LLM calls. **v2 source under integration, not an accepted release:** a local manifest can produce a scoped task PNG, an explicitly opted-in local fleet scan can produce a grouped statement PNG, and the existing Desktop pane shows an unverified current-session preview. Independent accuracy and outcome acceptance are pending; see [PLAN-v2.md](PLAN-v2.md). Missing evidence is not proof of a lie, and a successful tool invocation alone is not proof that a task is complete.

![Controlled scoped task receipt — synthetic evidence, not production telemetry](assets/v2-controlled-task.png)

The example is generated from actual controlled artifact bytes by `tests/create_demo.py`. It is **not** real fleet telemetry or observed USB paper. A supported completion only means *declared required criteria supported for the displayed version and window*, not unrestricted proof of completeness. [Controlled statement PNG](assets/v2-controlled-statement.png) shows the same small synthetic sample with counts only (n<10), not a population rate.

![Real session receipt](assets/real-receipt.png)

## Install / use

```bash
hermes plugins install thedavidmurray/hermes-receipts
hermes plugins enable receipts
hermes receipt last --png receipt.png
hermes receipt verify <full 64-character ledger sha256>
```

`/receipt` in CLI/Discord/Telegram renders the latest persisted **private v1** turn as text plus `MEDIA:` PNG; `/receipt <final-message-id>` selects a prior final answer. `hermes receipt last --session <id>` narrows a profile's session. Desktop has a **Receipts** pane and “Get the receipt” button for the active session: it requests a public v2 **unverified preview** only, since the pane does not accept a task manifest. Missing sessions fail closed, and requests from a prior session switch are discarded. The public Desktop SDK has no per-turn button contribution, so this does **not** patch transcript markup. Enable the Python plugin in Hermes and the Desktop component in Capabilities → Plugins; the latter alone cannot load the backend.

Experimental, local-only v2 commands (not an accepted release):

```bash
hermes receipt task --manifest /local/task.json --png /local/task.png
hermes receipt fleet --roots /explicit/local/hermes-root --start 2026-11-01T00:00:00Z --end 2026-12-01T00:00:00Z --report-db /local/report.db
hermes receipt report --report-db /local/report.db --group-by profile,model,source --png /local/statement.png
```

Task manifests must provide a linked `done` claim, explicit required criteria and `[start,end)` UNIX observation timestamps; the optional `probes` list explicitly names an `artifact_digest` file+SHA256 or `remote_commit` local bare remote+commit. It performs actual local readbacks; it **does not** trust supplied findings/evidence, execute manifest shell commands, or run network probes. Test-bound criteria require a trusted in-process runner and remain unverified via this CLI. Only explicit `reviewed_labels` from the built-in shareable alias vocabulary are rendered; everything else becomes `Criterion N`. Rates are supported/(supported+contradicted+unverified), with not_evaluable beside the denominator; n<10 shows counts only. Unknown model/source buckets and incomplete coverage remain visible. A digest is not a signature. Fleet reads are explicitly opt-in via local CLI, never through HTTP or chat.

≤90-second demo storyboard: 0–10 task-first hook; 10–25 local manifest and required scope; 25–35 missing push stays unverified; 35–50 show real observed paper **if** hardware is present, otherwise label PNG fallback; 50–65 perform real repair and independent artifact/test/remote readback; 65–80 group statement by profile/model/source or project; 80–90 report measured accuracy and coverage limits, with tokens/cost only in the footer. A controlled artifact is labelled a controlled demo, not a sampled agent session.

Optional: `hermes receipt last --print-usb 0xVVVV 0xPPPP` uses `python-escpos>=3,<4` installed separately. It fails with a readable error if the package, printer or USB permissions are absent. This is never automatic.

The optional response stamp is **off**: `HERMES_RECEIPTS_STAMP=1` enables a legacy `🧾 n✓ m✗` suffix, not a v2 task judgment. `transform_llm_output` uses first-non-None-wins arbitration; other response transforms can conflict. The optional `pre_tool_call` gate is **observe-only by default**: setting `HERMES_RECEIPTS_GATE=block` blocks `kanban_complete` only when the *last persisted answer of the same session* has an unbacked v1 claim; missing session IDs and missing history fail open. This is a review prompt, not proof of current-turn failure, and does not approve any tool. Do not enable the legacy stamp or gate for the v2 demo; they can conflict with other transforms or block from stale answer text.

## How to read this

✓ means a matching tool call in the **same user turn** has an explicit success result. ✗ means a matching tool failed or was not recorded; it is **not proof the action never happened**. “not observed” denotes explicit subagent, CI or out-of-process work. Unknown tool results cannot be marked successful. Claims are conservative regex detections, not general natural-language entailment. In particular, a `git push` command exiting zero is evidence of command completion, *not* read-back proof that the remote branch changed; tests exiting zero are evidence of that process, not that all possible tests pass. A SHA-256 hash checks the canonical ledger rows reconstructed from `state.db`, not a signature, tamper-resistant audit log, or external attestation. No tool result bodies appear in the receipt.

The tool hook writes `tool name`, `args SHA-256`, a coarse action category, status, duration and session/turn/call ids to profile-scoped `plugin_db('receipts')`. Raw args and results are not stored there. The history reader opens **only this profile's** `state.db` in SQLite `mode=ro`, and reconstructs turns lazily from the preceding user message, assistant `tool_calls`, matching tool responses and final answer. Existing turns need no import/migration. For historical turns Hermes records neither per-turn tool duration nor per-turn usage: these are shown as unavailable, with separate **session** token/cost totals if present. Wall time is the historical user→final interval. Ledger coverage is limited by persisted tool-call history; tool calls inside `execute_code`, subagents, shell children and separate CLI jobs are not attributed to a parent unless Hermes recorded them in that parent turn.

## Disclosure (catalog rule 13)

- **Outside reads:** by default only the current profile's `state.db` read-only; an **explicit local** `receipt fleet --roots ...` opt-in also reads state DBs under the selected roots, possibly including other profiles. Message text, conversation history, tool arguments and results can contain secrets and are processed in memory. The local `task --manifest` probes explicitly named artifact files or a local bare Git remote. No browser login, implicit cross-profile scan or remote probe.
- **Stored data and retention:** v1 keeps a profile-scoped SQLite ledger and private PNGs under `plugin-data/receipts/`. Legacy v1 `/receipt`, `last` and `verify` outputs contain answer excerpts, basenames, ids and hashes: **PRIVATE, review before sharing**. The v2 fleet writes sanitized, versioned metadata and coverage to the user-chosen `--report-db`; reports and task PNGs remain at the user-chosen output path **until the user deletes them**. The current-session public pane PNG is generated in memory. Private manifest/DB files may identify sessions or tasks and should be kept separate from shareable PNGs; no automatic expiry/deletion or pseudonym map is provided. Args SHA-256 hashes in v1 may be guessable for low-entropy inputs. Do not distribute private manifest/report DB.
- **Network/telemetry/inference:** no plugin-initiated network requests, no metrics export, model calls, remote attestation or self-updater. Images use local bundled/system fonts. The Desktop pane calls only the authenticated local plugin route.
- **Shell/USB:** no shell commands from a manifest and none by default. Explicit fleet scans stream read-only transactions and may report partial/unavailable coverage; an explicit task probe may read actual file bytes or local bare Git refs. User-invoked `--print-usb` accesses the specified printer through the optional third-party `python-escpos` library and requires USB permission. Submission ACK does **not** confirm paper; without observed hardware the deliverable is PNG only.
- **Credentials:** none stored or read from another client's login. Installing from GitHub is Hermes's normal plugin manager operation, not the plugin's own network call.
- **Approvals:** never approves tools. Optional `pre_tool_call` blocking is off by default and only reviews the prior persisted answer for unbacked claims in the same session; it is not a current-turn proof. The legacy stamp is also off by default.
- **Desktop:** SDK current-session pane and authenticated profile-scoped backend route; no per-turn DOM button or fleet web API. A missing/mismatched session is rejected; the pane cannot claim supported completion without an explicit task manifest.

## Local verification

```bash
PYTHONPATH=/path/to/hermes-agent python3 -m pytest -q --import-mode=importlib tests/test_receipts.py
HERMES_HOME=/path/to/disposable-home hermes plugins validate . --install-deps
HERMES_HOME=/path/to/disposable-home hermes plugins doctor . --ci
```

### Detection benchmark (not verdict accuracy)

`python3 eval/score.py --profiles /path/to/profiles` recomputes the test from `eval/labels.json`, `eval/claim_labels.json`, and local `hive`/`kilo` state.db files. We sampled 200 historical assistant finals from the two profiles: id divisible by 13, recent 3,000 qualifying finals per profile, then seeded stratification into 100 keyword-bearing and 100 other answers. **Kilo, the implementing agent**, labelled the answer excerpts (first 230–350 characters, checking later keyword contexts) for whether each asserted an affirmative past local action in the seven supported classes; critic independently spot-checked 30, agreeing on about 28/30. Kilo then reviewed each of the 66 extracted (kind, sentence) pairs and marked 5 incorrect; **critic did not independently review these claim-level labels**. The public JSON contains only message IDs, profile names, labels and hashed snippets, not private answer text. Results on this deliberately enriched, implementer-labelled sample: **TP=47 FP=7 FN=47 TN=99; answer-level precision=87.04%, recall=50.00%; claim-level extraction precision=61/66=92.42%**. This is not a claim that individual tool-evidence verdicts are correct; those were not independently labelled. Many paraphrases are missed. The corpus is development data, not an independent holdout. Keep the gate off unless you can tolerate false negatives and false positives.

**Catalog:** draft `plugin-catalog/receipts.yaml` in this repo. Upstream PR and demo post are reserved for David; hackathon cutoff **Nov 11, 1PM PT** per the Typeform confirmation (earlier than the tweet wording).
