# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `c22f76867f59` → `daffb05b5ad0`
Coverage: 41/41 eligible hunks
Duration: 215.8s; model calls: 4

## Findings (2)

### 1. The raw token ID null check validates the wrong field

`low` · `bug` · [services/src/main/java/org/keycloak/protocol/oidc/encode/AccessTokenContext.java:73 (RIGHT)](#)

Trigger: A caller constructs AccessTokenContext with a non-null grantType and a null rawTokenId.

Impact: The invalid context is accepted, and encodeTokenId() produces an ID containing the literal `null` instead of failing at construction.

Evidence: `Objects.requireNonNull(grantType, "Null rawTokenId not allowed");`

Suggested direction: Change the second check to Objects.requireNonNull(rawTokenId, "Null rawTokenId not allowed").

### 2. Access-token event tests never verify the expected grant shortcut

`medium` · `test_gap` · [testsuite/integration-arquillian/tests/base/src/test/java/org/keycloak/testsuite/AssertEvents.java:483 (RIGHT)](#)

Trigger: Any event assertion uses isAccessTokenId() with a valid encoded access-token ID such as `onrtac:<uuid>`.

Impact: The matcher rejects the match when its incorrectly selected characters happen to equal the expected shortcut, and otherwise returns true without checking the grant; tests can pass even when the token has the wrong grant context.

Evidence: `if (items[0].substring(3, 5).equals(expectedGrantShortcut)) return false;`

Suggested direction: Extract the grant shortcut from substring(4, 6) and return its equality result combined with the UUID check, rather than returning false when it matches.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
