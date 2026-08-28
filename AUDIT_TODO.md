# BugBunny audit — fix tracker

Source: full-codebase audit (2026-08-26), 74 verified findings + 4 supplemental
items, followed by an 18-finding architectural pass and integration review,
followed by an independent second audit (2026-08-27, 41 findings + 3 hygiene items).
Score = fix confidence (10 = mechanical & non-controversial, <5 = needs design debate).
Status legend: `[ ]` pending · `[~]` in progress · `[x]` fixed (with regression test) · deferred items listed separately.

Working rules: every fix gets a targeted test; `ruff check .` and full `pytest` must stay green; no behavior change outside the finding's scope.

## Third-pass independent audit (2026-08-27)

Seven parallel adversarial reviewers over the full codebase at `f76b173`;
every finding verified by code trace or executable repro before inclusion.
Per the current instruction, every item scoring at least 3 is in scope.
Items with low scores receive deliberately conservative/additive fixes.

### Tier 1 — fix before the 0.8.0 benchmark rerun

- [x] **[7] A1** `src/bugbunny/analysis.py:263`, `src/bugbunny/judge.py:945` (high/statistics) — All published metrics micro-pooled; paper describes per-PR macro-averaging; convention undocumented (2.1x divergence on archived data).
  - fix: record the aggregation convention explicitly in report schemas, add macro-averaged statistics alongside the upstream-faithful micro values, document the difference.
- [x] **[6] A2** `src/bugbunny/judge.py:768`, `src/bugbunny/analysis.py:248` (high/statistics) — Stored per-case precision unbounded (22 archived rows > 1.0); persisted unvalidated.
  - fix: keep upstream-faithful persisted fields but validate them at analysis binding, and report fraction-of-candidates-matched alongside pooled precision.
- [x] **[9] A3** `src/bugbunny/schemas.py:278`, `src/bugbunny/gateway.py:501,579` (high) — Huge integer literals raise uncaught OverflowError from float(), escaping quarantine/retry taxonomies and failing whole batches.
  - fix: overflow-safe numeric coercion at every float() boundary in wire validation.
- [x] **[9] A4** `src/bugbunny/gateway.py:365,988` (high) — `1e999` bypasses strict non-finite rejection via parse_float; success-path canonical hash then crashes outside the error taxonomy, losing CallRecord provenance.
  - fix: reject non-finite parse_float results in strict_json_loads; guard the success-path hash.
- [x] **[6] A5** `src/bugbunny/benchmark.py:1307` (high) — Re-export silently reverts a foreign tool's review row to the pinned base version when the tool exists in both base and bundle.
  - fix: prefer the committed bundle row for foreign tools (documented "preserved" semantics); surface a conflict diagnostic when content differs.
- [x] **[7] A6** `src/bugbunny/families.py:92` (high) — consolidate_semantic_duplicates drops verifier-kept findings on family-key equality alone.
  - fix: family label now only lowers the textual bar (category match + causal Jaccard >= 0.45); near-certain paraphrases (>= 0.82) still collapse.

### Tier 2 — reliability, integrity, security

- [x] **[6] A7** `src/bugbunny/gateway.py:800`, `src/bugbunny/engine.py:1217,1599` (medium) — No total deadline on generation/verifier calls; trickle-body responses hold semaphore slots forever.
  - fix: pass the existing operation-timeout primitive at the generation and verifier call sites.
- [x] **[9] A8** `src/bugbunny/repository.py:507` (medium) — git_grep -z parsing still breaks on newline inside a filename; silent repeated evidence loss.
  - fix: tokenize on NUL (path, line, then text-to-record-LF) instead of pre-splitting the stream on LF.
- [x] **[5] A9** `src/bugbunny/context.py:908-1153`, `src/bugbunny/engine.py:460,498,507` (medium/security) — Curated packets render control-char filenames unescaped above the untrusted guard; splitlines()-based exposure telemetry mis-reconciles them.
  - fix: render control-char paths in escaped form in curated headers/evidence and switch the three engine helpers to LF-only splitting.
- [x] **[9] A10** `src/bugbunny/cli.py:2016` (medium/security) — benchmark judge builds GatewayConfig inline, bypassing runtime-secret registration; gh auth token never registered.
  - fix: register resolved credentials for the judge path and the gh token.
- [x] **[8] A11** `src/bugbunny/cli.py:1671,1839` (medium) — Export hashes artifact bytes then re-reads files twice with no run-dir lock.
  - fix: read once/hash once/parse once; export shares the run-directory lock.
- [x] **[6] A12** `src/bugbunny/benchmark.py:749-881` (medium) — Pre-write preflight narrower than post-write refresh validation; rejected exports still mutate the shared bundle.
  - fix: run the structural sibling manifest/index validation before the first shared write.
- [x] **[7] A13** `src/bugbunny/benchmark.py:975-1156`, `src/bugbunny/analysis.py:536,911` (medium) — verify-export and analyze read the bundle unlocked and re-read/re-hash after checking/analyzing.
  - fix: take the root export lock and hash the exact bytes read.
