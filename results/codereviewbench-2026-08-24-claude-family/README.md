# CodeReviewBench: Claude Sonnet 5, Opus 5, Opus 4.8, and Fable 5

This is a complete 50-case model sweep completed on 2026-08-24 through the
Martian Gateway. All four candidate models used the same BugBunny pipeline,
declared context capacity, fixed verifier, calibrated operating point, and
fixed judge. The predeclared primary comparison is the `balanced` stage.

## Primary result

| Model | Precision | Recall | F1 | F1 95% PR-bootstrap CI | Candidates |
| --- | ---: | ---: | ---: | ---: | ---: |
| `anthropic/claude-opus-4-8` | 0.8000 | 0.2081 | 0.3303 | [0.2449, 0.4147] | 40 |
| `anthropic/claude-sonnet-5` | 0.7907 | 0.1965 | 0.3148 | [0.2170, 0.4093] | 41 |
| `anthropic/claude-opus-5` | 0.7857 | 0.1908 | 0.3070 | [0.2165, 0.3923] | 40 |
| `anthropic/claude-fable-5` | 0.8333 | 0.1734 | 0.2871 | [0.1881, 0.3810] | 34 |

Opus 4.8 has the highest point-estimate F1, but none of the six paired
pull-request bootstrap comparisons excludes zero. The observed ordering is
therefore suggestive, not statistically resolved on this 50-case sample.

The calibrated operating point is deliberately high precision and low recall.
The generator-stage behavior is much more dispersed: Opus 5 generated 495
validated candidates with 0.7225 recall and 0.2515 precision, while Opus 4.8
generated 189 with 0.5145 recall and 0.4588 precision. The fixed verifier and
confidence threshold reduced all four models to 34–41 primary candidates. This
is useful operational behavior, but it also compresses model spread; the
generator and threshold-curve diagnostics should be inspected alongside the
primary score.

Scores are benchmark-relative. Golden comments may be incomplete, and the
confidence intervals account for pull-request sampling but not judge-model
uncertainty.

## Context and reliability audit

All candidates used agentic repository context with the same declared
200,000-token capacity. Prompt/evidence bounds were hit in 1–5 of 50 reviews
per model. Discovery-bound signals were more common, primarily exhausted
selection rounds and unresolved search/list pagination. Full per-review
telemetry is retained in the run artifacts, and aggregate counts are in
[`bugbunny_evaluation_audit.json`](export/anthropic_claude-opus-4-5/bugbunny_evaluation_audit.json).

The Martian catalog did not expose input context capacity for these routes;
200,000 is therefore a conservative, equal declaration rather than a claim
about the providers' maximum capacity. This differs from the earlier GPT-5.6
sweep's 1,050,000-token declaration, so the two bundles are not perfectly
controlled for context capacity. The four models inside this sweep are.

The initial generation pass completed 187/200 reviews. Thirteen failures were
kept out of scoring: eight HTTP 529 overloads, three reasoning-only responses
without schema content, one invalid structured response, and one repeated read
timeout. Five identical checkpoint-resume passes eventually produced all 200
valid artifacts. The history is preserved under `recovery/`; the final run has
50 completed artifacts per model and no failed or partial review.

## Frozen experiment contract

- BugBunny version / source commit: `0.7.2` / `91eb604de7576b219267ab4beb6674f77c959780`
- CodeReviewBench source commit: `2b092b670f7d6cae6d429babaaee18948b4bdacb`
- Input SHA-256: `60fe361f1430ce3d55bf85d2ceba30205c6a207eb2c49447a2b527e23718d5ed`
- Canonical golden SHA-256: `e7838f447b938bc3c5cb704eb619a4395cfea2757597e5fb659397ca3b0674d0`
- Cases / golden issues: 50 / 173
- Profile / policy / context: `balanced` / `codereviewbench` / `agentic`
- Candidate reasoning: `high`, sent to all four Martian routes
- Candidate temperature: omitted for all four routes
- Candidate declared context / output cap: 200,000 / 50,000 tokens
- Verifier and judge: `anthropic/claude-opus-4-5`
- Verifier reasoning request: `low` (not sent by this route adapter)
- Verifier temperature / output cap / declared context: 0 / 32,768 / 200,000 tokens
- Operating point: `bugbunny-op-43df334528999a9c`, verifier confidence 0.92
- Bootstrap: 5,000 pull-request resamples, seed 17,042
- Final judge scope: exactly 12 exported BugBunny tool IDs, 600 review/tool matrices, 3,007 TP/FP/FN decisions, zero errors and zero timeouts

The immutable job plan records every resolved base/head SHA and the complete
effective context configuration. [`experiment.json`](experiment.json) records
commands, route behavior, recovery, telemetry, metrics, and file hashes.

