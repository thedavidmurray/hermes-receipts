# Integration / independent release evaluation

This is **experimental, NOT v2 accepted**. Numeric acceptance targets are not results.
The builder has not opened the sealed gold or run/tuned against it. The existing
independent verifier owns the one frozen-holdout run **after** the customization
child finishes. Do not run the detector against gold and then adjust it. The
current `deterministic-2.0` / `outcome-2.0` pipeline requires no model; proposed
cross-model inference evaluation is not represented as performed.

## Exact-source local gates

With Hermes dependencies already installed (no package installation or live
profile mutation), from a **clean committed checkout**:

```sh
PYTHONPATH=/path/to/hermes-agent /path/to/hermes-python scripts/release_gates.py
PYTHONPATH=/path/to/hermes-agent /path/to/hermes-python scripts/outcome_probe.py --output /local/public-demo
HERMES_REPO=/path/to/hermes-agent PYTHONPATH=/path/to/hermes-agent \
  RECEIPT_PYTHON=/path/to/hermes-python RECEIPT_PNG=/local/public-demo/pane-unverified.png \
  RECEIPT_SCREENSHOT=/local/public-demo/browser.png node tests/browser_v2.mjs
```

The gate runner uses `git archive HEAD`, separate test/validate/doctor processes,
a sibling disposable HERMES_HOME **outside** the scanned tree and no installs.
Pillow, pytest, tesseract and Hermes/FastAPI are needed. Browser harness additionally
uses esbuild, React and Playwright from the supplied Hermes checkout and a Chrome
binary (`RECEIPT_CHROME` overrides the macOS default). The actual plugin module
and router run in a disposable authenticated harness; the SDK admission/state
owner is simulated, not a full Desktop-host or production-activation test. It
checks 401 unauthenticated, 422 missing session, 404 cross-profile, actual button
click/render and an s1 response arriving **after** s2; stale pixels must not win.

The outcome probe creates actual local files, executes a digest-bound test,
commits/pushes into a disposable local bare remote and independently reads refs.
It asserts exact expected criterion/completion verdicts for 13 scenarios,
including missing artifact/push, wrong target/profile, stale/future test, repair,
linked/unlinked multiturn and empty criteria/evidence. It checks canonical rerun
and store idempotence. These are controlled fixtures, **not heldout performance**.
The full suite separately audits quiescent source DB/WAL/SHM hashes and canaries.
An absent call stays unverified; a genuine failed outcome readback may contradict
a criterion. Task completion never becomes supported until all required criteria
are supported for the linked target/version/window.

