# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `c22f76867f59` → `daffb05b5ad0`
Coverage: 41/41 eligible hunks
Duration: 311.2s; model calls: 4

## Findings (2)

### 1. Raw token IDs are not null-checked

`low` · `bug` · [services/src/main/java/org/keycloak/protocol/oidc/encode/AccessTokenContext.java:73 (RIGHT)](#)

Trigger: A caller constructs AccessTokenContext with a non-null grantType but a null rawTokenId and passes it to the default encoder.

Impact: Construction unexpectedly succeeds and encoding appends the literal string "null", potentially producing repeated or misleading token IDs instead of rejecting invalid input.

Evidence: `Objects.requireNonNull(grantType, "Null rawTokenId not allowed");`

Suggested direction: Change this check to Objects.requireNonNull(rawTokenId, "Null rawTokenId not allowed").

### 2. Access-token event matcher does not verify the expected grant shortcut

`medium` · `test_gap` · [testsuite/integration-arquillian/tests/base/src/test/java/org/keycloak/testsuite/AssertEvents.java:483 (RIGHT)](#)

Trigger: A tested flow emits an access-token ID with an incorrect grant shortcut, such as "onrtzz:<uuid>" when "ac" is expected.

Impact: The matcher normally returns true because it reads characters 3-4 instead of 4-5 and rejects equality rather than inequality, allowing grant-encoding regressions to pass integration tests.

Evidence: `if (items[0].substring(3, 5).equals(expectedGrantShortcut)) return false;`

Suggested direction: Require a six-character prefix and return false unless items[0].substring(4, 6).equals(expectedGrantShortcut).

## Audit trail

9 candidate(s) were retained in JSON but excluded from publication.
