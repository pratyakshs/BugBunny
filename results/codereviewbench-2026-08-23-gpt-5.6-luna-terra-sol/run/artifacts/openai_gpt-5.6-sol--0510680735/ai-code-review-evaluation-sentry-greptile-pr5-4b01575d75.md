# BugBunny review

PR: [code-review-benchmark/sentry__sentry-greptile__augment__PR5__20260122#1](https://github.com/code-review-benchmark/sentry__sentry-greptile__augment__PR5__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `49a275847631` → `ea188e2d736f`
Coverage: 210/210 eligible hunks
Duration: 375.7s; model calls: 4

## Findings (6)

### 1. Feature-gated dashboard tables discard all query results

`high` · `bug` · [static/app/views/dashboards/widgetCard/chart.tsx:166 (RIGHT)](#)

Trigger: An organization with the `use-table-widget-visualization` feature opens any dashboard table widget whose query returned rows.

Impact: The widget always renders an empty table with no columns, regardless of `result.data`, `result.meta`, configured fields, aliases, or loading state.

Evidence: `columns={[]}
              tableData={{
                data: [],
                meta: {
                  fields: {},
                  units: {},
                },
              }}`

Suggested direction: Construct `columns` from the widget fields and pass `result.data` and `result.meta` to `TableWidgetVisualization`; preserve the existing aliases, loading behavior, and custom renderers.

### 2. Replay errors and breadcrumbs are compared in different timestamp units

`medium` · `bug` · [src/sentry/replays/endpoints/project_replay_summarize_breadcrumbs.py:155 (RIGHT)](#)

Trigger: A real replay recording uses RRWeb event timestamps in milliseconds while its linked Sentry error event has a nodestore timestamp in Unix seconds.

Impact: Nearly every error timestamp compares as earlier than every breadcrumb timestamp, so errors are inserted before the first replay event instead of at their actual chronological position. Seer receives a misleading sequence and may produce an incorrect summary.

Evidence: `] < event.get("timestamp", 0):`

Suggested direction: Normalize both timestamp sources to the same unit before sorting and comparison, and add a regression test using realistic millisecond replay timestamps.

### 3. Hidden span attributes remain visible until the user searches

`medium` · `bug` · [static/app/views/performance/newTraceDetails/traceDrawer/details/span/eapSections/attributes.tsx:67 (RIGHT)](#)

Trigger: A span contains one of the newly designated hidden attributes (`is_segment`, `project_id`, or `received`) and the attribute search input is empty.

Impact: The early empty-search return bypasses this predicate, so the internal attributes are displayed normally and disappear only after a search term is entered.

Evidence: `!HIDDEN_ATTRIBUTES.includes(attribute.name) &&
        attribute.name.toLowerCase().trim().includes(searchQuery.toLowerCase().trim())`

Suggested direction: Filter `HIDDEN_ATTRIBUTES` from the sorted list before branching on `searchQuery`, then apply the text filter to the already-filtered list.

### 4. Negative report ages pass validation

`low` · `api` · [src/sentry/issues/endpoints/browser_reporting_collector.py:47 (RIGHT)](#)

Trigger: An age-based report supplies a negative `age` value.

Impact: The malformed elapsed-time value is accepted and counted, unlike `timestamp`, which correctly enforces a nonnegative value.

Evidence: `age = serializers.IntegerField(required=False)`

Suggested direction: Set `min_value=0` on the `age` field and add a negative-age validation test.

### 5. Mutual exclusion fails when both time fields are zero

`low` · `bug` · [src/sentry/issues/endpoints/browser_reporting_collector.py:52 (RIGHT)](#)

Trigger: A report includes both `age: 0` and `timestamp: 0`.

Impact: Both cross-field checks use truthiness, so neither detects the other zero-valued field and the mixed-format report is accepted despite the stated mutual-exclusion rule.

Evidence: `if self.initial_data.get("age"):`

Suggested direction: Check key presence (`"age" in self.initial_data` and `"timestamp" in self.initial_data`) in a serializer-level validator rather than checking value truthiness.

### 6. Falsy custom table render results incorrectly invoke fallback rendering

`low` · `bug` · [static/app/views/dashboards/widgets/tableWidget/defaultTableCellRenderers.tsx:71 (RIGHT)](#)

Trigger: A custom body or header renderer deliberately returns a valid falsy React node such as `null`, an empty string, or `0` to render a blank or numeric cell.

Impact: The table ignores the custom renderer result and renders the default cell instead, contrary to the documented contract that only `undefined` requests fallback behavior.

Evidence: `if (cell) {`

Suggested direction: Use `if (cell !== undefined)` in both the body and header renderer wrappers.

## Audit trail

6 candidate(s) were retained in JSON but excluded from publication.