- [x] **[8] A14** `src/bugbunny/analysis.py:302-350,855` (medium/statistics) — --allow-judge-errors computes per-pair intersections; docs promise one shared clean-case intersection.
  - fix: compute the global clean-case intersection across all compared tools and report exclusions.
- [x] **[4] A15** `src/bugbunny/calibration.py:74-134` (medium/statistics) — Frozen operating point is a tie-break artifact of a saturated corpus; precision floor unverifiable at n=10.
  - fix (conservative): report exact binomial uncertainty and saturation diagnostics; selection unchanged; corpus-size policy stays a design decision.
- [x] **[5] A16** `src/bugbunny/calibration.py:41,252` (medium) — "Excludes CodeReviewBench" attestation never checked against the 50 cases.
  - fix: optional cross-check of corpus observations against a provided benchmark_data.json.
- [x] **[9] A17** `src/bugbunny/exploration.py:459` vs `src/bugbunny/models.py:199` (medium) — repository_index_chars accepted at >=64 but operational floor is 82; accepted configs fail every agentic batch.
  - fix: align the config validation floor with the renderer's marker floor.
- [x] **[9] A18** `src/bugbunny/cli.py:1462` (medium) — Review-phase gather is fail-fast; stragglers write after the run-dir lock releases.
  - fix: settle all review jobs (return_exceptions) before closing/raising, mirroring the resolve gather.
- [x] **[6] A19** `src/bugbunny/engine.py:106`, `src/bugbunny/gateway.py:890` (medium) — wait_for(semaphore.acquire()) can leak a permit on the Python 3.11 floor.
  - fix: shared race-safe bounded-acquire helper that releases a late-granted permit.
- [x] **[8] A20** `src/bugbunny/prompts.py:312` (medium) — Prompt identity hash is categories-blind; custom include_categories records a hash matching no prompt sent.
  - fix: parameterize the hash by the resolved allowed categories (identical output for the default configuration).

### Tier 3 — lower severity

- [x] **[9] A21** `src/bugbunny/util.py:80` (low) — atomic writes never fsync the parent directory; commit points not crash-durable.
- [x] **[9] A22** `src/bugbunny/judge.py:1221` (low) — Judge failure-path gather detaches in-flight checkpoint writes past the lease.
- [x] **[9] A23** `src/bugbunny/judge.py:59,805` (low) — Phantom-row fallback regex misses custom (non-bugbunny-prefixed) tool IDs.
- [x] **[7] A24** `src/bugbunny/benchmark.py:254` (low) — Pinned-dataset hash never enforced; add an opt-in --expect-benchmark-sha256.
- [x] **[6] A25** `src/bugbunny/benchmark.py:940`, `src/bugbunny/cli.py:1889` (low) — verify-export ignores the cumulative index; index committed under a re-acquired lock.
- [x] **[7] A26** `src/bugbunny/judge.py:634` (low/docs) — Index-keyed duplicate scoring is harsher than upstream text-keyed scoring; comparability cost undocumented.
- [x] **[5] A27** `src/bugbunny/judge.py:937` (low) — Judge's printed metrics pool error-degraded rows that analysis refuses; summary carries no degraded flag.
- [x] **[6] A28** `src/bugbunny/analysis.py:491,458` (low/statistics) — Threshold curves omit dedup-sibling crediting; no curve==reduction equivalence test.
- [x] **[7] A29** `src/bugbunny/gateway.py:1139` (low/security) — Response bodies buffered with no size cap.
- [x] **[8] A30** `src/bugbunny/gateway.py:1092,1184` (low/security) — retry_errors redacted with weaker secret sets than the top-level error.
- [x] **[9] A31** `src/bugbunny/gateway.py:384,1070` (low) — Non-UTF-8 200 body raises UnicodeDecodeError outside the retryable taxonomy.
- [x] **[6] A32** `src/bugbunny/gateway.py:495` (low) — pattern keyword uses re.search `$` semantics, laxer than ECMA; diverges from codex-native enforcement.
- [x] **[7] A33** `src/bugbunny/github.py:444` (low/security) — Publication marker matched as substring of any author's review; spoofable already_published.
- [x] **[4] A34** `src/bugbunny/cli.py:2098` (low/security) — argparse's own error channel bypasses redaction.
  - fix (best-effort): redacting parser error path using environment/dotenv-derived secrets available pre-parse.
- [x] **[9] A35** `src/bugbunny/cli.py:1612` vs `src/bugbunny/github.py:280` (low) — SHA length contracts disagree (40-64 vs exactly 40).
- [x] **[6] A36** `src/bugbunny/cli.py:2073` (low) — publish exits 0 on clean_not_published.
- [x] **[5] A37** `src/bugbunny/exploration.py:876` (low) — Blob-read budget enforced per batch; docs promise per review.
  - fix: shared per-review cumulative budget across batches, matching the documented contract.
