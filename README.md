# Receipts for Hermes 🧾

Turn-by-turn thermal-paper receipts for what Hermes *observed*, not what the agent merely said. Ledger hooks see each tool call; historical receipts read existing `state.db` immediately on install. No LLM calls.

![Real session receipt](assets/real-receipt.png)

## Install / use

```bash
hermes plugins install thedavidmurray/hermes-receipts
hermes plugins enable receipts
hermes receipt last --png receipt.png
hermes receipt verify <full 64-character ledger sha256>
```

`/receipt` in CLI/Discord/Telegram renders the latest persisted turn as text plus `MEDIA:` PNG; `/receipt <final-message-id>` selects a prior final answer. `hermes receipt last --session <id>` narrows a profile's session. Desktop has a **Receipts** pane and “Get receipt” button for the active session. The public Desktop SDK has no per-turn button contribution, so this does **not** patch transcript markup. Enable the Python plugin in Hermes and the Desktop component in Capabilities → Plugins; the latter alone cannot load the backend.

Optional: `hermes receipt last --print-usb 0xVVVV 0xPPPP` uses `python-escpos>=3,<4` installed separately. It fails with a readable error if the package, printer or USB permissions are absent. This is never automatic.

The optional response stamp is **off**: `HERMES_RECEIPTS_STAMP=1` appends `🧾 n✓ m✗` to answers. `transform_llm_output` uses first-wins arbitration; other response transforms can conflict. The optional completion gate defaults to **observe**; `HERMES_RECEIPTS_GATE=block` blocks `kanban_complete` when the last persisted answer has unbacked claims. It never grants approval. Enable flags only on a disposable profile after testing; a gate should not decide which actions to take.

## How to read this

✓ means a matching tool call in the **same user turn** has an explicit success result. ✗ means a matching tool failed or was not recorded; it is **not proof the action never happened**. “not observed” denotes explicit subagent, CI or out-of-process work. Unknown tool results cannot be marked successful. Claims are conservative regex detections, not general natural-language entailment. In particular, a `git push` command exiting zero is evidence of command completion, *not* read-back proof that the remote branch changed; tests exiting zero are evidence of that process, not that all possible tests pass. A SHA-256 hash checks the canonical ledger rows reconstructed from `state.db`, not a signature, tamper-resistant audit log, or external attestation. No tool result bodies appear in the receipt.

The tool hook writes `tool name`, `args SHA-256`, a coarse action category, status, duration and session/turn/call ids to profile-scoped `plugin_db('receipts')`. Raw args and results are not stored there. The history reader opens **only this profile's** `state.db` in SQLite `mode=ro`, and reconstructs turns lazily from the preceding user message, assistant `tool_calls`, matching tool responses and final answer. Existing turns need no import/migration. For historical turns Hermes records neither per-turn tool duration nor per-turn usage: these are shown as unavailable, with separate **session** token/cost totals if present. Wall time is the historical user→final interval. Ledger coverage is limited by persisted tool-call history; tool calls inside `execute_code`, subagents, shell children and separate CLI jobs are not attributed to a parent unless Hermes recorded them in that parent turn.

## Disclosure (catalog rule 13)

- **Outside reads:** current profile's `state.db` (read-only, message text and tool argument JSON interpreted in memory); no other profile or browser login. This means the plugin can see sensitive conversations and tool arguments in its own profile.
- **Stored data:** the plugin's own SQLite ledger and generated PNGs under profile `plugin-data/receipts/`. Args are hashed but hashes of low-entropy args may be guessable; exported receipts disclose tool names, small basename labels, quoted answer claims and session ids. Review before sharing.
- **Network/telemetry:** none. No metrics export, no model calls, no self-updater. Images use local bundled/system fonts.
- **Shell commands:** none run by default. A user-invoked `--print-usb` accesses the specified USB printer through an optional third-party library; no background processes.
- **Credentials:** none stored or read from another client's login. Installing from GitHub is Hermes's normal plugin manager operation, not the plugin's own network call.
- **Approvals:** never approves tools or launches an unattended agent. The opt-in gate can block completion but cannot bypass permissions.
- **Desktop:** SDK pane and authenticated plugin backend route; not a per-turn injected DOM button.

## Local verification

```bash
PYTHONPATH=/path/to/hermes-agent python3 -m pytest -q --import-mode=importlib tests/test_receipts.py
HERMES_HOME=/path/to/disposable-home hermes plugins validate . --install-deps
HERMES_HOME=/path/to/disposable-home hermes plugins doctor . --ci
```

### Detection benchmark (not verdict accuracy)

`python3 eval/score.py --profiles /path/to/profiles` recomputes the test from `eval/labels.json` and local `hive`/`kilo` state.db files. We sampled 200 historical assistant finals from the two profiles: id divisible by 13, recent 3,000 qualifying finals per profile, then seeded stratification into 100 keyword-bearing and 100 other answers. A reviewer read the answer excerpts (first 230–350 characters, checking later keyword contexts) and labelled whether each asserted **an affirmative, past, local action** in the seven supported classes. The public JSON contains only message IDs, profile names and labels, not private answer text. Results on this deliberately enriched sample: **TP=52 FP=10 FN=42 TN=96, precision=83.87%, recall=55.32%** at answer-level claim *detection*. This is not a claim that 83.87% of individual tool-evidence verdicts are correct; those were not independently labelled. Many paraphrases are missed. Keep the gate off unless you can tolerate false negatives and false positives.

**Catalog:** draft `plugin-catalog/receipts.yaml` in this repo. Upstream PR and demo post are reserved for David; hackathon cutoff **Nov 11, 1PM PT** per the Typeform confirmation (earlier than the tweet wording).
