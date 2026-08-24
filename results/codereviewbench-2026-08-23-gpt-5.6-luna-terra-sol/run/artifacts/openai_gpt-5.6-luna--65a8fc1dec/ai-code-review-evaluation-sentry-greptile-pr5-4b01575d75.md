# BugBunny review

PR: [code-review-benchmark/sentry__sentry-greptile__augment__PR5__20260122#1](https://github.com/code-review-benchmark/sentry__sentry-greptile__augment__PR5__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `49a275847631` → `ea188e2d736f`
Coverage: 210/210 eligible hunks
Duration: 191.6s; model calls: 4

## Findings (2)

### 1. The table-widget rollout renders an empty placeholder instead of query results

`high` · `bug` · [static/app/views/dashboards/widgetCard/chart.tsx:164 (RIGHT)](#)

Trigger: Opening any dashboard table widget for an organization with the `use-table-widget-visualization` feature enabled.

Impact: The new visualization receives empty columns and empty data regardless of the query response, so the table appears blank and users cannot inspect results.

Evidence: `{organization.features.includes('use-table-widget-visualization') ? (`

Suggested direction: Pass the current `result.data` and `result.meta` as `tableData`, derive columns from the actual query fields or metadata, and preserve the existing title/custom renderer behavior as needed.

### 2. Attributes marked hidden remain visible when no search is entered

`low` · `bug` · [static/app/views/performance/newTraceDetails/traceDrawer/details/span/eapSections/attributes.tsx:67 (RIGHT)](#)

Trigger: Opening the span attributes section with an empty search query.

Impact: `is_segment`, `project_id`, and `received` are returned by the early `!searchQuery.trim()` path and are displayed despite being declared in `HIDDEN_ATTRIBUTES`.

Evidence: `!HIDDEN_ATTRIBUTES.includes(attribute.name) &&
        attribute.name.toLowerCase().trim().includes(searchQuery.toLowerCase().trim())`

Suggested direction: Filter `HIDDEN_ATTRIBUTES` from `sorted` before the empty-search early return, then apply the search predicate to the already-hidden list.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
