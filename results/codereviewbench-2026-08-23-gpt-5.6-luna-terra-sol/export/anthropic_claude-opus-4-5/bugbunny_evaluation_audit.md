# BugBunny evaluation audit

## Tracks

| Model | Stage | Candidates | Precision | Recall | F1 | F1 95% CI |
|---|---:|---:|---:|---:|---:|---:|
| openai/gpt-5.6-luna | balanced | 81 | 0.605 | 0.283 | 0.386 | [0.300, 0.462] |
| openai/gpt-5.6-sol | balanced | 139 | 0.596 | 0.486 | 0.535 | [0.472, 0.596] |
| openai/gpt-5.6-terra | balanced | 86 | 0.593 | 0.312 | 0.409 | [0.326, 0.488] |
| openai/gpt-5.6-luna | family | 79 | 0.593 | 0.277 | 0.378 | [0.297, 0.455] |
| openai/gpt-5.6-sol | family | 138 | 0.596 | 0.486 | 0.535 | [0.472, 0.596] |
| openai/gpt-5.6-terra | family | 86 | 0.593 | 0.312 | 0.409 | [0.326, 0.488] |
| openai/gpt-5.6-luna | generator | 266 | 0.341 | 0.526 | 0.414 | [0.351, 0.475] |
| openai/gpt-5.6-sol | generator | 471 | 0.256 | 0.694 | 0.374 | [0.328, 0.422] |
| openai/gpt-5.6-terra | generator | 249 | 0.365 | 0.532 | 0.433 | [0.370, 0.498] |

## Pipeline counts

| Model | Raw | Validated | Verified | Prompt-bound | Discovery-bound | Index-summarized | Gen budget max | Verifier budget max | Retries |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| openai/gpt-5.6-luna | 274 | 266 | 81 | 3 | 47 | 47 | 0.169 | 0.641 | 0 |
| openai/gpt-5.6-sol | 476 | 471 | 139 | 7 | 33 | 47 | 0.168 | 0.722 | 2 |
| openai/gpt-5.6-terra | 256 | 249 | 86 | 0 | 42 | 47 | 0.142 | 0.645 | 0 |

## Interpretation limits

- CodeReviewBench golden comments may be incomplete, so false positives are benchmark-relative.
- Threshold curves reuse the fixed judge pair matrix; they do not make additional judge calls.
- Confidence intervals resample pull requests and do not model judge-model uncertainty.
- Hierarchical repository-index summarization is reported separately from prompt and discovery bounds because the full inventory remains pageable.
