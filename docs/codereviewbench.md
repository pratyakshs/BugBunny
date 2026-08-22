# CodeReviewBench integration

BugBunny integrates with the fixed 50-case offline benchmark in
[`withmartian/code-review-benchmark`](https://github.com/withmartian/code-review-benchmark/tree/2b092b670f7d6cae6d429babaaee18948b4bdacb).
The benchmark source and its GitHub fixture organization serve different roles:

- the source repository contains human-curated golden issues, ingestion,
  extraction, deduplication, judging, scoring, and dashboard code;
- [`code-review-benchmark`](https://github.com/code-review-benchmark) contains
  tool-specific cloned repositories on which the review products ran.

BugBunny depends on neither project at runtime. Its bridge reads and writes the
benchmark's published JSON schemas.

## How the organization is structured

Fixture repository names follow the parser in
[`step1_download_prs.py`](https://github.com/withmartian/code-review-benchmark/blob/2b092b670f7d6cae6d429babaaee18948b4bdacb/offline/code_review_benchmark/step1_download_prs.py):

```text
{config_prefix}__{source_repo}__{tool_slug}__PR{source_pr_number}__{date}
```

For example, with a generic tool slug:

```text
cal_dot_com__cal.com__exampletool__PR8087__20260122
```

The fixture repository's review surface is pull request `#1`. Its base and head
reproduce the source pull request identified by `PR8087`; the number encoded in
the repository name is the source PR number, not the fixture PR number. The
third component identifies the tool whose comments Step 1 is expected to
collect.

Within the source repository, the relevant offline layout is:

```text
offline/
  golden_comments/             human-curated issues for 50 source PRs
  code_review_benchmark/
    step0_fork_prs.py           creates tool-specific fixture repositories
    step1_download_prs.py       joins fixture comments to golden cases
    step2_extract_comments.py   converts review prose into candidates
    step2_5_dedup_candidates.py groups semantically duplicate candidates
    step3_judge_comments.py     matches candidates to golden issues
  results/
    benchmark_data.json
    {judge_model}/
      candidates.json
      dedup_groups.json
      evaluations.json
```

`benchmark_data.json` is keyed by the original, golden PR URL. Each value holds
the golden comments plus a `reviews` array. A review entry identifies a tool,
its fixture PR URL, and the comments collected from that fixture.

## Why BugBunny reuses the existing fixtures

The published JSON contains fixture review entries for the benchmark cases.
BugBunny treats the selected fixture URLs strictly as code-only inputs:

1. load and validate `benchmark_data.json`;
2. deterministically select one fixture per case, or require the requested
   `--fixture-tool` entry for each golden URL;
3. resolve fixture PR `#1` to its full base and head commit IDs and verify their
   merge base;
4. fetch those objects into BugBunny's local content-addressed Git cache;
5. review the exact diff without fetching or reading fixture comments.

No new remote repositories are needed for local offline evaluation. The first
run still populates BugBunny's own safe local object cache, and subsequent
models reuse it. BugBunny deliberately does not discover or trust arbitrary
working clones outside its managed cache, so an existing local clone is not
automatically imported.

This reuse is valid because the evaluated input is the code at the exact
merge-base/head diff addressed by the resolved PR SHAs, not the historical
comments in the repository. The run artifact
records those SHAs and the resulting diff hash.

## No golden leakage

The loader validates and hashes the golden records so export can later prove
that they were not changed. It does not retain golden comment text in the case
passed to the review engine. During review, the engine receives only the
fixture PR's URL and metadata, exact Git snapshots, diff, and repository
context. The golden URL and hash are attached afterward as join metadata.

Do not point `review-pr` at an original golden PR for an official measurement:
its title, body, discussion, or follow-up changes may expose information that
the frozen fixture intentionally excludes.

## Run the 50 cases or a smoke subset

From the BugBunny checkout:

```bash
bugbunny benchmark run \
  --benchmark-data /path/to/code-review-benchmark/offline/results/benchmark_data.json \
  --fixture-tool auto \
  --model codex/gpt-5.6-luna \
  --cache-dir .bugbunny-cache \
  --active-reviews 2 \
  --run-dir runs/codex-luna
```

For a single case, filter on any substring of its case ID, source repository,
golden URL, or fixture URL:

```bash
bugbunny benchmark run \
  --benchmark-data /path/to/code-review-benchmark/offline/results/benchmark_data.json \
  --fixture-tool auto \
  --model codex/gpt-5.6-luna \
  --filter cal.com/pull/8087 \
  --limit 1 \
  --run-dir runs/cal-8087
```

Repeat `--model` to evaluate multiple generation models against identical
fixtures. BugBunny pre-resolves each selected PR once, records its URL/base/head
snapshot in an immutable `job_plan.json`, and gives that snapshot to every
model. Resume reads the frozen plan instead of re-resolving moving PR refs.
Pinning one `--verifier-model` across the sweep avoids changing two model roles
at once.

Scheduling has distinct limits so network, Git, review, and model work do not
serialize each other:

- `--github-concurrency 16` resolves PR metadata using `GITHUB_TOKEN`,
  `GH_TOKEN`, or the current `gh auth` login;
- `--git-concurrency 4` prewarms independent per-remote caches using Git
  HTTP/1.1;
- `--active-reviews 10` bounds concurrent case/model jobs;
- `--llm-concurrency 4` bounds generation calls inside one review;
- `--global-llm-concurrency 16` bounds all generation and verifier calls across
  the entire sweep.

The largest prepared diffs start first. The balanced verifier remains
sequential within a review. `--concurrency` is a deprecated alias for
`--active-reviews`.

For a Martian-backed high-reasoning sweep, place `MARTIAN_API_KEY` in the
ignored `.env` file and run:

```bash
bugbunny benchmark run \
  --benchmark-data /path/to/code-review-benchmark/offline/results/benchmark_data.json \
  --fixture-tool auto \
  --model openai/gpt-5.6-luna \
  --model openai/gpt-5.6-terra \
  --reasoning-effort high \
  --verifier-reasoning-effort high \
  --active-reviews 10 \
  --global-llm-concurrency 16 \
  --git-concurrency 4 \
  --run-dir runs/martian-high-sweep
```

## Compare context policies without changing the review policy

Context mode is not a third review profile. Keep the fixture set, generation
model, `fast`/`balanced` profile, verifier, judge, reasoning effort, and
concurrency policy fixed while varying only context acquisition. The default
`curated` mode uses generous deterministic bounds; `agentic` lets the review
model select additional evidence through bounded, read-only pageable-list,
read, and pageable literal-search actions. The agentic file cap covers distinct
additional files; the curated seed population is logged separately.

For model sweeps, declare each route's verified input capacity rather than
relying on model-name inference. The declarations deterministically derive
model-specific limits and are frozen into the artifacts:

```bash
bugbunny benchmark run \
  --benchmark-data /path/to/code-review-benchmark/offline/results/benchmark_data.json \
  --fixture-tool auto \
  --model provider/model-a \
  --model provider/model-b \
  --model-context-window provider/model-a=200000 \
  --model-context-window provider/model-b=32000 \
  --context-mode agentic \
  --profile balanced \
  --verifier-model provider/shared-verifier \
  --verifier-context-window-tokens 200000 \
  --run-dir runs/context-agentic
```

Use only window values verified for the selected Martian routes. A single-model
run can use `--context-window-tokens N`; the repeatable model-qualified form is
safer for heterogeneous sweeps. Explicit `--max-chunk-chars`,
`--max-context-chars`, and selector limits remain available when the experiment
requires fixed resources instead of model-native capacity. When using a shared
verifier, pass its verified capacity with
`--verifier-context-window-tokens N`; BugBunny derives and freezes a separate
full-prompt budget for verifier candidates and evidence.

Keep reasoning policy matched only where the transport supports it. The
Martian Chat Completions `reasoning_effort` parameter is sent for
`openai/*` reasoning routes and omitted for other provider prefixes; every
artifact records requested effort and whether BugBunny is configured to send
the transport parameter. Context-policy examples above intentionally leave
reasoning unchanged because it is a separate experimental axis.

If several models produce unexpectedly similar scores, treat a context-ceiling
hypothesis as a new matched experiment. Do not loosen bounds automatically in
the middle of a run. Run at least these frozen arms in separate directories:

1. a deliberately tight `curated` arm, for example with
   `--max-chunk-chars 36000`, `--max-context-chars 18000`, and
   `--initial-context-chars 18000`;
2. the generous default `curated` arm;
3. the generous `agentic` arm.

Use the same 50 cases and exact refs, pin one verifier and judge, repeat arms if
provider sampling is nondeterministic, and compare paired per-case score
deltas—not only aggregate F1. Before attributing wider model spread to model
quality, inspect each artifact's exact exposed files and character counts,
provider-reported input tokens, budget utilization, truncation, omitted
evidence, agentic selection metrics, and verifier allocation/final-fit
omissions. Verify that exported manifests have identical case sets, base/head
SHAs, and diff hashes. If loosening context
reliably increases between-model spread beyond repeat noise, the tight arm was
compressing the measurement; if it does not, context bounds are not supported
as the cause.

The run directory contains a manifest and one JSON plus Markdown artifact per
case/model:

```text
runs/codex-luna/
  job_plan.json
  run_checkpoint.json
  run_manifest.json
  artifacts/
    codex_gpt-5.6-luna--{model-hash}/
      {case_id}.json
      {case_id}.md
```

## Direct candidate export

The benchmark's
[`step2_extract_comments.py`](https://github.com/withmartian/code-review-benchmark/blob/2b092b670f7d6cae6d429babaaee18948b4bdacb/offline/code_review_benchmark/step2_extract_comments.py)
concatenates a tool's review prose and asks another LLM to reconstruct distinct
issues. That is necessary for heterogeneous third-party review formats, but it
is lossy for BugBunny: its final artifact already contains one grounded,
structured issue per causal site.

Export final findings directly:

```bash
bugbunny benchmark export \
  --benchmark-data /path/to/code-review-benchmark/offline/results/benchmark_data.json \
  --run-dir runs/codex-luna \
  --judge-model openai/gpt-5.2 \
  --output-dir /tmp/bugbunny-results
```

For each review model, the exporter:

- creates a deterministic tool ID such as
  `bugbunny-codex-gpt-5-6-luna-{config-hash}`, keeping model, harness,
  profile, verifier, prompt, and runtime configurations distinct;
- inserts final findings as review comments in a copy of
  `benchmark_data.json`;
- writes the same findings directly to
  `{judge_model}/candidates.json`, bypassing Step 2;
- writes singleton groups to `{judge_model}/dedup_groups.json`, because the
  harness has already performed exact-root-cause deduplication;
- records canonical artifact-value hashes, golden hashes, and exact hashes of
  all three emitted Step 3 files in a model-qualified
  `bugbunny-{tool-model}_export_manifest.json` and
  refuses stale, partial, non-fixture, mixed-configuration, or golden-changing
  exports.

All review models for one judge share the same three physical Step 3 files.
Adding a model refreshes every prior BugBunny manifest to the final bundle
hashes; `bugbunny_export_index.json` binds those final manifest hashes as well.

When artifacts contain multiple review models, export additionally requires an
identical case population and matching golden URL, review URL, base SHA, head
SHA, and diff hash per case. This prevents a moving fixture from becoming a
misleading model comparison.

The output directory is shaped like CodeReviewBench's `offline/results/`:

```text
/tmp/bugbunny-results/
  benchmark_data.json
  openai_gpt-5.2/
    candidates.json
    dedup_groups.json
    bugbunny-{tool-model}_export_manifest.json
    bugbunny_export_index.json
```

## Run the Step 3 judge

BugBunny can judge the exported bundle directly through Martian without
copying it into another checkout. Verify a model-qualified manifest first,
then select one or more exact tool IDs from `bugbunny_export_index.json`:

```bash
bugbunny benchmark verify-export \
  --manifest /tmp/bugbunny-results/openai_gpt-5.2/bugbunny-{tool-model}_export_manifest.json

bugbunny benchmark judge \
  --results-dir /tmp/bugbunny-results \
  --judge-model openai/gpt-5.2 \
  --tool bugbunny-{first-model-and-config-id} \
  --tool bugbunny-{second-model-and-config-id} \
  --judge-concurrency 20 \
  --review-concurrency 10 \
  --force
```

Omit `--tool` to judge every exported review in the bundle. All golden/candidate
comparisons across selected tools share one global Martian queue; there is no
per-PR request-batch barrier. Comparison results are still reduced in the
benchmark's original order, preserving its TP/FP/FN behavior. The runner writes
an atomic checkpoint after every completed review/tool pair and resumes all
error-free records. Its JSON summary reports aggregate precision, recall, F1,
review count, and judge errors for each tool.

The judge model is independent of both BugBunny's generation model and optional
verifier; record all three roles when comparing results. The output remains the
standard `{judge_model}/evaluations.json`, so the benchmark's scoring and
dashboard tools can consume it unchanged. Use a disposable benchmark checkout
only when independently cross-checking with its command-line Step 3 runner.

## When dedicated fixtures are required

Read-only reuse and GitHub publication have different rules.

CodeReviewBench Step 1 derives tool identity from the fixture repository name
and collects bot-authored comments already present on its PR. Publishing a
BugBunny review into a fixture owned by another tool would contaminate that
entry and misattribute BugBunny's comments. Never publish to a borrowed fixture.

If the experiment must exercise the benchmark's GitHub post-to-Step-1 ingestion
path, create one dedicated fixture per case. Use BugBunny's
`dedicated_fixture_tool()` result as the tool component: it is at most 30
characters because Step 0 truncates longer slugs while creating repository
names. Publish only to repositories whose tool component begins `bugbunny-`;
the publisher refuses borrowed organization fixtures even when artifact
metadata is absent.

Step 1 normally retains only comments authored by a login ending in `[bot]`.
Use a GitHub App/bot identity for the untouched path. If a normal PAT is used,
the benchmark must be patched to add the exact BugBunny tool slug to its
`_NON_BOT_TOOLS` set; doing so also risks collecting unrelated human comments,
so use an isolated dedicated fixture. Add the exact slug to ingestion and run
Steps 1–3. Separate fixtures are required for configurations that must remain
independently attributable.

For normal offline model evaluation, the direct structured exporter is both
faster and less lossy, and requires no new GitHub repositories or writes.
