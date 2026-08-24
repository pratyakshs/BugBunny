# BugBunny review

PR: [code-review-benchmark/grafana__grafana__augment__PR106778__20260122#1](https://github.com/code-review-benchmark/grafana__grafana__augment__PR106778__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `47e5bd23163e` → `8df850371034`
Coverage: 25/25 eligible hunks
Duration: 134.4s; model calls: 4

## Findings (1)

### 1. Filtered Grafana rule items are rendered without React keys

`low` · `bug` · [public/app/features/alerting/unified/rule-list/FilterView.tsx:157 (RIGHT)](#)

Trigger: Filtered results are replaced or reordered as asynchronous sources/pages complete or the filter changes.

Impact: React reconciles these stateful GrafanaRuleListItem/RuleActionsButtons instances by array index, producing key warnings and allowing menu, modal, or drawer state to be retained by a different rule after reordering.

Evidence: `<GrafanaRuleListItem`

Suggested direction: Pass `key={key}` to GrafanaRuleListItem, matching the datasource and unknown-rule branches.

## Audit trail

7 candidate(s) were retained in JSON but excluded from publication.