- [x] **[7] A38** `src/bugbunny/engine.py:158` (low) — Per-chunk context headers unbudgeted; trailing chunks' seed context clipped.
- [x] **[7] A39** `src/bugbunny/exploration.py:1441` (low) — Placeholder observations charged to the context budget; beyond-EOF header renders start > end.
- [x] **[9] A40** `src/bugbunny/diff.py:654` (low) — Empty-file addition leaves old_path set instead of None.
- [x] **[4] A41** `src/bugbunny/repository.py:165`, `src/bugbunny/diff.py:154` (low) — Non-UTF-8 filenames collapse to U+FFFD and become silently ungroundable.
  - fix (conservative): detect lossy path decoding and surface an explicit unsupported-encoding signal instead of FileNotFoundError.
- [ ] **[10] A42** `pyproject.toml` (hygiene) — package-data references nonexistent `schemas/*.json`.
- [ ] **[10] A43** `.github/workflows/ci.yml` (hygiene) — CI omits `ruff format --check`.
- [x] **[8] A44** `tests/test_new_families.py` (hygiene/test-gap) — families.py has 2 tests; expand alongside A6.

## Second-pass architectural audit (2026-08-26)

These scores measure confidence that the proposed remediation is the right fix,
not defect severity. `10` means the change is mechanical and non-controversial;
lower scores indicate broader compatibility, policy, or transactional-design
choices. Per the current instruction, every item scoring at least 5 is in scope.

- [x] **[7] Build-bound run/artifact identity** — The prior `0.7.2` contract resumed artifacts created before eleven behavior-changing commits.
  - direction: add an explicit artifact-contract/build identity to plans, artifacts, resume, export fingerprints, and manifests; reject legacy identities by default.
- [x] **[5] Untrusted generated-file exclusion** — A newly added `@generated` comment can exclude an entire file while coverage remains complete.
  - direction: stop treating author-controlled added markers as sufficient proof of generated provenance; retain explicit path/name policy until a trusted provenance design is chosen.
- [x] **[10] Batch-local grounding** — A finding for another generation batch passes global grounding and reaches verification without its patch/context.
  - direction: require every proposal to resolve to exactly one chunk in its originating batch and reject invalid provenance.
- [x] **[10] Strict judge response contract** — String booleans inflate scores and string confidence values can abort judging.
  - direction: strict schema/type/range validation, semantic retry, and durable fail-closed errors.
- [x] **[7] Analysis input/sidecar binding** — Analysis accepts incomplete populations and tampered candidate-audit indices/hashes.
  - direction: verify manifests and index hashes, bind candidate index/text/hash, judged-input identity, and exact case populations.
- [x] **[6] Cross-judge export bundle transaction** — Shared `benchmark_data.json` conflicts with judge-directory-local identity/phantom checks.
  - direction: make shared identity discovery bundle-wide while retaining judge-local candidate/dedup validation.
- [x] **[8] Cross-process judge checkpointing** — Concurrent judge processes overwrite one another's completed rows.
  - direction: serialize updates with a durable evaluations-file lock and bind lock scope to the full invocation.
- [x] **[8] Complete judge resume identity** — Resume omits golden metadata and judge/prompt/backend identity.
  - direction: hash complete golden objects plus a versioned judge contract and execution configuration.
- [x] **[10] Verified-stage semantics** — Fast or verifier-disabled artifacts can be exported as `balanced-verified`.
  - direction: require successful verifier provenance for balanced/family tracks and make stream identity intrinsic.
- [x] **[9] Cumulative export index** — A later CLI export overwrites the index and hides earlier tracks that remain in the bundle.
  - direction: merge/validate the existing index and commit it under the same bundle lock/identity contract.
- [x] **[6] Cross-process publication idempotency** — GitHub GET-then-POST coordination is process-local and can duplicate reviews.
  - direction: add durable local coordination and document the unavoidable cross-host/server-side limit precisely.
- [x] **[10] Snapshot cancellation cleanup** — Cancellation marks the acquisition Future cancelled while its worker returns an unclosed snapshot.
  - direction: shield acquisition and dispose the eventual result deterministically.
- [x] **[7] Exact Git path preservation** — Trailing-space filenames are trimmed and become ungroundable.
  - direction: preserve semantic path bytes/characters; trim only surrounding diff syntax.
- [x] **[9] Verifier retry input bounds** — Retry notices are appended after fitting and can exceed the declared budget with stale telemetry.
  - direction: rebuild/re-fit and record each retry attempt independently.
- [x] **[10] Git-compatible verifier line numbering** — Verifier excerpts use `splitlines()` instead of Git's LF-only semantics.
  - direction: use the shared `git_lines` helper throughout verifier evidence assembly.
- [x] **[7] Asymmetric `allow_judge_errors` analysis** — Per-tool exclusions leave unequal paired populations and still crash.
  - direction: use and report the paired clean-case intersection for comparisons.
- [x] **[6] Agentic resource bounds** — Blob reads can exceed the cumulative budget and selector queue/retry time lacks a whole-operation bound.
  - direction: pass only remaining blob bytes and separate bounded queue wait from bounded execution/retry time.
