# BugBunny review

PR: [code-review-benchmark/sentry__sentry__augment__PR77754__20260122#1](https://github.com/code-review-benchmark/sentry__sentry__augment__PR77754__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-5`
Base/head: `bb5a6837cb5b` → `9501091c52ae`
Coverage: 15/15 eligible hunks
Duration: 209.8s; model calls: 4

## Findings (2)

### 1. `queued` default evaluated once at import time

`medium` · `bug` · [src/sentry/integrations/services/assignment_source.py:18 (RIGHT)](#)

Trigger: Any `AssignmentSource(...)` or `AssignmentSource.from_integration(...)` construction after the module is imported (e.g. an inbound Jira webhook assigning a group).

Impact: Every instance carries the same timestamp — the moment the module was first imported — instead of the time the assignment was queued, so `queued` (and the value in `to_dict()`/task payloads) is stale and useless for debouncing or latency logging.

Evidence: `queued: datetime = timezone.now()`

Suggested direction: Use `queued: datetime = field(default_factory=timezone.now)` so the default is computed per instance.

### 2. Misspelled test name `test_from_dict_inalid_data`

`low` · `style` · [tests/sentry/integrations/services/test_assignment_source.py:13 (RIGHT)](#)

Trigger: Anyone selecting the test by name (`-k invalid_data`) or reading the test report.

Impact: The test cannot be found by the expected name and the report shows a typo for the invalid-input case.

Evidence: `def test_from_dict_inalid_data(self):`

Suggested direction: Rename the method to `test_from_dict_invalid_data`.

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
