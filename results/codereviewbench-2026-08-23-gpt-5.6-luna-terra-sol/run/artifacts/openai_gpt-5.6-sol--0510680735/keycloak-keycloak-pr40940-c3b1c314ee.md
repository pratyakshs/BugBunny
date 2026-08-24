# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR40940__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR40940__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `c834e7473c02` → `4def149a50c4`
Coverage: 5/5 eligible hunks
Duration: 197.4s; model calls: 4

## Findings (1)

### 1. Preserve the non-null contract of getSubGroupsCount

`medium` · `api` · [model/infinispan/src/main/java/org/keycloak/models/cache/infinispan/GroupAdapter.java:275 (RIGHT)](#)

Trigger: A cached GroupAdapter is used after the underlying group has concurrently been deleted, causing modelSupplier.get() to return null.

Impact: getSubGroupsCount() returns null despite GroupModel documenting that it never does so; admin responses requested with subGroupsCount enabled can consequently contain a null or omitted count, and callers that unbox the result can still fail with an NPE.

Evidence: `return model == null ? null : model.getSubGroupsCount();`

Suggested direction: Return a non-null fallback such as 0L when the persistence model no longer exists, or remove the stale group from the result before invoking this method.

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