- [x] **[8] CLI misleading success/fallback paths** — Custom API-key fallback is preempted and misspelled explicit judge tools succeed as no-ops.
  - direction: honor documented credential precedence and fail requested-but-absent tools.

### Second-pass integration/documentation work

- [x] Update artifact/export/judge/analysis schema documentation and migration notes.
- [x] Update `README.md`, `docs/architecture.md`, `docs/codereviewbench.md`, and `docs/HANDOFF.md` as contracts change.
- [x] Add cross-subsystem and concurrency regression coverage; build/install smoke where the local environment permits.
  - evidence: 358 tests, Ruff clean, isolated `0.8.0` wheel install and `pip check` clean; source/installed identity `1f1cd6b0d82ea279936f39aa51d9443beae27b31a876d286950563ebff4ec8e0`; smoke wheel SHA-256 `a13ac093e27f25ff50a6f154fa9520ab6909849ece52becf7e6cdf13454a4dfb`.
- [ ] Regenerate a post-hardening benchmark from frozen inputs. This is a separate evaluation run, not a code-fix task.

### Supplemental integration findings

- [x] **[10] Current export-index schema identity** — Bump the already-used v2 index contract to v3 when adding implementation binding; reject legacy/current ambiguity.
- [x] **[10] Strict judge JSON duplicates/non-finite values** — Reject duplicate keys and non-finite constants before semantic reduction instead of accepting parser-last values.
- [x] **[9] Pre-write foreign bundle rejection** — Preflight current-schema manifests/indexes under the bundle lock so a rejected foreign build cannot mutate shared Step 3 files.
- [x] **[9] Whitespace/backslash Git paths** — Preserve literal backslashes and leading, trailing, or all-space Git filenames through parsing, grounding, exploration, export, and publication.
- [x] **[9] Stale judge invalidation on timeout** — Persist removal of a changed-input evaluation row before replacement calls, so timeout/crash cannot report stale metrics.
- [x] **[9] Analysis judged-input coordinates** — Recompute current golden/candidate/group identity and validate every pair coordinate/text before attributing signed audit rows.
- [x] **[8] Duplicate review/comment identity** — Reject duplicate case/tool reviews and reduce equal golden/candidate strings by stable index rather than text keys.
- [x] **[8] One selector deadline and conservative failed-read charging** — Bound both local/global queues and the operation, pass only remaining bytes, and charge failed/time-out reads so retries cannot bypass the total.
- [x] **[8] Snapshot loop-shutdown races** — Close late or already-materialized snapshots even when cancellation and event-loop teardown interleave.
- [x] **[8] Final verifier-attempt telemetry** — Make top-level prompt/context/file metrics describe the final actual attempt while retaining every attempt independently.
- [x] **[9] Index-based analysis reduction** — Reconstruct threshold curves and stored TP/FP/FN from ordered pair indexes so duplicate comment text cannot collapse identities or hide counter tampering.
- [x] **[9] Run-artifact/export binding** — Compare every current run artifact's canonical hash with the export manifest before using its stage decisions or telemetry.
- [x] **[9] Homogeneous judge identity** — Reject comparisons whose rows were judged under different model/backend/prompt/timeout/retry identities, and report the one common identity.
- [x] **[10] Contained analysis paths** — Resolve run artifacts, export manifests, and candidate audits beneath their declared roots before reading hash-bound files.
- [x] **[9] Judge population-shrink invalidation** — Durably remove same-tool evaluation rows for cases no longer present after a subset re-export, so ordinary resume remains analyzable.
- [x] **[9] Derived category attribution** — Build TP/FN category counts from the validated pair matrix and current golden metadata instead of trusting mutable summary arrays.
- [x] **[10] Finite judge library bounds** — Reject non-finite/zero timeouts and non-integer/bool concurrency or attempt limits at the exported judge API, not only in CLI parsing.
- [x] **[9] Verifiable judge identity payload** — Persist the versioned non-secret judge identity payload, bind its hash to every row, and make analysis validate current implementation/prompt/backend hash/bounds.
- [x] **[9] Transactional judge input snapshot** — Read and verify all Step 3 inputs under the root export lock so judging cannot consume a concurrent or crash-torn bundle.
- [x] **[9] Legacy bundle preflight rejection** — Reject native older-schema BugBunny manifests/indexes before the first shared-file write instead of preserving rows that the current cumulative index cannot represent.

## To fix (score >= 5) — 70 items

- [x] **[10]** `src/bugbunny/gateway.py:624` (medium) — Retry-After: nan yields asyncio.sleep(nan) which never completes, deadlocking a semaphore slot
  - fix: Ignore non-finite Retry-After values.
- [x] **[10]** `src/bugbunny/benchmark.py:361` (low) — KeyError from export validation escapes main()'s ad-hoc exception taxonomy as a traceback
  - fix: Raise ValueError instead of KeyError for the missing-golden-url case.
- [x] **[10]** `src/bugbunny/engine.py:1610` (low) — Declared-window constants duplicated as literals in artifact provenance
  - fix: Use the shared DECLARED_WINDOW_* constants in the budget provenance block.
