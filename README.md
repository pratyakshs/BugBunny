# BugBunny

BugBunny is a fast, standalone LLM code-review harness designed for reproducible
local reviews and the 50-case CodeReviewBench offline evaluation. It resolves
exact base/head Git commits and reviews the merge-base-to-head PR diff, assigns every eligible diff hunk to a model call,
grounds findings on added or deleted lines, and retains the evidence needed to explain
both accepted and rejected findings.

See [architecture](docs/architecture.md) for the pipeline invariants and
[CodeReviewBench integration](docs/codereviewbench.md) for the offline workflow.

## Highlights

- Lossless diff chunking records complete, partial, or failed hunk coverage; a
  failed shard is never reported as a clean review.
- Configurable whole-repository context defaults to generous deterministic
  evidence and can optionally let the review model choose additional bounded,
  read-only file reads and literal searches.
- Diff chunks are reviewed concurrently. `fast` uses one model stage;
  `balanced` adds a batched precision verifier.
- Benchmark runs resolve inputs once, prewarm per-repository Git caches, start
  larger diffs first, and schedule every case/model pair through shared global
  review and model-call limits.
- Deterministic gates check path, changed side/line, cited base-or-head evidence, category, and
  exact duplicates before a finding can be final.
- Raw proposals, deterministic candidates, rejected proposals, final findings, exclusions, exact refs,
  diff hash, model calls, token usage, latency, and available cost are preserved
  in one JSON artifact. There is no early finding cap.
- API-backed models route directly through the Martian Gateway, making provider
  and model sweeps a CLI setting. A narrow `codex/*` adapter reuses an existing
  ChatGPT/Codex login.

## Install

BugBunny requires Python 3.11 or newer and Git.

```bash
git clone https://github.com/pratyakshs/BugBunny.git
cd BugBunny
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
bugbunny --version
```

Check Git, Martian authentication, and the Codex CLI login without printing any
secret values:

```bash
bugbunny doctor
```

## Model authentication

### Reuse the current ChatGPT/Codex login

Sign in once with the installed Codex CLI, then select the `codex/` transport:

```bash
codex login
bugbunny doctor
bugbunny review-pr https://github.com/OWNER/REPO/pull/NUMBER \
  --model codex/gpt-5.6-luna \
  --output review.json \
  --markdown review.md
```