Two extra end-to-end checks were run by hand against the committed code (no
network, no live profile, all temp roots under the caller's scratch):

1. **CLI task path.** A manifest with `artifact_digest` + `remote_commit`
   criteria was driven through the real `v2.cli.handle` entry point that the
   plugin registers. Before the real `git push` into the disposable local bare
   remote the PNG read `COMPLETION: UNVERIFIED` / `REQ 2 ... CONTRADICTED`;
   after the push the same manifest read `COMPLETION: SUPPORTED` with every
   criterion supported. Note the declared `artifact_version` is the shared
   version for *every* criterion — a criterion binding a different version than
   the claim correctly fails closed.
2. **Fleet -> report path.** An opt-in `receipt fleet --roots <disposable home>`
   scan over a synthetic two-profile `profiles/*/state.db` home imported 2
   finals from 2 sources with the source DB bytes bit-identical before and after
   (no source writes), and the follow-up `receipt report --group-by
   profile,model,source,project_key` rendered a grouped statement with two
   labelled groups, coverage 2/2 complete, `DONE ASSERTIONS` reported separately
   from `ACTION CLAIMS`/`TASK COMPLETION` and the cost line only in the footer.

`assets/integration/` contains output from an actual controlled run: before/after
PNG, grouped statement, no-manifest PNG, browser screenshot and the <=90s replay.
The replay uses real generated PNGs with synthetic fixture metadata, labels every
frame, and explicitly says PNG fallback / hardware unverified. It is not a live
recording, customer telemetry or evidence of physical paper. The video helper
reuses the coordinator's shared `make_cut.py`; see `scripts/outcome_video.py`.

## Blind verifier-only exports and scoring

Frozen handle: `receipts-v2-gold-final-v1`.
Manifest SHA256: `0f6debbb22a5b4c21778cef59764d46ecd6179368cac9b8da0898f708849bc57`.

The verifier, not a builder, checks the original seal and exports two **private**
JSON envelopes outside the public repo. An export is an adapter, not a replacement
gold set. Preserve exact frozen IDs, text offsets, claim/evidence/window/links,
labels and populations; attest export equivalence and export byte SHA256. Do not
send the builder case-level labels, errors, snippets, IDs or private paths.

Unlabelled envelope: `{handle,detection,verdict}`.
- detection row: `{case_id,answer,scope,links?}`.
- verdict row: `{case_id,claim,evidence,window,links?,requirements?}`. Native v2
  dictionaries; evidence normalization belongs to the verifier's input adapter.
  `done` goes through `judge_task`; other kinds through `judge_claims`.

Private gold export: `{handle,manifest_sha256,detection,verdict}`.
- detection row: `{case_id,stratum,claims:[{kind,span_start,span_end}]}`; stratum
  random/enriched, 120 each, half-open Unicode offsets in the original answer.
- verdict row: `{case_id,verdict,kind,evidence_type,population}`; 120 cases,
  evidence_type invocation/process_result/outcome_readback/missing/mixed;
  population synthetic/reviewed_real. Aliases must reflect actual frozen inputs,
  not invented categories. Four native verdict labels only.

```sh
python -m eval.release_predict --cases /verifier/unlabelled.json --output /verifier/predictions.json
python -m eval.release_eval --gold /verifier/gold-export.json \
  --gold-sha256 <verifier-attested-export-byte-sha256> \
  --predictions /verifier/predictions.json --expected-repo-sha <exact-final-40-hex-sha> \
  --output /verifier/aggregate.json
```

Prediction requires a clean committed tree, pins the actual Git SHA/input byte
hash and creates output exclusively at 0600. Both CLIs reject private inputs or
outputs inside the public repo. A different filename can bypass exclusive-create;
the verifier must enforce the **one-run protocol**, not treat this as a sandbox.
The scorer rejects missing/extra/duplicate IDs, wrong handle/manifest/SHA,
wrong denominators, unknown classes and unreviewed population aliases. Aggregate
output excludes case IDs, spans and raw strings. Model-adjudicated gold is not
human truth. Repeated synthetic families and the bounded two-profile historical
frame limit generalization; no deploy positives is a coverage gap.

Report random seven-action answer TP/FP/FN/TN and P/R as the primary detection
score; enriched separately; same-kind maximum one-to-one overlapping-span scores;
done separately; all-eight-kind breakdown; verdict four-way matrices, macro-F1,
P/R and denominators by kind/evidence/population; support/decision/abstention
coverage; done false-support withholding and true-support withholding separately
from explicit contradiction. Wilson95 intervals cover binomial P/R and done
rates, not model uncertainty or macro-F1. No macro-F1 interval is claimed.

A kit-reliability suggestion needs n>=10 and both support/contradiction precision
>=.90; undefined precision fails. It is not automatic kit activation. The final
verifier must also run the separate `eval/jev_holdout.py` lane in its authorized
environment, preserve default-off inference and report deterministic/Jev/combined
separately. Combined equals deterministic by policy, not an observed improvement.

## Release boundary

Do not repin `plugin-catalog/receipt-printer.yaml` until exact-head independent review
and clean gates establish the release SHA. Existing catalog pin is retained;
source publication or a green fixture suite is **not** v2 acceptance. David owns
activation/submission; the newer coordinator deadline is Nov 11 **2 PM PT**
(5 PM EST), superseding older 1 PM text in the frozen plan. Hardware remains
unverified until an observer actually sees the paper. The customization child
must preserve extensible public shapes and mandatory evidence/verdict wording.