- [x] **[10]** `src/bugbunny/validation.py:299` (low) — LEFT-side rejection reasons hardcode 'head file' and 'added anchor line'
  - fix: Use the existing side-aware labels in the two rejection messages.
- [x] **[9]** `src/bugbunny/calibration.py:99` (high) — Empty-prediction precision defined as 1.0 makes the precision-floor guard unreachable and silently freezes a recall-0 threshold
  - fix: Zero-prediction thresholds cannot satisfy a precision floor: exclude tp+fp==0 rows from eligibility so the intended CalibrationError fires. Small, clearly right.
- [x] **[9]** `src/bugbunny/calibration.py:86` (medium) — Calibration threshold sweep rounds candidate thresholds but compares against raw confidences, making achievable operating points unreachable
  - fix: Stop rounding candidate thresholds; use the exact observed confidences.
- [x] **[9]** `src/bugbunny/engine.py:626` (medium) — _anchor_patch_excerpt uses a substring needle match that can anchor verifier patch evidence to a header row instead of the cited changed line
  - fix: Match the exact coordinate token (R{n}, R{n}/L{m}, L{n}) in the annotated gutter instead of substring containment.
- [x] **[9]** `src/bugbunny/exploration.py:397` (medium) — _safe_path admits newline/control characters, allowing forged observation headers from hostile filenames
  - fix: Reject all control characters (<0x20) in selector paths.
- [x] **[9]** `src/bugbunny/exploration.py:1378` (medium) — read action numbers lines with str.splitlines(), diverging from git/diff line numbers
  - fix: Use the same git-lines helper for read-action numbering.
- [x] **[9]** `src/bugbunny/gateway.py:467` (medium) — Schema validator silently ignores the 'pattern' keyword that VERIFIER_SCHEMA declares
  - fix: Implement the 'pattern' keyword in the string branch of the local schema validator.
- [x] **[9]** `src/bugbunny/repository.py:503` (medium) — git_grep record split uses str.splitlines(), fragmenting matches that contain non-\n line boundaries
  - fix: Split git grep -z output on \n only.
- [x] **[9]** `src/bugbunny/schemas.py:346` (medium) — side enum is the only schema enum not checked by the strict parser; non-canonical sides are mis-routed then silently lost
  - fix: Normalize side with .upper() and severity with .lower() in the wire parser, then validate membership; mirrors the existing category aliasing.
- [x] **[9]** `src/bugbunny/schemas.py:352` (medium) — Parser ignores the schema's maxLength bounds; one grounded oversized-evidence finding aborts the whole review
  - fix: Enforce the schema's maxLength bounds in findings_from_payload so oversize findings are quarantined per-item instead of aborting the review.
- [x] **[9]** `src/bugbunny/validation.py:297` (medium) — Anchor lookup uses str.splitlines(), mis-numbering lines vs git's \n-based ledger
  - fix: Introduce a git-semantics line splitter (\n only, trailing-newline aware) and use it for anchor lookup and the normalized line map.
- [x] **[9]** `src/bugbunny/benchmark.py:298` (low) — Loader does not enforce case_id uniqueness, so URL variants collide and by_id() silently drops cases
  - fix: Reject duplicate case_ids at load.
- [x] **[9]** `src/bugbunny/cli.py:868` (low) — doctor --check-env replaces the default reported variables instead of adding to them
  - fix: Union --check-env names with the defaults.
- [x] **[9]** `src/bugbunny/cli.py:1329` (low) — Synchronous artifact reads and hashing on the event loop inside concurrent review jobs
  - fix: Move artifact reads/hashing in review_job into asyncio.to_thread.
- [x] **[9]** `src/bugbunny/context.py:468` (low) — Grep-limit telemetry mislabels dynamic per-call hit_limit saturation as max_hits_per_symbol
  - fix: Split the saturation counter from the max_hits_per_symbol label.
- [x] **[9]** `src/bugbunny/context.py:641` (low) — A failed git grep is cached as an authoritative empty result for all later chunks
  - fix: Do not cache grep failures as authoritative empty results.
- [x] **[9]** `src/bugbunny/exploration.py:121` (low) — Dedup key includes hypothesis_id, so identical retrievals re-execute and duplicate evidence
  - fix: Drop hypothesis_id from the action dedup key.
- [x] **[9]** `src/bugbunny/exploration.py:1051` (low) — request_limit_hit is overwritten each round, losing earlier-round cap hits
  - fix: Accumulate request_limit_hit with |= instead of overwriting per round.
- [x] **[9]** `src/bugbunny/schemas.py:331` (low) — Wire validation is tolerant for category but case-sensitive for severity and skips side entirely
  - fix: Same class as the side fix: case-normalize severity before the membership check.
- [x] **[8]** `src/bugbunny/benchmark.py:495` (medium) — Candidate text suppresses the evidence quote whenever it appears in the never-rendered body
  - fix: Render Evidence unconditionally (suppress only if already present in the rendered text). Changes candidate text for future exports - acceptable as a scoring-input bug fix.
