# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR38446__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR38446__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `85737f52b59a` → `ce2dcc537b4b`
Coverage: 20/20 eligible hunks
Duration: 436.0s; model calls: 4

## Findings (1)

### 1. The test user-storage provider allows the same recovery code to be reused indefinitely

`medium` · `test_gap` · [testsuite/integration-arquillian/servers/auth-server/services/testsuite-providers/src/main/java/org/keycloak/testsuite/federation/BackwardsCompatibilityUserStorage.java:340 (RIGHT)](#)

Trigger: The same externally stored recovery code is submitted in two or more login attempts.

Impact: Every attempt succeeds because validation never removes or marks the matched code as consumed. The new integration test's single successful login therefore gives false assurance for a credential that is required to be single-use.

Evidence: `return generatedKeys.stream().anyMatch(key -> key.equals(input.getChallengeResponse()));`

Suggested direction: Persist consumption atomically when validation succeeds and add an integration assertion that immediately reusing the submitted code is rejected.

## Audit trail

8 candidate(s) were retained in JSON but excluded from publication.
