# Architecture

BugBunny is a review harness, not a many-agent framework. Its default balanced
path has two model stages; the fast path has one.

```text
exact PR refs
    |
    v
immutable cached checkout --> unified diff --> lossless annotated chunks
    |                                             |
    +-- generous curated repository evidence -----+
    |                                             |
    +-- optional hypothesis-driven selection -----+
                                                  |
                                      parallel generation calls
                                                  |
                                      raw atomic findings
                                                  |
                             deterministic path/line/evidence gates
                                                  |
                              persisted validated candidate stream
                                                  |
                                  optional batched verifier
                                                  |
                             same-site semantic duplicate gate
                                                  |
                         final findings + complete audit artifact
                                                  |
                    generator / balanced / family exports
```

## Invariants

1. **Exact input.** A review records the resolved base and head SHAs, computes a
   verified merge base, and addresses the merge-base/head diff by hash.
2. **No silent coverage loss.** Every eligible hunk is assigned to exactly one
   chunk. Coverage completion compares the exact eligible and completed hunk-ID
   sets; a coincidentally equal count is insufficient. Failed chunks make the
   run partial or failed.
3. **No repository execution.** Default context collection reads Git objects and
   files; it does not run package scripts, compilers, linters, or tests.
4. **Grounded output.** A final finding names a changed text file and side: an
   added RIGHT line grounded in the head snapshot or a deleted LEFT line
   grounded in the merge-base snapshot. The location must also belong to the
   exact generation chunk that proposed it; global diff membership alone is
   insufficient.
5. **Atomic issues.** One finding describes one root cause. Summary prose is not
   re-extracted into candidates.
6. **Observable filtering.** Raw, deterministically validated, rejected, and
   final findings are retained as distinct streams.
7. **Separated effects.** Review and verifier models are separate settings. A
   model sweep can pin or disable verification. Context mode is a separate
   setting from both `fast`/`balanced` review profiles.
8. **Comparable sweeps.** Fixture base/head refs are resolved once before any
   model runs. Multi-model export requires the same cases, fixture URLs,
   base/head commits, and exact diff hashes, even when models are added in
   separate export invocations.
9. **No early finding cap.** Generation and evaluation artifacts retain all
   validated findings. Causal-family grouping is a transparent export-only
   presentation and retains every atomic member ID/location.
10. **Side effects are explicit.** Review creation and publication are separate
   operations; the model never receives a write token.
11. **Resume is content-bound.** Review resume binds the frozen run contract
    and exact implementation identity. Judge resume additionally binds complete
    golden objects, ordered candidates, dedup groups, judge model/API-base
    identity, prompt/schema identity, timeouts, and retry policy. The complete
    non-secret identity payload is persisted for analysis, with a hash of the
    API base rather than the raw URL. Changed or legacy-unbound inputs are not
    silently reused; a stale row is durably removed before its replacement
    call begins.
12. **Shared state is serialized.** A benchmark run directory and the shared
    export bundle each have an inter-process lock around their complete
    read-modify-write transaction. Judge checkpoint and publication coordination
    use durable file locks as well.
13. **Build identity is intrinsic.** Versioned review/run/export schemas carry a
    path-independent SHA-256 identity of every installed BugBunny Python source
    file. Current-schema metadata from another build is rejected before shared
    files are modified.
14. **Candidate stages are truthful.** `balanced` and `family` export requires
    artifacts that actually ran a configured verifier; a fast or
    verifier-disabled artifact can only enter the generator track.

## Benchmark scheduler

A benchmark run first writes an immutable `job_plan.json` containing every
selected PR's complete resolved metadata and exact base/head SHAs. Repository
objects are then prepared in independent per-remote caches, with cold Git work
bounded separately from GitHub metadata resolution. Case/model pairs share one
active-review limit and one gateway-wide model-call limit; the per-review limit
still controls chunk fan-out. The Git limit applies again if review-phase
acquisition must fetch after prewarming. Larger diffs are submitted first so
long jobs do not accumulate at the tail. A nonblocking exclusive lock is held
on the run directory for the full invocation, so an accidental double resume
cannot interleave checkpoint rewrites.

Each completed pair is atomically committed to `run_checkpoint.json`. Resume
requires the frozen plan, artifact checksum, dataset, configuration, runtime,
tool version, prompt identities, and complete coverage to agree. Fully reusable
cases do not repeat repository preparation.

