# BugBunny evaluation audit

## Tracks

| Model | Stage | Candidates | Precision | Recall | F1 | F1 95% CI |
|---|---:|---:|---:|---:|---:|---:|
| anthropic/claude-fable-5 | balanced | 34 | 0.833 | 0.173 | 0.287 | [0.188, 0.381] |
| anthropic/claude-opus-4-8 | balanced | 40 | 0.800 | 0.208 | 0.330 | [0.245, 0.415] |
| anthropic/claude-opus-5 | balanced | 40 | 0.786 | 0.191 | 0.307 | [0.216, 0.392] |
| anthropic/claude-sonnet-5 | balanced | 41 | 0.791 | 0.197 | 0.315 | [0.217, 0.409] |
| anthropic/claude-fable-5 | family | 34 | 0.833 | 0.173 | 0.287 | [0.188, 0.381] |
| anthropic/claude-opus-4-8 | family | 38 | 0.837 | 0.208 | 0.333 | [0.246, 0.419] |
| anthropic/claude-opus-5 | family | 40 | 0.791 | 0.197 | 0.315 | [0.219, 0.405] |
| anthropic/claude-sonnet-5 | family | 36 | 0.850 | 0.197 | 0.319 | [0.218, 0.419] |
| anthropic/claude-fable-5 | generator | 354 | 0.353 | 0.717 | 0.473 | [0.414, 0.532] |
| anthropic/claude-opus-4-8 | generator | 189 | 0.459 | 0.514 | 0.485 | [0.412, 0.556] |
| anthropic/claude-opus-5 | generator | 495 | 0.252 | 0.723 | 0.373 | [0.318, 0.431] |
| anthropic/claude-sonnet-5 | generator | 253 | 0.344 | 0.503 | 0.408 | [0.343, 0.473] |

## Pipeline counts

| Model | Raw | Validated | Verified | Prompt-bound | Discovery-bound | Index-summarized | Gen budget max | Verifier budget max | Retries |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| anthropic/claude-fable-5 | 356 | 354 | 34 | 4 | 22 | 50 | 0.851 | 0.731 | 3 |
| anthropic/claude-opus-4-8 | 192 | 189 | 40 | 1 | 47 | 48 | 0.481 | 0.666 | 9 |
| anthropic/claude-opus-5 | 498 | 495 | 40 | 5 | 31 | 50 | 0.771 | 0.721 | 3 |
| anthropic/claude-sonnet-5 | 270 | 253 | 41 | 3 | 40 | 49 | 0.796 | 0.695 | 1 |

## Interpretation limits

- CodeReviewBench golden comments may be incomplete, so false positives are benchmark-relative.
- Threshold curves reuse the fixed judge pair matrix; they do not make additional judge calls.
- Confidence intervals resample pull requests and do not model judge-model uncertainty.
- Hierarchical repository-index summarization is reported separately from prompt and discovery bounds because the full inventory remains pageable.