- [x] **[8]** `src/bugbunny/cli.py:763` (medium) — Secret redaction in main() bypassed by argparse prefix abbreviations and dotenv-sourced keys
  - fix: Build the redaction secret set from parsed arguments and resolved credentials rather than scanning raw argv (defeats prefix abbreviation; includes dotenv-sourced keys).
- [x] **[8]** `src/bugbunny/cli.py:1722` (medium) — Multi-model comparability gate silently accepts differing diff hashes
  - fix: Remove the same_legacy_diff escape: require identical diff.sha256 across models (all archived artifacts already satisfy it; the semantic-fields fallback covers a pre-hash schema no current artifact uses).
- [x] **[8]** `src/bugbunny/context.py:548` (medium) — Deletion-only chunks focus head-revision excerpts using base-file line numbers
  - fix: For head-revision chunks with no added lines, use hunk new_start positions (the existing, currently unreachable fallback) instead of old_line numbers.
- [x] **[8]** `src/bugbunny/exploration.py:1186` (medium) — Unreserved evidence separator overflows the context budget and clips the deterministic seed
  - fix: Reserve the joining separator before clipping evidence so the seed is never displaced.
- [x] **[8]** `src/bugbunny/gateway.py:198` (medium) — Dotenv parser returns quoted values with quotes intact when an inline comment follows
  - fix: Parse the quoted span first, then discard the trailing comment; today the comment branch re-quotes the value.
- [x] **[8]** `src/bugbunny/gateway.py:1181` (medium) — Codex subprocess is orphaned when the awaiting task is cancelled
  - fix: Kill and reap the codex subprocess on cancellation (try/except CancelledError around communicate).
- [x] **[8]** `src/bugbunny/cli.py:1150` (low) — Fail-fast gather abandons in-flight resolve_case tasks that keep using a GitHubClient being closed
  - fix: gather with return_exceptions=True so all resolution tasks settle before the client closes; then raise the first failure.
- [x] **[8]** `src/bugbunny/cli.py:1312` (low) — No inter-process exclusion on the benchmark run directory: concurrent resumes silently lose checkpoint records
  - fix: Hold a file lock on the run directory for the duration of benchmark run.
- [x] **[8]** `src/bugbunny/cli.py:1961` (low) — main() catches only RuntimeError/FileNotFoundError/ValueError, so plausible failures bypass redaction and the exit-code contract
  - fix: Broaden the top-level handler to redact and exit 2 on any Exception (KeyboardInterrupt kept separate).
- [x] **[8]** `src/bugbunny/context.py:50` (low) — _TEST_NAME regex misclassifies production files like latest.ts and backtest.py as tests
  - fix: Anchor the test-name regex so 'latest.ts'/'backtest.py' stop matching.
- [x] **[8]** `src/bugbunny/context.py:739` (low) — _path_test_hints rescans and re-filters the entire head tree once per chunk
  - fix: Precompute the test-file candidate list once per build.
- [x] **[8]** `src/bugbunny/diff.py:143` (low) — _decode_git_quoted crashes with a bare ValueError on octal escapes above \377
  - fix: Treat octal escapes above \377 as literal text instead of crashing.
- [x] **[8]** `src/bugbunny/diff.py:881` (low) — chunk_diff(include_excluded=True) silently drops typed exclusion records for excluded files that have hunks
  - fix: Record typed exclusions even when include_excluded=True.
- [x] **[8]** `src/bugbunny/engine.py:326` (low) — _fit_generation_prompt fails a fittable batch when context headroom is smaller than the truncation marker
  - fix: Fall back to empty context (with omission telemetry) when only the truncation marker fails to fit.
- [x] **[8]** `src/bugbunny/exploration.py:546` (low) — requests_rejected double-counts omitted-by-cap and accepted-then-failed requests
  - fix: Separate rejected/omitted/failed request counters so they sum coherently.
- [x] **[8]** `src/bugbunny/gateway.py:243` (low) — resolved_api_key skips the documented MARTIAN_API_KEY fallback when api_key_env is set but unset in the environment
  - fix: Fall through to MARTIAN_API_KEY/dotenv when the configured api_key_env is unset, per the documented precedence.
- [x] **[8]** `tests/test_new_judge.py:143` (low) — Judge error/resume contract ('resumes only error-free records') is never tested
  - fix: Test the error/resume contract (errors_count>0 re-runs; clean records resume) alongside the resume-binding fix.
- [x] **[7]** `src/bugbunny/analysis.py:334` (high) — Skipped and error-degraded judge rows are pooled into published metrics, CIs, and curves
  - fix: Filter skipped rows everywhere judge.py does; hard-fail on errors_count>0 rows with an explicit override flag and surface counts in the report.
- [x] **[7]** `src/bugbunny/benchmark.py:974` (high) — Cumulative multi-model export never checks cross-model case-set or fixture/base/head/diff identity (invariant 12)
  - fix: Record per-case identity (fixture URL, base/head SHA, diff hash) in each export manifest and cross-check every other BugBunny manifest in the bundle at export time. Contained; needs care for legacy manifests.