The evaluation runner applies the CodeReviewBench matching prompt to all
golden/candidate pairs through one bounded Martian queue. It first reads and,
when BugBunny metadata is present, verifies all three Step 3 inputs under the
same root lock as export, so a concurrent or interrupted multi-file update
cannot become a mixed judge snapshot. Pair results are
reduced in their original deterministic order, while completed review/tool
records are atomically checkpointed with coalesced full-state writes. A record
is resumable only when it is error-free and its judged-input hash still matches
the current golden/candidate/dedup content. Metrics and exit status are scoped
to the tools selected for the current invocation. Pair-level matches,
confidence, retries, and safe errors are retained without changing reduction.
Post-hoc analysis excludes skipped rows, rejects judge-error-degraded rows by
default, resamples pull requests for paired confidence intervals, and
reconstructs verifier-threshold curves with the judge's greedy reduction. It
makes no new judge calls and cannot change the frozen operating point.

## Profiles

`fast` performs generation plus deterministic validation. It is useful for broad
model sweeps. Generator confidence is retained as telemetry but does not filter
models with incomparable confidence calibration.

`balanced` adds one keep/drop/merge verifier pass over bounded candidate batches.
The verifier model and externally calibrated operating point are frozen in the
artifact. Calibration uses a versioned constructed corpus that explicitly
excludes benchmark cases and binds the model, reasoning setting, prompt, schema,
observations, and objective. Structurally valid responses that violate dynamic
decision relationships receive a small, configured number of semantic retries;
every attempt and safe failure reason is retained. Exhaustion or any other
verifier failure fails closed for final publication while preserving the earlier
streams. Loading an operating point re-derives its selection from the bound
observations, so changing the threshold, observations, selection, or operating
point ID is detected.

The shared model gateway applies its bounded retry policy to transport errors,
retryable HTTP statuses, JSON extraction, and local response-schema validation.
Attempt count, safe errors, and aggregate token/cost telemetry therefore include
malformed successful HTTP responses instead of misclassifying them as one-shot
coverage failures.

## Context acquisition

The default `curated` mode builds deterministic packets from immutable Git
objects. It combines changed-file source with bounded whole-tree searches for
definitions, usages, callers, imports, and path-matched tests, plus explicit
risk hypotheses. Its fixed defaults are intentionally generous: 72,000 patch
characters and 120,000 context characters per generation batch, with up to
36,000 curated characters used as the seed when model-directed selection is
enabled.

In `agentic` mode, failure of the selector model call or its root protocol fails
the affected coverage explicitly. Individual reads, searches, and listings are
optional evidence requests: one failed action is counted and audited while the
review continues with deterministic seed context and any successful actions.

`agentic` is orthogonal to the review profile. Before a generation batch, the
same review model makes two structured context-selection rounds by default and
maintains tentative evidence hypotheses. Hypotheses may be open, resolved, or
rejected, guide actions only, and are never supplied as final findings.
`--context-selection-rounds` can raise this to eight for a deliberate
capability study. Its portable JSON protocol exposes only three declarative
actions:

- `list` returns bounded, cursor-pageable paths from the frozen repository
  inventory;
- `read` returns a bounded line interval from an inventory file at the exact
  head commit;
- `search` performs bounded, literal (non-regex), offset-pageable Git search
  against that commit.

For a large inventory, the first view is a hierarchical root/subtree count
summary rather than an alphabetical path prefix. Exact paths remain discoverable
through `list`, so repository size does not create a hidden first-N ceiling.

The engine validates paths and action semantics, deduplicates requests, enforces
per-round, per-file, cumulative blob-byte, line, character, hit, and
total-context limits, and passes the selected evidence to generation and
verification. Identical retrievals deduplicate across hypotheses; transient
read/search failures remain retryable. Paths containing control characters are
rejected, and source lines use Git's `\n`-only numbering semantics. Repository
content is always delimited as untrusted data, including when prompt headers
must be clipped. There is no general tool interface: the selector cannot
execute code, invoke a shell, access the network, or mutate the snapshot.

The configured file limit applies to distinct files added through agentic
reads and searches; deterministic seed files are accounted separately. Search
and inventory pagination caps are logged independently from actual character
truncation, including whether any reachable page remained unresolved when
selection stopped.

Every artifact freezes `context_mode`, all resolved limits, and whether those
limits came from fixed values or a declared model window. A global
`--context-window-tokens N` declaration applies to one model or every model in
a sweep; repeatable `--model-context-window MODEL=N` entries override it by
exact model ID. Derived bounds reserve output and protocol room and use a
deterministic source-code character planning estimate. The engine then measures
and enforces the complete rendered prompt in characters, including escaped PR
metadata and verifier candidate JSON. A separately pinned verifier requires its
own `--verifier-context-window-tokens N` declaration when generation uses a
declared window. BugBunny deliberately does not infer context capacity from
model names or query a mutable provider catalog.

The character estimate is neither a provider tokenizer nor a hard token
guarantee. Non-ASCII input and tokenizer-specific behavior can require more
tokens. Exact rendered characters and UTF-8 bytes are therefore retained next
to provider-reported token usage, and experiments can choose lower explicit
character limits when their routes need more margin.

