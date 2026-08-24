# CodeReviewBench: GPT-5.6 Luna, Terra, and Sol

This is a complete 50-case model sweep produced on 2026-08-23 through the
Martian Gateway. All three generation models used the same BugBunny pipeline,
the same fixed verifier and calibrated operating point, and the same fixed
judge. The primary comparison is the `balanced` stage.

## Primary result

| Model | Precision | Recall | F1 | F1 95% PR-bootstrap CI | Candidates |
| --- | ---: | ---: | ---: | ---: | ---: |
| `openai/gpt-5.6-luna` | 0.6049 | 0.2832 | 0.3858 | [0.3004, 0.4622] | 81 |
| `openai/gpt-5.6-terra` | 0.5934 | 0.3121 | 0.4091 | [0.3258, 0.4882] | 86 |
| `openai/gpt-5.6-sol` | 0.5957 | 0.4855 | 0.5350 | [0.4722, 0.5965] | 139 |

The paired pull-request bootstrap estimated Sol minus Terra at +0.1259 F1
(95% CI [0.0516, 0.2031]) and Sol minus Luna at +0.1492
(95% CI [0.0665, 0.2394]). Luna versus Terra was inconclusive.

These scores are benchmark-relative. Golden comments may be incomplete, and
the confidence intervals account for pull-request sampling but not judge-model
uncertainty.

## Frozen experiment contract

- BugBunny source commit: `29a9bc71e961fecf0a18aa0f6ffb3601e2b02e4f`
- CodeReviewBench source commit: `2b092b670f7d6cae6d429babaaee18948b4bdacb`
- Input: `offline/results/benchmark_data.json`
- Input SHA-256: `60fe361f1430ce3d55bf85d2ceba30205c6a207eb2c49447a2b527e23718d5ed`
- Canonical golden SHA-256: `e7838f447b938bc3c5cb704eb619a4395cfea2757597e5fb659397ca3b0674d0`
- Cases / golden issues: 50 / 173
- Profile / policy / context: `balanced` / `codereviewbench` / `agentic`
- Generation reasoning request: `high`
- Generation declared context: 1,050,000 tokens for each model
- Verifier and judge: `anthropic/claude-opus-4-5`
- Verifier reasoning request: `low` (the Anthropic transport does not send an
  OpenAI-only `reasoning_effort` field)
- Verifier declared context: 200,000 tokens
- Operating point: `bugbunny-op-43df334528999a9c`, verifier confidence 0.92
- Operating-point file: `calibration/opus-4-5-low-v1.json`
- Bootstrap: 5,000 pull-request resamples, seed 17,042

The complete resolved contract, including prompt budgets, policy and
calibration hashes, every resolved base/head SHA, and transport provenance, is
in [`run/run_manifest.json`](run/run_manifest.json). The machine-readable
experiment summary is [`experiment.json`](experiment.json).

## Reproduce

Use fresh output directories. A resumed directory is safe only when BugBunny
accepts its frozen job plan.

```bash
git clone https://github.com/pratyakshs/BugBunny.git
cd BugBunny
git checkout 29a9bc71e961fecf0a18aa0f6ffb3601e2b02e4f
python3 -m venv .venv
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
  --model openai/gpt-5.6-luna \
  --model openai/gpt-5.6-terra \
  --model openai/gpt-5.6-sol \
  --profile balanced \
  --review-policy codereviewbench \
  --verifier-model anthropic/claude-opus-4-5 \
  --reasoning-effort high \
  --verifier-reasoning-effort low \
  --context-mode agentic \
  --model-context-window openai/gpt-5.6-luna=1050000 \
  --model-context-window openai/gpt-5.6-terra=1050000 \
  --model-context-window openai/gpt-5.6-sol=1050000 \
  --verifier-context-window-tokens 200000 \
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
  --active-reviews 3 \
  --global-llm-concurrency 3 \
  --github-concurrency 16 \
  --git-concurrency 4 \
  --timeout 900 \
  --cache-dir .bugbunny-cache/codereviewbench \
  --run-dir runs/gpt-5.6-luna-terra-sol \
  --env-file .env

bugbunny benchmark export \
  --benchmark-data ../code-review-benchmark/offline/results/benchmark_data.json \
  --run-dir runs/gpt-5.6-luna-terra-sol \
  --judge-model anthropic/claude-opus-4-5 \
  --finding-stage generator \
  --finding-stage balanced \
  --finding-stage family \
  --output-dir results/gpt-5.6-luna-terra-sol

bugbunny benchmark judge \
  --results-dir results/gpt-5.6-luna-terra-sol \
  --judge-model anthropic/claude-opus-4-5 \
  --judge-concurrency 20 \
  --review-concurrency 10 \
  --call-timeout 60 \
  --review-timeout 1800 \
  --max-retries 5 \
  --env-file .env
```

The initial judge pass encountered gateway rate limits. Its persisted pair
matrix was retained, then the same command was resumed with
`--judge-concurrency 5 --review-concurrency 2`; only ten incomplete review/tool
matrices were repaired. No completed judgment was recomputed. Finally:

```bash
bugbunny benchmark analyze \
  --run-dir runs/gpt-5.6-luna-terra-sol \
  --results-dir results/gpt-5.6-luna-terra-sol \
  --judge-model anthropic/claude-opus-4-5 \
  --bootstrap-samples 5000 \
  --bootstrap-seed 17042
```

## Archive layout

- `run/`: 150 full generation artifacts plus the immutable job plan,
  checkpoint, and run manifest.
- `export/anthropic_claude-opus-4-5/`: nine candidate tracks, all 6,722 pair
  decisions, export manifests, deduplication records, and the JSON/Markdown
  statistical audit.
- `bundle_manifest.json`: checksums for all archived files.

The export-generated `benchmark_data.json` is intentionally not duplicated.
Regenerate it with the pinned input and `benchmark export`; the export index
records its expected SHA-256 as
`b0ff7ef28059e107dad4c51f01255c4a5fa1e107d24489361639e6140680be43`.