## Reproduce

Use fresh output directories. Reusing a run directory is safe only when
BugBunny accepts its frozen job-plan hash.

```bash
git clone https://github.com/pratyakshs/BugBunny.git
cd BugBunny
git checkout 91eb604de7576b219267ab4beb6674f77c959780
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

git clone https://github.com/code-review-benchmark/code-review-benchmark.git ../code-review-benchmark
git -C ../code-review-benchmark checkout 2b092b670f7d6cae6d429babaaee18948b4bdacb
shasum -a 256 ../code-review-benchmark/offline/results/benchmark_data.json

cp .env.example .env
# Set MARTIAN_API_KEY in the ignored .env file.

bugbunny benchmark run \
  --benchmark-data ../code-review-benchmark/offline/results/benchmark_data.json \
  --fixture-tool auto \
  --model anthropic/claude-sonnet-5 \
  --model anthropic/claude-opus-5 \
  --model anthropic/claude-opus-4-8 \
  --model anthropic/claude-fable-5 \
  --profile balanced \
  --review-policy codereviewbench \
  --verifier-model anthropic/claude-opus-4-5 \
  --reasoning-effort high \
  --verifier-reasoning-effort low \
  --context-mode agentic \
  --model-context-window anthropic/claude-sonnet-5=200000 \
  --model-context-window anthropic/claude-opus-5=200000 \
  --model-context-window anthropic/claude-opus-4-8=200000 \
  --model-context-window anthropic/claude-fable-5=200000 \
  --verifier-context-window-tokens 200000 \
  --max-output-tokens 50000 \
  --verifier-max-output-tokens 32768 \
  --context-selection-rounds 2 \
  --context-requests-per-round 16 \
  --max-context-files 64 \
  --context-read-lines 240 \
  --context-read-chars 48000 \
  --context-blob-read-bytes 16000000 \
  --context-search-hits 24 \
  --context-search-max-offset 100000 \
  --repository-index-chars 120000 \
  --operating-point calibration/opus-4-5-low-v1.json \
  --llm-concurrency 1 \
  --active-reviews 4 \
  --global-llm-concurrency 4 \
  --github-concurrency 16 \
  --git-concurrency 4 \
  --timeout 900 \
  --cache-dir .bugbunny-cache/codereviewbench \
  --run-dir runs/claude-family \
  --env-file .env

bugbunny benchmark export \
  --benchmark-data ../code-review-benchmark/offline/results/benchmark_data.json \
  --run-dir runs/claude-family \
  --judge-model anthropic/claude-opus-4-5 \
  --finding-stage generator \
  --finding-stage balanced \
  --finding-stage family \
  --output-dir judge-results/claude-family
```

Run the judge with one `--tool` argument for every `tool_id` in
`bugbunny_export_index.json`. The exact 12-ID argv is stored in
`experiment.json`; the essential limits are:

```bash
bugbunny benchmark judge \
  --results-dir judge-results/claude-family \
  --judge-model anthropic/claude-opus-4-5 \
  --tool '<repeat for each export-index tool_id>' \
  --judge-concurrency 10 \
  --review-concurrency 5 \
  --call-timeout 60 \
  --review-timeout 1800 \
  --max-retries 5 \
  --env-file .env

bugbunny benchmark analyze \
  --run-dir runs/claude-family \
  --results-dir judge-results/claude-family \
  --judge-model anthropic/claude-opus-4-5 \
  --bootstrap-samples 5000 \
  --bootstrap-seed 17042
```

The `--tool` filter is required. CodeReviewBench's shared export input also
contains submissions outside this experiment; judging without the filter
expands the scope and wastes calls. The archived `candidates.json` and
`dedup_groups.json` are deterministic projections containing only the 12 tool
IDs in this export index. Per-track export manifests retain the hashes recorded
during the original cumulative export; `bundle_manifest.json` is authoritative
for the archived filtered projections. The original benchmark input is pinned,
not copied.

## Archive layout

- `run/`: 200 complete JSON and 200 Markdown review artifacts, job plan,
  checkpoint, and run manifest.
- `export/anthropic_claude-opus-4-5/`: twelve filtered candidate tracks, 600
  zero-error matrices, 3,007 pair decisions, export manifests, candidate
  audits, deduplication records, and the JSON/Markdown statistical audit.
- `recovery/`: initial generation failure evidence and five identical resume
  checkpoints; no discarded unfiltered judge data is published.
- `calibration/` and `model_catalog_snapshot.json`: frozen operating point and
  route metadata used to interpret the run.
- `bundle_manifest.json`: SHA-256 and byte length for every archived file.

The export-generated `benchmark_data.json` is intentionally omitted. Recreate
it from the pinned CodeReviewBench input before exporting or judging.
