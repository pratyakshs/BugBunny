# BugBunny review

PR: [code-review-benchmark/sentry__sentry-greptile__augment__PR3__20260122#1](https://github.com/code-review-benchmark/sentry__sentry-greptile__augment__PR3__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `a5d290951def` → `6ad6fe3899f6`
Coverage: 13/13 eligible hunks
Duration: 466.7s; model calls: 4

## Findings (2)

### 1. Upsampling eligibility checks the outer dataset instead of the dataset being queried

`medium` · `bug` · [src/sentry/api/endpoints/organization_events_stats.py:221 (RIGHT)](#)

Trigger: A Discover stats request is automatically split or scoped to the errors dataset, while the original outer `dataset` remains `discover` and the query does not literally contain `event.type:error`.

Impact: The helper classifies the request using the wrong dataset and returns false, so allowlisted error events are counted without upsampling.

Evidence: `snuba_params, organization, dataset, request`

Suggested direction: Pass `scoped_dataset` to `is_errors_query_for_error_upsampled_projects` so eligibility follows the query that `_get_event_stats` actually executes.

### 2. Substring matching misclassifies negated and mixed event-type queries

`medium` · `bug` · [src/sentry/api/helpers/error_upsampling.py:137 (RIGHT)](#)

Trigger: A Discover query contains the token in a different semantic context, such as `!event.type:error` or `(event.type:error OR event.type:transaction)`.

Impact: The query is treated as error-only and `count()` is replaced with a sample-weight sum even though the result includes or exclusively targets event types without error sample weights, producing undercounted results or an invalid backend query.

Evidence: `if "event.type:error" in query:`

Suggested direction: Use the parsed search conditions or resolved dataset to prove that the query is restricted to error events; do not infer semantics with a raw substring search.

## Audit trail

8 candidate(s) were retained in JSON but excluded from publication.
