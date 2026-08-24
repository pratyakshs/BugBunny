# BugBunny review

PR: [code-review-benchmark/grafana__grafana__augment__PR94942__20260122#1](https://github.com/code-review-benchmark/grafana__grafana__augment__PR94942__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `cbe1e7d63f09` → `f3317b329b4e`
Coverage: 16/16 eligible hunks
Duration: 231.9s; model calls: 4

## Findings (1)

### 1. Retained feature flag still advertises a backend-disabled SQL capability

`medium` · `api` · [pkg/expr/reader.go:195 (RIGHT)](#)

Trigger: An administrator enables `sqlExpressions`; the frontend checks that same configured flag to expose SQL expressions, or an API client discovers the still-advertised SQL query type and submits one through `ExpressionQueryReader`.

Impact: The UI and query schema advertise SQL expressions as available, but every corresponding backend request is rejected, leaving enabled installations with a selectable capability that cannot execute.

Evidence: `enabled := !h.features.IsEnabledGlobally(featuremgmt.FlagSqlExpressions)
	if enabled {
		return false
	}
	return false`

Suggested direction: Keep backend execution disabled, but retire or force-disable `sqlExpressions` in the configuration exposed to the frontend and remove SQL from the advertised query-type schema until a safe implementation exists.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