BugBunny invokes `codex exec` from an empty temporary directory with an output
schema, ephemeral session, read-only sandbox, minimal allowlisted environment,
and all model-visible shell/browser/app/plugin tools disabled. It reuses the
CLI session but never reads or converts the credential cache. OpenAI documents the supported
[Codex sign-in methods](https://learn.chatgpt.com/docs/auth) and
[`codex exec` flags](https://learn.chatgpt.com/docs/developer-commands?surface=cli).

### Use the Martian Gateway

The default model is the Martian route `openai/gpt-5.6-luna`. Copy the committed
template and put your key in the ignored local file:

```bash
cp .env.example .env
# Edit .env and set MARTIAN_API_KEY.
bugbunny review-pr https://github.com/OWNER/REPO/pull/NUMBER \
  --model openai/gpt-5.6-luna \
  --output review.json
```

BugBunny reads `MARTIAN_API_KEY` from the environment first and then from `.env`.
`--api-key-env NAME`, `--env-file PATH`, and `--api-key` are also supported;
the first two keep the secret out of shell history. API requests go directly to
`https://api.withmartian.com/v1/chat/completions` by default. Use `--api-base`
only for a compatible custom endpoint. Supply any provider-prefixed model ID
available through Martian, such as `openai/...`, `anthropic/...`, or `google/...`.
Use `codex/*` specifically for the separate logged-in Codex transport. See
Martian's [HTTP integration](https://docs.withmartian.com/integrations/http-client)
and [model catalog](https://docs.withmartian.com/api-reference/models).

## Review profiles

`balanced` is the default. It generates atomic candidates in parallel, applies
deterministic grounding gates, then asks the configured verifier to keep, drop,
or merge candidates in batches. A conservative same-site semantic pass removes
paraphrases that crossed verifier-batch boundaries without collapsing independent
occurrences. The verifier defaults to `same`; pin a separate
model with `--verifier-model PROVIDER/MODEL`, or disable it with
`--verifier-model none`.

Verifier confidence must be calibrated outside the benchmark. BugBunny ships a
versioned, manually labeled synthetic corpus that explicitly contains no
CodeReviewBench cases. Run the pinned verifier once, freeze its operating point,
and pass that immutable file to every compared model:

```bash
bugbunny calibrate \
  --corpus calibration/verifier_corpus.json \
  --output calibration/opus-operating-point.json \
  --verifier-model anthropic/claude-opus-4-5

bugbunny benchmark run \
  --benchmark-data /path/to/benchmark_data.json \
  --model openai/gpt-5.6-luna \
  --verifier-model anthropic/claude-opus-4-5 \
  --operating-point calibration/opus-operating-point.json
```

The operating point binds the corpus bytes, labels, verifier responses, model,
reasoning setting, prompt, schema, objective, and threshold. A run rejects a
file produced by a different verifier contract. A manual
`--min-verifier-confidence` remains available for diagnosis, but should not be
tuned on the canonical 50 cases.

## Review policies

Execution profile and review scope are independent. `review-pr` defaults to the
versioned `production` policy: behaviorally meaningful correctness, security,
data, concurrency, API, performance, test-behavior, and material documentation
defects. `benchmark run` defaults to the versioned `codereviewbench` policy,
whose broader scope also admits concrete benchmark style, maintainability,
compatibility, and uncertainty findings. Every prompt and artifact records the
selected policy version and hash, preventing a model sweep from silently
comparing different targets.

`fast` makes only the generation calls and retains everything that passes the
objective location/evidence gates. Generator self-confidence is recorded but is
not used as a cross-model filter because providers do not calibrate it uniformly:

```bash
bugbunny review-pr https://github.com/OWNER/REPO/pull/NUMBER \
  --model openai/YOUR_MODEL \
  --profile fast \
  --output review.json
```

Both profiles use `--llm-concurrency` to bound parallel calls. `fast` is useful
for inexpensive broad sweeps; `balanced` is the precision-oriented default.
`--max-output-tokens` defaults to 32,768 so high-reasoning models have room for
their internal reasoning plus the required structured review output. Martian
enforces it; the Codex CLI adapter uses it as a declared planning reserve.

`--reasoning-effort` and `--verifier-reasoning-effort` are sent by the Codex
adapter and by Martian for `openai/*` routes. Martian's Chat Completions
[`reasoning_effort` parameter](https://docs.withmartian.com/api-reference/endpoints)
is limited to OpenAI reasoning models, so BugBunny omits it for `anthropic/*`,
`google/*`, and other Martian prefixes. Artifacts record both the requested
value and whether BugBunny is configured to send the transport parameter; a
requested `high` value must not be interpreted as sent when that flag is false.
The gateway also rejects `temperature` for some catalog routes; BugBunny keeps
an explicit model-scoped compatibility list and records `temperature_applied`
as false for those routes without changing fixed-temperature behavior elsewhere.

## Repository context

Context acquisition is an independent experiment axis, not a review profile.
Use either `fast` or `balanced` with either context mode:

- `--context-mode curated` (the default) deterministically supplies changed-file
  source plus repository definitions, callers, imports, related tests, and risk
  hypotheses. The generous fixed defaults allow 72,000 patch characters and
  120,000 context characters per generation batch.
- `--context-mode agentic` starts from a curated seed, then lets the same review
  model maintain falsifiable evidence hypotheses and request additional context.
  Hypotheses guide discovery only; they are never treated as final findings.
  The fixed default seed is up to 36,000
  characters; declared-window runs automatically leave a substantial share for
  selected evidence, including at a 32K window. The
  selector can only page through immutable inventory paths, read bounded line
  ranges, and page through bounded literal searches. Large repositories begin
  with a hierarchical directory/count summary instead of an alphabetically
  clipped file prefix, and `list` can discover remaining subtrees. It cannot run project
  code, issue shell commands, make network calls, or write to the repository.

All bounds are explicit CLI options, including `--max-chunk-chars`,
`--max-context-chars`, `--initial-context-chars`,
`--context-selection-rounds`, `--context-requests-per-round`,
`--max-context-files`, `--context-blob-read-bytes`, and the per-read/search
limits shown by `--help`. `--max-context-files` limits distinct files added by
agentic actions; files already represented by the curated seed are measured
separately in the artifact.
For example, a single model can use a user-verified 200,000-token window:

```bash
bugbunny review-pr https://github.com/OWNER/REPO/pull/NUMBER \
  --model provider/model-a \
  --context-mode agentic \
  --context-window-tokens 200000 \
  --output review.json
```

`--context-window-tokens` is a declaration, not provider discovery. BugBunny
reserves completion and protocol room, then derives reproducible patch,
context, selector, and repository-index bounds. The character-to-token
conversion is a planning estimate, so artifacts also record exact rendered
prompt characters, UTF-8 bytes, and provider-reported token usage. It is not a
provider tokenizer or a hard token guarantee—non-ASCII and route-specific
tokenization can consume more tokens—so use verified windows and lower explicit
character bounds when a route needs extra margin. BugBunny never guesses
capacity from a model name or a mutable model catalog. In a sweep, repeat
`--model-context-window MODEL=N` so each model receives bounds derived from its
own declared window:

```bash
bugbunny benchmark run \
  --benchmark-data /path/to/code-review-benchmark/offline/results/benchmark_data.json \
  --fixture-tool auto \
  --model provider/model-a \
  --model provider/model-b \
  --model-context-window provider/model-a=200000 \
  --model-context-window provider/model-b=32000 \
  --context-mode agentic \
  --run-dir runs/agentic-window-matched
```

Use only context-window values verified for the selected routes. The resolved
budgets and their `fixed` or `declared_window` source are frozen in every
artifact, so a resumed run cannot silently adopt different limits. When a
declared generation window is used, a balanced run with a pinned verifier must
also pass `--verifier-context-window-tokens N`; verifier candidates,
patch/source anchors, and cross-file context are fitted to that separately
declared window.

Martian requests apply `--max-output-tokens` at the transport. The logged-in
Codex adapter records the same value as a planning reserve because the Codex
CLI has no corresponding per-call completion-cap setting; provenance states
whether the transport applied the cap.

Reviewing is local by default. It creates JSON and optional Markdown artifacts
and does not post to GitHub. Publishing is a separate, explicit command:

```bash
export GITHUB_TOKEN="..."
bugbunny publish review.json --confirm-publish
```

## Run CodeReviewBench offline

The recommended workflow reuses the existing read-only fixture PRs already
listed in CodeReviewBench's `benchmark_data.json`; it does not create another
50-repository fixture set. By default, BugBunny deterministically selects one
existing fixture per case, resolves its exact base/head SHAs, reconstructs the
merge-base/head diff, and never downloads its review comments. Use
`--fixture-tool TOOL_SLUG` to pin a specific fixture set.

```bash
bugbunny benchmark run \
  --benchmark-data /path/to/code-review-benchmark/offline/results/benchmark_data.json \
  --fixture-tool auto \
  --model codex/gpt-5.6-luna \
  --run-dir runs/codex-luna
```

The first run fetches each selected fixture into BugBunny's safe local object
cache; later runs and models reuse that cache. Before a model sweep starts,
BugBunny resolves every fixture exactly once and freezes its complete PR
metadata, base SHA, and head SHA in `job_plan.json`. A resumed run uses that
immutable plan rather than querying moving PR refs again. BugBunny does not
import arbitrary working clones from outside its managed cache.

Repeat `--model` for a model sweep. This runs both requested models through
Martian, with high reasoning for generation and each model's own verifier:

```bash
bugbunny benchmark run \
  --benchmark-data /path/to/code-review-benchmark/offline/results/benchmark_data.json \
  --fixture-tool auto \
  --model openai/gpt-5.6-luna \
  --model openai/gpt-5.6-terra \
  --reasoning-effort high \
  --verifier-reasoning-effort high \
  --active-reviews 10 \
  --llm-concurrency 4 \
  --global-llm-concurrency 16 \
  --github-concurrency 16 \
  --git-concurrency 4 \
  --run-dir runs/martian-high-sweep
```

To isolate generation-model quality, instead pin one shared
`--verifier-model PROVIDER/MODEL` for the entire sweep.

Use `--filter TEXT` and/or `--limit N` for a smoke run. Completed artifacts are
resumed by default only after exact dataset, refs, configuration, runtime,
coverage, version, and prompt checks; `--no-resume` forces a rerun. BugBunny
requires the standard 50-case source file before applying the smoke selection,
and refuses to reuse a run directory for a different experiment. Each finished
case/model record is committed to `run_checkpoint.json`; a final
`run_manifest.json` commits the complete run. Fully resumed cases skip Git
prewarming.

The defaults permit 10 active reviews, four model calls inside each review, and
16 model calls globally. Repository preparation uses four independent
per-remote Git caches over HTTP/1.1, while authenticated GitHub metadata
resolution permits 16 requests. Larger prepared diffs enter the review queue
first. The optional balanced verifier remains sequential within each review.
`--concurrency` is retained as a deprecated alias for `--active-reviews`.

Export one or more explicitly named candidate tracks directly to the files
consumed by the official judge:

```bash
bugbunny benchmark export \
  --benchmark-data /path/to/code-review-benchmark/offline/results/benchmark_data.json \
  --run-dir runs/sweep-01 \
  --judge-model openai/gpt-5.2 \
  --output-dir /tmp/bugbunny-results \
  --finding-stage generator \
  --finding-stage balanced \
  --finding-stage family
```

The export writes:

```text
/tmp/bugbunny-results/
  benchmark_data.json
  openai_gpt-5.2/
    candidates.json
    dedup_groups.json
    bugbunny-{stage-model-and-config-id}_candidate_audit.json
    bugbunny-{stage-model-and-config-id}_export_manifest.json
    bugbunny_export_index.json
```

`generator` measures every deterministically grounded proposal, `balanced`
measures the frozen verifier operating point, and `family` presents repeated
causal patterns once while retaining every atomic member ID and location in its
audit sidecar. These tracks isolate generation capability, precision filtering,
and presentation effects without rerunning the review model. BugBunny bypasses
Step 2's prose re-extraction because each finding is already structured and
includes the location in candidate text because the official judge compares
text. The exporter accepts only current,
completed, fully covered fixture artifacts whose dataset and per-case golden
hashes match. For multi-model exports it also requires identical case sets and
identical golden URL, fixture URL, base, head, and diff hash for every case.
Original golden fields are hash-checked before and after export. Each
model-qualified manifest binds the exact bytes of all three shared Step 3
inputs. Verify the bundle after copying it and before invoking the judge:

```bash
bugbunny benchmark verify-export \
  --manifest /tmp/bugbunny-results/openai_gpt-5.2/bugbunny-{model-and-config-id}_export_manifest.json
```

Run the compatible Step 3 judge directly from BugBunny. Repeat `--tool` to
select multiple exported tool IDs, or omit it to judge every review in the
bundle:

```bash
bugbunny benchmark judge \
  --results-dir /tmp/bugbunny-results \
  --judge-model openai/gpt-5.2 \
  --tool bugbunny-{first-model-and-config-id} \
  --tool bugbunny-{second-model-and-config-id} \
  --judge-concurrency 20 \
  --review-concurrency 10 \
  --force
```

The judge keeps the benchmark prompt and metric reduction unchanged, sends all
selected tools through one bounded Martian request queue, atomically
checkpoints each completed review to `evaluations.json`, and prints aggregate
precision, recall, and F1. It also retains every golden/candidate pair decision,
confidence, retry count, and safe retry reason for audit. A rerun resumes
error-free records automatically.

Balanced reviews also retry a structurally valid verifier response when its
dynamic decision relationships are invalid (for example, a merge that points
forward). The default is two retries, configurable with
`--verification-semantic-retries N`; exhaustion remains fail-closed and every
attempt is audited.

Gateway retries cover malformed JSON and local schema violations as well as
retryable transport/HTTP failures. Their attempt count, safe error trace, and
aggregate token/cost telemetry are stored in the review artifact.

After judging, produce JSON and Markdown diagnostics with pull-request-level
bootstrap intervals, paired model deltas, pipeline attrition, category counts,
context pressure, retries, and threshold curves reconstructed from the fixed
generator pair matrix:

```bash
bugbunny benchmark analyze \
  --run-dir runs/sweep-01 \
  --results-dir /tmp/bugbunny-results \
  --judge-model openai/gpt-5.2
```

The analyzer never changes scores or makes new model calls. Threshold curves
are diagnostic only; the reported operating point remains the externally frozen
one used during the run.

See [CodeReviewBench integration](docs/codereviewbench.md) for the fixture
layout, reuse decision, exact judge workflow, and why publishing requires
dedicated BugBunny fixtures.

## Artifacts and telemetry

Every case records:

- exact PR URL, base/head and merge-base SHAs, diff hash, changed-line ledger,
  changed-file statistics, and config;
- eligible, excluded, completed, and failed file/hunk coverage;
- exact context characters, UTF-8 bytes, file paths and counts; changed,
  unchanged, and cross-file coverage; budget utilization; truncation and
  omission reasons; clearly labeled token estimates; agentic action/round and
  pagination metrics; verifier preallocation and final-fit omissions; and
  provider-reported input tokens when available;
- requested/resolved model, secret-free transport/runtime configuration, stage,
  chunk, latency, tokens, available cost, attempt/retry provenance,
  request/schema/response hashes, and errors for every call;
- all raw, deterministically validated, verifier-rejected, family-labeled, and
  final findings;
- run status (`completed`, `partial`, or `failed`) and benchmark join metadata.

This keeps coverage loss, generation failure, grounding rejection, verifier
rejection, and judge mismatch separately diagnosable.

## Security boundaries

BugBunny treats the repository, diff, PR title, and PR body as untrusted data.
It does not execute repository hooks, filters, package scripts, tests, linters,
or build commands. Repository context uses bounded reads from exact Git objects
and a separately materialized snapshot. Model code runs outside the repository,
and the review engine never receives a GitHub write capability. Publishing is
an explicit user action and validates the artifact against the resolved PR.

## Current limitations

- “Offline” describes CodeReviewBench's frozen dataset and judge pipeline, not
  an air-gapped run. Initial fixture acquisition and hosted models need network
  access; a warmed Git cache only removes repeated Git downloads.
- The CLI currently targets public GitHub pull-request URLs. It does not import
  arbitrary existing working clones into its cache.
- Findings anchor to RIGHT-side additions or LEFT-side deletions. Binary,
  generated, vendored, lockfile, combined-diff, and metadata-only changes are
  excluded and reported.
- No project code is executed, so defects provable only through builds, tests,
  generated artifacts, or runtime behavior may be missed.
- Model quality, cost, and latency remain provider- and prompt-dependent. The
  repository does not claim a benchmark score or full-suite timing until such a
  run is recorded reproducibly.
- CodeReviewBench itself is static, its golden set may be incomplete, and its
  LLM judge is model-dependent. Report the review model, verifier model, judge
  model, profile, and artifact manifest with every result.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```

Tests use fake model transports and local Git fixtures unless a test explicitly
states otherwise. The project is MIT licensed.
