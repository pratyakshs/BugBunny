# BugBunny review

PR: [code-review-benchmark/grafana__grafana__augment__PR94942__20260122#1](https://github.com/code-review-benchmark/grafana__grafana__augment__PR94942__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-fable-5`
Base/head: `cbe1e7d63f09` → `f3317b329b4e`
Coverage: 16/16 eligible hunks
Duration: 161.1s; model calls: 4

## Findings (1)

### 1. enableSqlExpressions always returns false, making the FlagSqlExpressions feature-toggle check dead code

`high` · `bug` · [pkg/expr/reader.go:195 (RIGHT)](#)

Trigger: An administrator enables the `sqlExpressions` feature toggle and submits an expression query with type "sql" through the expression-parser path (ReadQuery via buildCMDNode with FlagExpressionParser on).

Impact: Both branches of the helper return false, so `IsEnabledGlobally(featuremgmt.FlagSqlExpressions)` has no effect: SQL expressions are rejected with "sqlExpressions is not implemented" even when the feature flag is explicitly enabled. Additionally the variable `enabled` is assigned the negation of the flag, so it is true exactly when the feature is disabled, making the code actively misleading.

Evidence: `enabled := !h.features.IsEnabledGlobally(featuremgmt.FlagSqlExpressions)
	if enabled {
		return false
	}
	return false`

Suggested direction: If the flag is meant to gate the feature, replace the helper body with `return h.features.IsEnabledGlobally(featuremgmt.FlagSqlExpressions)` (and make it a method on *ExpressionQueryReader). If SQL expressions are meant to be unconditionally disabled for the security fix, delete the flag check and the helper and return the error directly in the QueryTypeSQL case.

## Audit trail

1 candidate(s) were retained in JSON but excluded from publication.