- [x] **[7]** `src/bugbunny/calibration.py:236` (high) — load_operating_point never binds the frozen threshold to the observations/selection, so threshold tampering passes every integrity check
  - fix: Recompute select_operating_point from the hash-bound observations at load and require the stored threshold/selection to match; also verify operating_point_id. Must confirm the archived op file still loads.
- [x] **[7]** `src/bugbunny/judge.py:540` (high) — Judge resume is not bound to candidate content; re-exports are silently skipped
  - fix: Add a candidates/dedup content hash to each evaluation record; is_done requires it to match the current export. Clear fix; only debate is how legacy hashless records resume (choose: treat as stale).
- [x] **[7]** `src/bugbunny/analysis.py:261` (medium) — Threshold-curve reduction uses different FP semantics than the actual Step 3 scoring, systematically overstating precision
  - fix: Reimplement _threshold_case with the judge's greedy best-confidence claiming over the same pair matrix; verify equivalence against evaluate_review on synthetic data.
- [x] **[7]** `src/bugbunny/benchmark.py:996` (medium) — No inter-process locking around the read-modify-write of shared judge inputs; concurrent exports silently lose a model's rows
  - fix: Take a file lock (same flock helper the repo cache uses) around the export read-modify-write.
- [x] **[7]** `src/bugbunny/diff.py:842` (medium) — Quadratic re-rendering in _segments_for_hunk makes chunking take seconds-to-minutes of blocking CPU
  - fix: Track running annotated length incrementally instead of re-rendering the candidate segment per line.
- [x] **[7]** `src/bugbunny/engine.py:1275` (medium) — Finding validation and verifier evidence perform per-finding blocking git reads on the event loop
  - fix: Run validate_findings, _verification_evidence, and assert_clean via asyncio.to_thread; they are self-contained synchronous units.
- [x] **[7]** `src/bugbunny/exploration.py:1084` (medium) — Transiently failed actions are invisible to the selector and permanently blocked by dedup
  - fix: Record dedup keys only for successful or permanently-failed actions so transient failures (timeout, read_failed, search_failed) can be retried.
- [x] **[7]** `tests/test_new_analysis.py:5` (medium) — The entire analysis report pipeline has zero test coverage
  - fix: Add tests for _metrics/_bootstrap/_threshold_case/analyze_evaluation with synthetic fixtures (write alongside the analysis fixes).
- [x] **[7]** `src/bugbunny/context.py:890` (low) — Header clipping can truncate the prompt-injection guard while untrusted evidence still renders
  - fix: Guarantee the untrusted-data guard line survives header clipping (reserve its bytes first).
- [x] **[7]** `src/bugbunny/judge.py:605` (low) — Reported metrics and CLI exit code aggregate the entire historical evaluations.json, not the current run
  - fix: Aggregate metrics and derive the exit code from the tool set actually processed this invocation.
- [x] **[7]** `src/bugbunny/models.py:348` (low) — Coverage.complete is count-based identity; eligible hunk IDs are never recorded
  - fix: Record eligible hunk IDs in Coverage and compare ID sets; additive artifact change.
- [x] **[7]** `src/bugbunny/validation.py:166` (low) — Semantic fingerprint omits the diff side, collapsing distinct LEFT/RIGHT findings
  - fix: Add side to the semantic fingerprint and bump the finding-id version to bugbunny-v2.
- [x] **[7]** `tests/test_new_cli.py:574` (low) — No test covers the benchmark-run failure path: redacted error records, failed status counts, exit code 1, or --no-resume
  - fix: Add a benchmark-run failure-path test (failed status counts, redacted error record, exit code 1).
- [x] **[7]** `tests/test_new_engine.py:233` (low) — FakeSnapshot ignores the revision argument and serves one source map for base and head, so LEFT/merge-base wiring is untestable
  - fix: Make FakeSnapshot revision-aware and add a LEFT-side acceptance test through the engine.
- [x] **[6]** `src/bugbunny/exploration.py:981` (high) — Selector wait_for timeout also covers semaphore queue wait and gateway-internal retries, causing spurious coverage failures
  - fix: Remove the outer wait_for (transport already bounds each attempt) or start timing after semaphore acquisition. Direction is clear once the Retry-After hang is fixed; changes failure semantics, so needs a careful test.
- [x] **[6]** `src/bugbunny/benchmark.py:721` (medium) — Exporting under a second judge model silently invalidates every committed manifest in other judge directories
  - fix: Refresh prior manifests across all judge directories that bind the shared benchmark_data.json, not just the current one.
- [x] **[6]** `src/bugbunny/benchmark.py:984` (medium) — Re-export silently drops foreign tools' review records while preserving their candidates, corrupting the shared Step 3 bundle
  - fix: Carry over every existing review row whose tool is absent from the target entry, not only bugbunny-prefixed ones; document the semantics.
