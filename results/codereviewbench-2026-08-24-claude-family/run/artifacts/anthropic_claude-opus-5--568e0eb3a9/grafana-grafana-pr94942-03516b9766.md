# BugBunny review

PR: [code-review-benchmark/grafana__grafana__augment__PR94942__20260122#1](https://github.com/code-review-benchmark/grafana__grafana__augment__PR94942__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-5`
Base/head: `cbe1e7d63f09` → `f3317b329b4e`
Coverage: 16/16 eligible hunks
Duration: 149.9s; model calls: 4

## Findings (1)

### 1. enableSqlExpressions computes an inverted flag check and discards it, always returning false

`medium` · `bug` · [pkg/expr/reader.go:195 (RIGHT)](#)

Trigger: Any call to ReadQuery with type "sql", with or without the sqlExpressions feature toggle enabled.

Impact: Both branches return false, so the toggle lookup is dead code; worse, the condition is negated (`enabled` is true when the flag is *off*), so a future maintainer who "fixes" the dead branch by returning true will re-enable SQL expressions exactly when the toggle is disabled, silently reintroducing the disabled code path.

Evidence: `enabled := !h.features.IsEnabledGlobally(featuremgmt.FlagSqlExpressions)
	if enabled {
		return false
	}
	return false`

Suggested direction: Either drop the feature lookup entirely and make the helper `return false` with a comment explaining SQL expressions are hard-disabled, or write the correct predicate `return h.features.IsEnabledGlobally(featuremgmt.FlagSqlExpressions)` and gate it separately.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