Context telemetry is recorded both per packet/batch and in aggregate. Exact
measurements include rendered characters, UTF-8 bytes, exposed file paths,
changed/unchanged/cross-file counts, budget utilization, evidence rows rendered
or omitted, exact generation/verifier prompt sizes, limit-hit flags, and
truncation reasons. Token counts derived from characters are explicitly labeled
estimates; provider-reported call usage remains separate.
Agentic traces add selection rounds, accepted/rejected/deduplicated requests,
omitted and failed requests, action counts, lines and hits returned, search/list
pagination pressure, cumulative blob-read usage, selected files, context size,
and failure status without persisting search queries or model rationale.
Verifier traces separately report evidence available before allocation,
evidence retained by the verifier budget, evidence retained after exact prompt
fitting, and file-level omissions.

## Model gateway

Any provider-prefixed model ID available through Martian Gateway is accepted.
Examples include `openai/gpt-5.6-luna`, `anthropic/...`, and `google/...`.
`codex/gpt-5.6-luna` selects the secure Codex CLI adapter for a pre-existing
ChatGPT/Codex login.

Each call records requested and resolved model, gateway, stage, chunk, latency,
token usage and cost when available, request/schema/response hashes, and errors.
Artifacts also record secret-free transport limits, exact HTTP-client/Codex
version, auth mode, and a hashed API-base identity independently for generation
and verification. Secrets and raw authentication material are never stored.
Martian enforces the configured completion cap. Codex CLI provenance labels it
as a planning reserve because that transport does not expose a per-call output
cap.

The Codex adapter applies configured reasoning effort. On Martian Chat
Completions, the parameter is sent only to `openai/*` reasoning routes because
Martian documents it as OpenAI-only. Other provider prefixes use deterministic
temperature instead. Stage-specific provenance records the requested effort and
whether BugBunny is configured to send the transport parameter, so
cross-provider experiments do not silently claim an unsupported setting.

The current-login Codex adapter starts in an empty temporary directory, passes a
minimal environment required for the existing login, ignores user/project
configuration and rules, and disables model-visible shell, browser, app,
plugin, skill, and multi-agent features. Cancellation kills and reaps the child
process while retaining any safe partial telemetry. The shared gateway rejects
non-finite retry delays, validates schema string patterns, and redacts secrets
resolved from arguments, environment variables, or dotenv before a CLI error
can escape. Repository text is only prompt data.

## Export and evaluation integrity

The exporter updates shared CodeReviewBench inputs under one cross-process
lock. It preserves existing review rows from other tools, records per-case
fixture/base/head/diff identity in every new manifest, checks that identity
against prior BugBunny manifests, refreshes sibling judge-directory manifests
when the shared `benchmark_data.json` changes, and commits the export index
last. Verification rejects a bundle containing candidate, dedup, or review rows
for a BugBunny tool ID that has no committed manifest, which makes an
interrupted export visible instead of judgeable.

The statistical analyzer performs candidate-audit/run-artifact identity checks
before attributing judge decisions. It fails on judge-error rows unless the
caller explicitly requests `--allow-judge-errors`, in which case those rows are
excluded on the paired clean-case intersection and surfaced as degraded input
rather than folded into
precision, recall, confidence intervals, or threshold curves.

## Hardening status and deliberate boundaries

Version `0.8.0` enforces the second-pass invariants above. In particular:

- author-controlled generated markers no longer exclude a file; name/path
  policy remains explicit and observable;
- generation findings are batch-local, verifier retries are independently
  fitted and attributed, and all excerpt line numbers use Git's LF semantics;
- model and judge structured outputs reject duplicate keys, non-finite values,
  type coercion, unknown fields, and invalid ranges;
- agentic selection has separate finite queue, operation, action, and cumulative
  blob-read budgets, including failed or timed-out reads;
- exact Git paths survive diff parsing, model hydration, grounding, export, and
  publication without trimming; control characters remain intentionally
  unsupported in the line-oriented agent protocol;
- export indexes are cumulative, sibling judge manifests/indexes are refreshed
  under the root bundle lock, and foreign current-build metadata fails before
  the first Step 3 write;
- analysis verifies the run, index, every export manifest and candidate-audit
  hash, each run artifact's canonical export identity, the ordered pair matrix
  and stored reduction, one common judge identity, exact populations, and the
  paired clean-case intersection used by `--allow-judge-errors`.

Some boundaries are intentional rather than unfinished correctness work. A
CodeReviewBench bundle consists of several files, so crash consistency is
fail-closed through hashes/manifests rather than a filesystem-wide atomic
rename. GitHub offers no server-side review idempotency key, so publication is
coordinated across local processes but not perfectly across independent hosts.
BugBunny does not execute repository code. Broader vendor/generated exclusion
policy and a larger engine decomposition are deferred design decisions listed
in [`AUDIT_TODO.md`](../AUDIT_TODO.md).