- [x] **[6]** `src/bugbunny/engine.py:928` (medium) — Cancellation during to_thread(acquire) leaks a fully materialized worktree
  - fix: Shield the acquire thread and close the materialized snapshot if the awaiting task was cancelled (done-callback pattern).
- [x] **[6]** `src/bugbunny/benchmark.py:1109` (low) — Interrupted export leaves unmanifested phantom tool rows in a bundle whose remaining manifests all verify clean
  - fix: Detect phantom tools at verify time: any BugBunny tool present in candidates/dedup/reviews without a manifest fails bundle verification.
- [x] **[6]** `src/bugbunny/cli.py:1260` (low) — --git-concurrency resource domain enforced only during prewarm; review phase re-fetches without it
  - fix: Wrap the cache handed to engines so review-phase acquires also respect --git-concurrency (threading semaphore inside the sync boundary).
- [x] **[6]** `src/bugbunny/exploration.py:1017` (low) — 4-chars-per-token output heuristic hard-fails valid in-budget selector responses
  - fix: Drop the serialized-length output heuristic (schema maxItems + transport cap already bound the response).
- [x] **[6]** `src/bugbunny/gateway.py:1124` (low) — Failed codex calls lose token usage, resolved model, and response hash telemetry that the Martian path preserves
  - fix: Populate _BackendFailure.backend from partial codex results so failed calls keep usage/model telemetry.
- [x] **[6]** `src/bugbunny/judge.py:596` (low) — Full-state checkpoint rewrite per review is O(N^2) with the added pair_matches payload
  - fix: Coalesce checkpoint saves (single writer, skip when a save is in flight) while keeping the upstream file format.
- [x] **[5]** `src/bugbunny/analysis.py:290` (medium) — Statistical join across evaluations, candidate audit, and run artifacts has no identity binding
  - fix: Bind the join: verify candidate-audit tool/finding identities against the run artifacts before attributing decisions. Needs a schema decision on what to bind.
- [x] **[5]** `src/bugbunny/diff.py:107` (low) — _decode_git_quoted strips surrounding whitespace, corrupting paths with leading/trailing blanks
  - fix: Stop stripping whitespace inside _decode_git_quoted; audit each call site for where trimming is actually required.
- [x] **[5]** `src/bugbunny/prompts.py:317` (low) — generation_prompt_sha256 is policy-blind, so the recorded prompt identity does not match the bytes sent for non-production review policies
  - fix: Parameterize the prompt hash by policy and record the policy-aware value; needs a resume-compat fallback for artifacts recorded under the legacy default-policy hash.
- [x] **[7]** `src/bugbunny/exploration.py:1366` (low) — context_blob_read_bytes documented as total but enforced per read action
  - fix: Enforce a cumulative per-review blob-read budget matching the documented 'total'.
- [x] **[9]** `docs/HANDOFF.md` (low) — Clarify golden-count discrepancy vs the paper (173 correct for pinned commit)
  - fix: One clarifying line in the docs.

## Deferred (score < 5) — needs discussion — 8 items

- [ ] **[4]** `src/bugbunny/cli.py:1037` (medium) — Run/artifact identity is hand-maintained in five separate field-list sites
  - fix: Consolidating five hand-maintained identity field lists is a refactor with subtle resume-compat implications.
- [ ] **[4]** `src/bugbunny/diff.py:65` (low) — Over-broad vendor/generated path-component exclusions silently remove first-party code from review
  - fix: Exclusion breadth is a benchmark-comparability policy decision, not a patch.
- [ ] **[4]** `src/bugbunny/gateway.py:973` (low) — max_retries compounds multiplicatively across the structured-output loop, the transport loop, and the fallback post
  - fix: Collapsing the nested retry loops into one total-attempt budget changes retry behavior; needs its own decision.
- [ ] **[4]** `src/bugbunny/repository.py:317` (low) — Immutable-diff tamper re-check is silently skipped whenever diff_context_lines != 12
  - fix: Making the tamper re-check cover non-default context widths needs an acquisition-time contract change; document for now.
- [ ] **[3]** `src/bugbunny/validation.py:388` (medium) — Verifier mutates the validated stream in place; analysis silently depends on it
  - fix: Copy-before-verify breaks analysis._decision_by_finding, which reads verifier confidences off the validated stream; needs a coordinated schema decision.
- [ ] **[2]** `src/bugbunny/engine.py:879` (low) — ReviewEngine.review is a ~930-line god-method mixing orchestration, fitting, verification, and telemetry
  - fix: Real refactor of a 930-line method; high regression risk, needs its own design pass.
- [ ] **[4]** `results/codereviewbench-2026-08-24-claude-family/export` (medium) — Archived bundle missing the manifest-bound benchmark_data.json
  - fix: Mutating an archived bundle needs explicit sign-off; queued as a proposal.
- [ ] **[4]** `src/bugbunny/repository.py:109` (low) — Global git config not isolated (GIT_CONFIG_GLOBAL)
  - fix: Could break legitimate proxy/credential setups; needs a decision.
