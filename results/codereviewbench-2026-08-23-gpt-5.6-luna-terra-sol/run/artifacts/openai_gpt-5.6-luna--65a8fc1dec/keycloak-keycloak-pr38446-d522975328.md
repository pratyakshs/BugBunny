# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR38446__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR38446__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `85737f52b59a` → `ce2dcc537b4b`
Coverage: 20/20 eligible hunks
Duration: 218.7s; model calls: 4

## Findings (1)

### 1. Federated recovery codes are never consumed after successful validation

`high` · `security` · [testsuite/integration-arquillian/servers/auth-server/services/testsuite-providers/src/main/java/org/keycloak/testsuite/federation/BackwardsCompatibilityUserStorage.java:340 (RIGHT)](#)

Trigger: A user authenticates through the newly added federated recovery-code path and then submits the same recovery code again.

Impact: The compatibility user storage accepts the same bearer recovery code indefinitely, defeating the one-time-use protection provided by the local recovery-code provider.

Evidence: `return generatedKeys.stream().anyMatch(key -> key.equals(input.getChallengeResponse()));`

Suggested direction: On a successful match, atomically remove the matched code from the stored list and persist the updated credential; add coverage that rejects reuse of a consumed code.

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
